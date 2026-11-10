"""Whole-item routing stops before evaluation; no owner rule is activated."""

from dataclasses import replace
from pathlib import Path
from shutil import copyfile
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import load_settings
from app.demo.scenarios import Scenario, get_scenario
from app.main import create_app
from app.models import Fact, Purchase
from app.worker.poller import poll_once

ROOT = Path(__file__).resolve().parents[2]
ENV = {
    "DATA_MODE": "synthetic",
    "ENGINE_MODE": "mock_reviewed_ruleset",
    "DEMO_MODE": "true",
    "LLM_MODE": "replay",
}


@pytest.fixture
def client(request: pytest.FixtureRequest, tmp_path: Path):
    signed = getattr(request, "param", True)
    rules = tmp_path / "rules"
    rules.mkdir()
    for name in ("entries.yaml", "rates.yaml", "catalog.yaml"):
        copyfile(ROOT / "rules" / name, rules / name)
    if signed:
        catalog = rules / "catalog.yaml"
        catalog.write_text(
            catalog.read_text().replace("status: candidate", "status: verified"),
            encoding="utf-8",
        )
    settings = replace(
        load_settings(ENV),
        database_url=f"sqlite:///{tmp_path / 'routing.db'}",
        rules_dir=str(rules),
    )
    with TestClient(create_app(settings, start_worker=False)) as session:
        # Default headers apply to reads as well as mutations.
        session.headers["X-Requested-With"] = "taxleak"
        login = session.post(
            "/api/v1/demo/login", json={}, headers={"Idempotency-Key": "login"}
        )
        assert login.status_code == 200, login.text
        if token := login.json().get("csrf_token"):
            session.headers["X-CSRF-Token"] = token
        yield session, signed


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Mock:
    spy = Mock(side_effect=AssertionError("Routing stop must not call the engine"))
    monkeypatch.setattr("app.domain.pipeline.evaluate_line", spy)
    return spy


def _scenario(case: str) -> Scenario:
    scenario = get_scenario(case)
    assert scenario is not None
    return scenario


def _create(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, scenario: Scenario
) -> str:
    # Only synthetic inputs vary; production demo fixtures remain untouched.
    monkeypatch.setattr("app.api.routes.get_scenario", lambda _: scenario)
    response = client.post(
        "/api/v1/demo/events",
        json={"scenario": scenario.id},
        headers={"Idempotency-Key": "routing-event"},
    )
    assert response.status_code == 200, response.text
    return response.json()["purchase_id"]


def _advance(client: TestClient, purchase_id: str) -> dict:
    assert poll_once(client.app.state.ctx) == 1
    response = client.get(f"/api/v1/purchases/{purchase_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _fact(detail: dict, field: str) -> dict:
    facts = [fact for fact in detail["facts"] if fact["field"] == field]
    assert len(facts) == 1
    return facts[0]


def _held(detail: dict, reason: str, named_fact: str, engine: Mock) -> None:
    assert detail["queue"] == "held"
    assert [(h["reason"], h["named_fact"]) for h in detail["holds"]] == [
        (reason, named_fact)
    ]
    assert detail["determinations"] == []
    assert detail["purchase_amount_note"] == "purchase amount, not tax"
    engine.assert_not_called()


def _nhr(detail: dict, reason: str, engine: Mock) -> dict:
    assert detail["queue"] == "needs_human_review"
    assert detail["holds"] == []
    assert len(detail["determinations"]) == 1
    row = detail["determinations"][0]
    assert row["jurisdiction"] == "ITEM"
    assert row["label"] == "NEEDS_HUMAN_REVIEW"
    assert row["nhr_reason"] == reason
    for field in (
        "engine_amount",
        "expected_tax",
        "direction",
        "engine_treatment",
        "rule_reference",
        "engine_request",
        "engine_response",
    ):
        assert row[field] is None, field
    engine.assert_not_called()
    return row


@pytest.mark.parametrize(
    "client",
    [False, True],
    indirect=True,
    ids=["candidate-catalog", "verified-catalog"],
)
def test_null_category_is_missing_not_unsupported(client, monkeypatch, engine):
    session, _ = client
    purchase_id = _create(session, monkeypatch, _scenario("case1"))
    with session.app.state.ctx.session_factory() as db:
        purchase = db.get(Purchase, purchase_id)
        assert purchase is not None
        purchase.category = None
        db.commit()
    _held(_advance(session, purchase_id), "MISSING_REQUIRED_FACT", "category", engine)


@pytest.mark.parametrize(
    "client",
    [False, True],
    indirect=True,
    ids=["candidate-catalog", "verified-catalog"],
)
def test_present_unsupported_category_precedes_catalog(client, monkeypatch, engine):
    session, _ = client
    scenario = replace(_scenario("case1"), category="SYNTHETIC_UNSUPPORTED_PRODUCT")
    purchase_id = _create(session, monkeypatch, scenario)
    _nhr(_advance(session, purchase_id), "NO_SUPPORTED_CATEGORY", engine)


@pytest.mark.parametrize(
    "unconfirmed_delivery", [False, True], ids=["missing", "unconfirmed"]
)
def test_hardware_bill_to_never_confirms_delivery(
    client, monkeypatch, engine, unconfirmed_delivery
):
    session, _ = client
    base = _scenario("case5")
    scenario = replace(base, lines=(replace(base.lines[0], delivery_date=None),))
    purchase_id = _create(session, monkeypatch, scenario)
    if unconfirmed_delivery:
        # The writer intentionally creates no delivery row without a delivery date.
        # Model an existing unconfirmed proposal without admitting bill-to evidence.
        with session.app.state.ctx.session_factory() as db:
            bill_to = db.scalar(
                select(Fact).where(
                    Fact.purchase_id == purchase_id, Fact.field_key == "bill_to"
                )
            )
            line_fact = db.scalar(
                select(Fact).where(
                    Fact.purchase_id == purchase_id, Fact.field_key == "shipping"
                )
            )
            assert bill_to is not None and line_fact is not None
            db.add(
                Fact(
                    id=str(uuid4()),
                    org_id=bill_to.org_id,
                    purchase_id=purchase_id,
                    line_id=line_fact.line_id,
                    field_key="delivery_evidence",
                    value="NY",
                    origin=bill_to.origin,
                    source="Synthetic unconfirmed delivery proposal",
                    evidence_rung=None,
                    confirmation="UNCONFIRMED",
                    version=1,
                    is_current=True,
                    supersedes=None,
                )
            )
            db.commit()
    detail = _advance(session, purchase_id)
    reason = (
        "UNCONFIRMED_REQUIRED_FACT" if unconfirmed_delivery else "MISSING_REQUIRED_FACT"
    )
    _held(detail, reason, "delivery_evidence", engine)
    assert detail["lines"][0]["delivery_date"] is None
    assert _fact(detail, "bill_to")["confirmation"] == "UNCONFIRMED"
    assert _fact(detail, "bill_to")["value"] == base.bill_to
    for field in ("tax_charged", "shipping"):
        fact = _fact(detail, field)
        assert fact["value"] == "0.00"
        assert fact["confirmation"] == "CONFIRMED"
        assert fact["source"] == "replay recording"


@pytest.mark.parametrize("tax_state", [False, True], ids=["TX-use", "CA-use-TX-tax"])
def test_undeclared_state_stops_whole_item(client, monkeypatch, engine, tax_state):
    session, _ = client
    base = _scenario("case3" if tax_state else "case1")
    line = (
        replace(base.lines[0], tax_jurisdiction="TX")
        if tax_state
        else replace(
            base.lines[0], location_state="TX", location_name="Synthetic Texas office"
        )
    )
    assert line.seats == line.seats_covered
    assert line.location_rung == 1 and not line.low_confidence
    purchase_id = _create(session, monkeypatch, replace(base, lines=(line,)))
    detail = _advance(session, purchase_id)
    location = _fact(detail, "user_location")
    assert location["value"] == ("CA" if tax_state else "TX")
    assert location["confirmation"] == "CONFIRMED"
    assert location["evidence_rung"] == 1
    if tax_state:
        charged = _fact(detail, "tax_charged_jurisdiction")
        assert charged["value"] == "TX" and charged["confirmation"] == "CONFIRMED"
        assert _fact(detail, "tax_charged")["value"] == base.lines[0].tax_charged
    row = _nhr(detail, "UNDECLARED_JURISDICTION", engine)
    refused = session.post(
        f"/api/v1/determinations/{row['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Synthetic routing refusal check",
            "expected_version": row["version"],
        },
        headers={"Idempotency-Key": "approve-undeclared"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE"
    engine.assert_not_called()


def test_empty_jurisdiction_set_holds(client, monkeypatch, engine):
    session, _ = client
    purchase_id = _create(session, monkeypatch, _scenario("case1"))
    with session.app.state.ctx.session_factory() as db:
        location = db.scalar(
            select(Fact).where(
                Fact.purchase_id == purchase_id, Fact.field_key == "user_location"
            )
        )
        assert location is not None
        # Existing confirmed but empty input passes catalog presence checking.
        # It still cannot establish a state or a no-tax result.
        location.value = ""
        db.commit()
    _held(
        _advance(session, purchase_id), "MISSING_REQUIRED_FACT", "user_location", engine
    )
