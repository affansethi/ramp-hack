"""RG-7: determination identity is unique and a losing race leaves no extras."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import UniqueConstraint, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import load_settings
from app.context import AppContext, build_context
from app.db import Base, init_database, make_engine, make_session_factory
from app.demo.scenarios import LineInput, Scenario, get_scenario
from app.domain.pipeline import advance_purchase, ingest_scenario
from app.domain.ruleset import Ruleset, load_ruleset
from app.models import Determination, Hold, Job, Purchase, PurchaseLine, Question
from app.seed import ORG_ID
from app.worker import poller

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = (
    "org_id",
    "line_id",
    "jurisdiction",
    "facts_version",
    "ruleset_version",
)


def test_uq_determination_is_enforced_on_a_fresh_sqlite_schema(tmp_path: Path) -> None:
    table = Base.metadata.tables["determinations"]
    named = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_determination"
    ]
    assert len(named) == 1
    assert tuple(column.name for column in named[0].columns) == IDENTITY
    assert table.c.line_id.nullable is False

    engine = make_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    Base.metadata.create_all(engine)
    columns = inspect(engine).get_columns("determinations")
    reflected = {column["name"]: column for column in columns}
    assert reflected["line_id"]["nullable"] is False
    uniques = inspect(engine).get_unique_constraints("determinations")
    assert any(
        constraint["name"] == "uq_determination"
        and constraint["column_names"] == list(IDENTITY)
        for constraint in uniques
    )

    factory = make_session_factory(engine)
    with factory() as session:
        purchase_id = _purchase(session)
        _add(session, purchase_id, "line-1", facts_version=1, ruleset_version="v1")
        session.commit()
        _add(session, purchase_id, "line-1", facts_version=2, ruleset_version="v1")
        session.commit()
        _add(session, purchase_id, "line-1", facts_version=1, ruleset_version="v2")
        session.commit()

        with pytest.raises(IntegrityError, match="UNIQUE"):
            _add(session, purchase_id, "line-1", facts_version=1, ruleset_version="v1")
            session.commit()
        session.rollback()

        with pytest.raises(IntegrityError, match="NOT NULL"):
            _add(session, purchase_id, None, facts_version=3, ruleset_version="v3")
            session.commit()
        session.rollback()


def test_advance_purchase_twice_keeps_one_determination_per_line_jurisdiction(
    tmp_path: Path,
) -> None:
    rules = load_ruleset(_signed_rules(tmp_path))
    database = tmp_path / "advance.db"
    factory = init_database("sqlite:///" + database.resolve().as_posix())
    scenario = Scenario(
        id="identity-lines",
        event_id="evt-identity-lines",
        vendor="Synthetic Cloud Co",
        invoice_number="SYN-IDENTITY",
        invoice_date="2026-11-15",
        entity_amount="300.00",
        ocr_taxes="0.00",
        category="REMOTE_ACCESS_SAAS",
        bill_to="Seattle bill-to, synthetic",
        lines=(
            _wa_line("100.00", "First office"),
            _wa_line("200.00", "Second office"),
        ),
    )
    with factory() as session:
        ingested = ingest_scenario(session, scenario, ORG_ID)
        session.commit()
        assert ingested.purchase_id is not None
        advance_purchase(session, ingested.purchase_id, rules)
        session.commit()
        before = _identity_rows(session, ingested.purchase_id)
        advance_purchase(session, ingested.purchase_id, rules)
        session.commit()
        after = _identity_rows(session, ingested.purchase_id)
        purchase = session.get(Purchase, ingested.purchase_id)
        assert purchase is not None
        assert purchase.item_status == "DETERMINED"
        assert purchase.facts_version == before[0][3]
        assert rules.version == before[0][4]
        assert list(session.scalars(select(Hold.id))) == []
        assert list(session.scalars(select(Question.id))) == []

    assert len(before) == 2
    assert before == after
    assert {row[1] for row in after} == {"WA"}
    assert len({row[0] for row in after}) == 2


def test_racing_insert_rolls_back_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules_dir = _signed_rules(tmp_path)
    ctx = build_context(
        replace(
            load_settings(),
            database_url=f"sqlite:///{tmp_path / 'race.db'}",
            rules_dir=str(rules_dir),
        )
    )
    scenario = get_scenario("case1")
    assert scenario is not None
    with ctx.session_factory() as session:
        ingested = ingest_scenario(session, scenario, ORG_ID)
        session.commit()
        assert ingested.job_id is not None
        assert ingested.purchase_id is not None
        job_id = ingested.job_id
        purchase_id = ingested.purchase_id

    planted: dict[str, str] = {}
    conflicts: list[IntegrityError] = []
    _record_conflicts(monkeypatch, conflicts)
    monkeypatch.setattr(poller, "advance_purchase", _racing_advance(ctx, planted))

    assert poller.poll_once(ctx) == 0
    job = _job(ctx, job_id)
    assert (job.status, job.attempts) == ("QUEUED", 1)
    assert conflicts
    assert all("UNIQUE" in str(error) for error in conflicts)
    mid = _effects(ctx, purchase_id)
    assert mid["determinations"] == [planted["id"]]
    assert mid["holds"] == []
    assert mid["questions"] == []
    assert _item_status(ctx, purchase_id) == "INTAKEN"

    assert poller.poll_once(ctx) == 1
    job = _job(ctx, job_id)
    assert (job.status, job.attempts, job.error_code) == ("SUCCEEDED", 2, None)
    done = _effects(ctx, purchase_id)
    assert done == mid
    assert done["jobs"] == [job_id]
    with ctx.session_factory() as session:
        row = session.get(Determination, planted["id"])
        lines = session.scalars(
            select(PurchaseLine).where(PurchaseLine.purchase_id == purchase_id)
        ).all()
        assert row is not None
        assert len(lines) == 1
        assert row.line_id == lines[0].id
        assert row.jurisdiction == "WA"
        assert row.is_current is True
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        assert purchase.item_status == "INTAKEN"


def _racing_advance(ctx: AppContext, planted: dict[str, str]):
    real = poller.advance_purchase

    def wrapped(session: Session, purchase_id: str, ruleset: Ruleset) -> None:
        def add(instance: object) -> None:
            # The duplicate check has already passed. SQLite allows one writer,
            # so release this attempt's open transaction, commit the same
            # identity from another session, then let the insert conflict.
            if "id" not in planted and isinstance(instance, Determination):
                competing = _clone(instance)
                session.rollback()
                with ctx.session_factory() as other:
                    other.add(competing)
                    other.commit()
                planted["id"] = competing.id
            Session.add(session, instance)

        session.add = add
        real(session, purchase_id, ruleset)

    return wrapped


def _record_conflicts(
    monkeypatch: pytest.MonkeyPatch, conflicts: list[IntegrityError]
) -> None:
    commit = Session.commit
    flush = Session.flush

    def tracked_commit(self: Session) -> None:
        try:
            commit(self)
        except IntegrityError as exc:
            conflicts.append(exc)
            raise

    def tracked_flush(self: Session, objects: object = None) -> None:
        try:
            flush(self, objects)
        except IntegrityError as exc:
            conflicts.append(exc)
            raise

    monkeypatch.setattr(Session, "commit", tracked_commit)
    monkeypatch.setattr(Session, "flush", tracked_flush)


def _clone(source: Determination) -> Determination:
    values = {
        column.key: getattr(source, column.key)
        for column in Determination.__table__.columns
    }
    values["id"] = str(uuid4())
    values["is_current"] = True
    return Determination(**values)


def _signed_rules(tmp_path: Path) -> Path:
    dest = tmp_path / "rules"
    dest.mkdir()
    for name in ("entries.yaml", "rates.yaml", "catalog.yaml"):
        text = (ROOT / "rules" / name).read_text(encoding="utf-8")
        if name == "catalog.yaml":
            text = text.replace("status: candidate", "status: signed")
            text = text.replace("signed_off_by:", "signed_off_by: AF")
        (dest / name).write_text(text, encoding="utf-8")
    return dest


def _wa_line(amount: str, description: str) -> LineInput:
    return LineInput(
        description=description,
        line_amount=amount,
        tax_charged="0.00",
        tax_jurisdiction=None,
        service_start="2026-11-01",
        service_end="2026-11-30",
        delivery_date=None,
        shipping=None,
        location_name="Seattle, WA",
        location_state="WA",
        location_rung=1,
        seats=10,
        seats_covered=10,
    )


def _purchase(session: Session) -> str:
    purchase_id = str(uuid4())
    session.add(
        Purchase(
            id=purchase_id,
            org_id="org-1",
            vendor="Synthetic Vendor",
            invoice_number="SYN-ID",
            invoice_date="2026-11-15",
            entity_name="Acme Startup LLC",
            currency="USD",
            item_status="INTAKEN",
            extraction_mode="replay",
            facts_version=1,
            created_at="2026-11-15T00:00:00Z",
        )
    )
    session.flush()
    return purchase_id


def _add(
    session: Session,
    purchase_id: str,
    line_id: str | None,
    *,
    facts_version: int,
    ruleset_version: str,
) -> None:
    session.add(
        Determination(
            id=str(uuid4()),
            org_id="org-1",
            purchase_id=purchase_id,
            line_id=line_id,
            jurisdiction="WA",
            facts_version=facts_version,
            ruleset_version=ruleset_version,
            derived_direction=False,
            unit_status="READY_FOR_REVIEW",
            version=1,
            is_current=True,
        )
    )


def _identity_rows(
    session: Session, purchase_id: str
) -> list[tuple[str, str, str, int, str]]:
    rows = session.scalars(
        select(Determination)
        .where(Determination.purchase_id == purchase_id)
        .order_by(Determination.line_id)
    ).all()
    return [
        (row.line_id, row.jurisdiction, row.id, row.facts_version, row.ruleset_version)
        for row in rows
    ]


def _item_status(ctx: AppContext, purchase_id: str) -> str:
    with ctx.session_factory() as session:
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        return purchase.item_status


def _job(ctx: AppContext, job_id: str) -> Job:
    with ctx.session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def _effects(ctx: AppContext, purchase_id: str) -> dict[str, list[str]]:
    with ctx.session_factory() as session:
        return {
            "determinations": list(
                session.scalars(
                    select(Determination.id).where(
                        Determination.purchase_id == purchase_id
                    )
                )
            ),
            "holds": list(
                session.scalars(select(Hold.id).where(Hold.purchase_id == purchase_id))
            ),
            "questions": list(
                session.scalars(
                    select(Question.id).where(Question.purchase_id == purchase_id)
                )
            ),
            "jobs": list(
                session.scalars(select(Job.id).order_by(Job.created_at, Job.id))
            ),
        }
