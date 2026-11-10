"""A missing tax line is not 0.00, and a Ramp gap over one cent stays unconfirmed."""

from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.db import init_database
from app.demo.scenarios import LineInput
from app.domain.pipeline import (
    _apply_cross_check,
    _write_facts,
    advance_purchase,
)
from app.domain.ruleset import Ruleset, load_ruleset
from app.models import (
    Determination,
    Document,
    Evidence,
    Fact,
    Hold,
    Purchase,
    PurchaseLine,
)
from app.seed import ORG_ID

ROOT = Path(__file__).resolve().parents[2]


def _rules(tmp_path: Path, *, signed: bool) -> Ruleset:
    dest = tmp_path / "rules"
    dest.mkdir()
    for name in ("entries.yaml", "rates.yaml", "catalog.yaml"):
        text = (ROOT / "rules" / name).read_text(encoding="utf-8")
        if signed and name == "catalog.yaml":
            text = text.replace("status: candidate", "status: verified")
        (dest / name).write_text(text, encoding="utf-8")
    return load_ruleset(dest)


def _line(
    *,
    amount: str = "1440.00",
    tax: str | None = None,
    jurisdiction: str | None = None,
) -> LineInput:
    return LineInput(
        description="Remote access subscription",
        line_amount=amount,
        tax_charged=tax,
        tax_jurisdiction=jurisdiction,
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


def _open(tmp_path: Path) -> Session:
    database = tmp_path / "facts.db"
    factory = init_database("sqlite:///" + database.resolve().as_posix())
    return factory()


def _write(
    session: Session,
    line: LineInput,
    *,
    entity_amount: str,
    ocr_taxes: str | None,
) -> Purchase:
    purchase = Purchase(
        id=str(uuid4()),
        org_id=ORG_ID,
        vendor="Synthetic Cloud Co",
        invoice_number=f"SYN-{uuid4().hex[:8]}",
        invoice_date="2026-11-15",
        entity_name="Acme Startup LLC",
        currency="USD",
        item_status="OPEN",
        category="REMOTE_ACCESS_SAAS",
        extraction_mode="replay_miss",
        facts_version=1,
        ramp_entity_amount=entity_amount,
        ramp_ocr_taxes=ocr_taxes,
        created_at=utcnow(),
    )
    session.add(purchase)
    document = Document(
        id=str(uuid4()),
        purchase_id=purchase.id,
        sha256="abc",
        mime_type="application/json",
        pages=1,
    )
    stored = PurchaseLine(
        id=str(uuid4()),
        purchase_id=purchase.id,
        line_number=1,
        description=line.description,
        quantity="1",
        unit_price=line.line_amount,
        line_amount=line.line_amount,
        service_period_start=line.service_start,
        service_period_end=line.service_end,
        delivery_date=None,
        location_name=line.location_name,
        shipping_amount=None,
    )
    session.add(document)
    session.add(stored)
    session.flush()
    _write_facts(session, purchase, document, [stored], (line,), "")
    return purchase


def _facts(session: Session, purchase_id: str) -> list[Fact]:
    return list(
        session.scalars(select(Fact).where(Fact.purchase_id == purchase_id)).all()
    )


def _hold(session: Session, purchase_id: str) -> Hold:
    hold = session.scalar(
        select(Hold).where(Hold.purchase_id == purchase_id, Hold.active.is_(True))
    )
    assert hold is not None
    return hold


def test_missing_tax_line_is_not_stored_as_zero(tmp_path: Path) -> None:
    session = _open(tmp_path)
    try:
        purchase = _write(
            session,
            _line(tax=None),
            entity_amount="1440.00",
            ocr_taxes="0.00",
        )
        advance_purchase(session, purchase.id, _rules(tmp_path, signed=True))
        facts = _facts(session, purchase.id)
        assert not any(fact.field_key == "tax_charged" for fact in facts)
        assert all(fact.value != "0.00" for fact in facts)
        hold = _hold(session, purchase.id)
        assert hold.reason == "MISSING_REQUIRED_FACT"
        assert hold.named_fact == "tax_charged"
        assert "0.00" not in f"{hold.reason} {hold.named_fact}"
        assert (
            session.scalars(
                select(Determination).where(Determination.purchase_id == purchase.id)
            ).all()
            == []
        )
    finally:
        session.close()


def test_explicit_zero_is_a_confirmed_fact_without_a_jurisdiction(
    tmp_path: Path,
) -> None:
    session = _open(tmp_path)
    try:
        purchase = _write(
            session,
            _line(tax="0.00"),
            entity_amount="1440.00",
            ocr_taxes="0.00",
        )
        facts = _facts(session, purchase.id)
        tax = next(fact for fact in facts if fact.field_key == "tax_charged")
        assert tax.value == "0.00"
        assert tax.confirmation == "CONFIRMED"
        evidence = session.scalar(select(Evidence).where(Evidence.fact_id == tax.id))
        assert evidence is not None
        assert not any(fact.field_key == "tax_charged_jurisdiction" for fact in facts)
        advance_purchase(session, purchase.id, _rules(tmp_path, signed=True))
        holds = session.scalars(
            select(Hold).where(Hold.purchase_id == purchase.id, Hold.active.is_(True))
        ).all()
        assert all(hold.named_fact != "tax_charged_jurisdiction" for hold in holds)
        assert all(hold.named_fact != "tax_charged" for hold in holds)
    finally:
        session.close()


def test_gap_over_one_cent_unconfirms_without_changing_the_amount(
    tmp_path: Path,
) -> None:
    session = _open(tmp_path)
    try:
        purchase = _write(
            session,
            _line(amount="1440.00", tax="10.00"),
            entity_amount="1442.00",
            ocr_taxes="10.00",
        )
        advance_purchase(session, purchase.id, _rules(tmp_path, signed=True))
        amount = next(
            fact
            for fact in _facts(session, purchase.id)
            if fact.field_key == "line_amount"
        )
        assert amount.value == "1440.00"
        assert amount.confirmation == "UNCONFIRMED"
        hold = _hold(session, purchase.id)
        assert hold.reason == "UNCONFIRMED_REQUIRED_FACT"
        assert hold.named_fact == "line_amount"
        assert (
            session.scalars(
                select(Determination).where(Determination.purchase_id == purchase.id)
            ).all()
            == []
        )
    finally:
        session.close()


def test_tax_gap_over_one_cent_unconfirms_the_stored_tax(tmp_path: Path) -> None:
    session = _open(tmp_path)
    try:
        purchase = _write(
            session,
            _line(amount="1440.00", tax="10.00"),
            entity_amount="1440.00",
            ocr_taxes="12.00",
        )
        advance_purchase(session, purchase.id, _rules(tmp_path, signed=True))
        tax = next(
            fact
            for fact in _facts(session, purchase.id)
            if fact.field_key == "tax_charged"
        )
        assert tax.value == "10.00"
        assert tax.confirmation == "UNCONFIRMED"
        hold = _hold(session, purchase.id)
        assert hold.reason == "UNCONFIRMED_REQUIRED_FACT"
        assert hold.named_fact == "tax_charged"
        assert (
            session.scalars(
                select(Determination).where(Determination.purchase_id == purchase.id)
            ).all()
            == []
        )
    finally:
        session.close()


def test_one_cent_difference_stays_confirmed(tmp_path: Path) -> None:
    session = _open(tmp_path)
    try:
        purchase = _write(
            session,
            _line(amount="1440.00", tax="10.00"),
            entity_amount="1440.01",
            ocr_taxes="10.01",
        )
        facts = _facts(session, purchase.id)
        lines = list(
            session.scalars(
                select(PurchaseLine).where(PurchaseLine.purchase_id == purchase.id)
            ).all()
        )
        _apply_cross_check(purchase, lines, facts)
        amount = next(fact for fact in facts if fact.field_key == "line_amount")
        tax = next(fact for fact in facts if fact.field_key == "tax_charged")
        assert amount.confirmation == "CONFIRMED"
        assert amount.value == "1440.00"
        assert tax.confirmation == "CONFIRMED"
        assert tax.value == "10.00"
    finally:
        session.close()


def test_unsigned_catalog_holds_before_any_determination(tmp_path: Path) -> None:
    session = _open(tmp_path)
    try:
        purchase = _write(
            session,
            _line(tax="0.00"),
            entity_amount="1440.00",
            ocr_taxes="0.00",
        )
        advance_purchase(session, purchase.id, _rules(tmp_path, signed=False))
        hold = _hold(session, purchase.id)
        assert hold.named_fact == "required_fact_catalog"
        assert hold.reason == "UNCONFIRMED_REQUIRED_FACT"
        assert (
            session.scalars(
                select(Determination).where(Determination.purchase_id == purchase.id)
            ).all()
            == []
        )
    finally:
        session.close()
