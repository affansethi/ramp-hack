"""Demo flow: unsigned rules hold; a signed test ruleset can approve without a zero."""

import hashlib
import hmac
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.guards import CSRF_HEADER
from app.api.routes import COOKIE
from app.config import load_settings
from app.main import create_app
from app.models import Job, Purchase, SourceEvent
from app.worker.poller import poll_once
from tests.api.conftest import assert_refund_candidates_stay_separate

ROOT = Path(__file__).resolve().parents[2]
GOOD_ENV = {
    "DATA_MODE": "synthetic",
    "ENGINE_MODE": "mock_reviewed_ruleset",
    "DEMO_MODE": "true",
    "LLM_MODE": "replay",
}


_csrf_token: str | None = None


def reset_csrf() -> None:
    global _csrf_token
    _csrf_token = None


def _headers(key: str, *, csrf: bool = True) -> dict[str, str]:
    headers = {"Idempotency-Key": key, "X-Requested-With": "taxleak"}
    if csrf and _csrf_token:
        headers[CSRF_HEADER] = _csrf_token
    return headers


def login(client: TestClient, key: str = "login") -> str:
    """Sign in, keep the HttpOnly cookie, and remember the CSRF token."""
    global _csrf_token
    _csrf_token = None
    response = client.post("/api/v1/demo/login", json={}, headers=_headers(key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert "session_id" not in body
    token = body["csrf_token"]
    assert isinstance(token, str) and token
    cookie = client.cookies.get(COOKIE)
    assert isinstance(cookie, str) and cookie != token
    assert cookie not in response.text
    _csrf_token = token
    return token


def _activate(dest: Path) -> None:
    dest.mkdir()
    for name in ("entries.yaml", "rates.yaml", "catalog.yaml"):
        text = (ROOT / "rules" / name).read_text(encoding="utf-8")
        text = text.replace("review_status: candidate", "review_status: reviewed")
        text = text.replace("status: candidate", "status: signed")
        text = text.replace("reviewed_by:", "reviewed_by: AF")
        text = text.replace("signed_off_by:", "signed_off_by: AF")
        (dest / name).write_text(text, encoding="utf-8")


@pytest.fixture
def signed_client(tmp_path: Path, _reset_csrf_token: None) -> TestClient:
    rules = tmp_path / "rules"
    _activate(rules)
    database = tmp_path / "app.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(rules),
        ramp_webhook_secret="test-secret",
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        login(client)
        yield client


def _run(client: TestClient, scenario: str, key: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/demo/events",
        json={"scenario": scenario},
        headers=_headers(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert poll_once(client.app.state.ctx) == 1 or body["duplicate"] is True
    return body


def test_unsigned_catalog_holds_case_1_with_no_tax_amount(tmp_path: Path) -> None:
    database = tmp_path / "plain.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(ROOT / "rules"),
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        login(client)
        created = _run(client, "case1", "case1")
        detail = client.get(f"/api/v1/purchases/{created['purchase_id']}")
        body = detail.json()
        assert body["queue"] == "held"
        assert body["holds"][0]["named_fact"] == "required_fact_catalog"
        assert body["determinations"] == []
        assert "engine_amount" not in json.dumps(body["holds"])


def test_signed_case_1_approves_the_engine_string(signed_client: TestClient) -> None:
    created = _run(signed_client, "case1", "case1")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    assert detail["queue"] == "ready"
    determination = detail["determinations"][0]
    assert determination["engine_amount"] == {"value": "151.92", "currency": "USD"}
    assert determination["rule_reference"] == "WA-RAS-001"
    approved = signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed the engine result",
            "expected_version": determination["version"],
        },
        headers=_headers("approve-case1"),
    )
    assert approved.status_code == 200, approved.text
    totals = signed_client.get("/api/v1/totals").json()
    engine = determination["engine_amount"]["value"]
    assert totals["approved_liabilities"]["by_jurisdiction"] == [
        {
            "jurisdiction": "WA",
            "period": "2026Q4",
            "total": {"value": engine, "currency": "USD"},
            "line_count": 1,
        }
    ]
    assert totals["approved_liabilities"]["message"] is None
    assert_refund_candidates_stay_separate(totals)


def test_case_6_holds_for_location_and_stays_out_of_totals(
    signed_client: TestClient,
) -> None:
    _run(signed_client, "case1", "seed-case1")
    created = _run(signed_client, "case6", "case6")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    assert detail["queue"] == "held"
    assert detail["holds"][0]["named_fact"] == "user_location"
    assert detail["determinations"] == []
    assert detail["purchase_amount_note"] == "purchase amount, not tax"
    totals = signed_client.get("/api/v1/totals").json()
    assert totals["held"]["count"] == 1
    assert totals["held"]["label"] == "purchase amount, not tax"
    assert totals["approved_liabilities"]["message"] == "No approved liabilities"
    assert "0.00" not in json.dumps(totals["approved_liabilities"])
    assert_refund_candidates_stay_separate(totals)


def test_case_4_is_unresolved_transition_with_no_amount(
    signed_client: TestClient,
) -> None:
    created = _run(signed_client, "case4", "case4")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    assert detail["queue"] == "needs_human_review"
    determination = detail["determinations"][0]
    assert determination["nhr_reason"] == "RULE_TRANSITION_UNRESOLVED"
    assert determination["engine_amount"] is None
    assert determination["expected_tax"] is None
    refused = signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "no",
            "expected_version": 1,
        },
        headers=_headers("nope"),
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE"


def test_replay_of_the_same_webhook_does_not_duplicate(
    signed_client: TestClient,
) -> None:
    first = _run(signed_client, "case1", "first")
    second = _run(signed_client, "case7", "second")
    assert second["duplicate"] is True
    assert second["purchase_id"] == first["purchase_id"]
    listing = signed_client.get("/api/v1/purchases").json()
    assert len(listing["purchases"]) == 1


def test_fact_endpoint_refuses_a_treatment(signed_client: TestClient) -> None:
    created = _run(signed_client, "case6", "case6-forbid")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    refused = signed_client.post(
        f"/api/v1/purchases/{created['purchase_id']}/facts",
        json={
            "field": "treatment",
            "value": "taxable",
            "source": "no",
            "reason": "no",
            "expected_version": detail["facts_version"],
        },
        headers=_headers("forbid"),
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "FORBIDDEN_FIELD"


def test_bad_signature_stores_nothing_payable(tmp_path: Path) -> None:
    database = tmp_path / "hook.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(ROOT / "rules"),
        ramp_webhook_secret="test-secret",
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        raw = json.dumps(
            {
                "id": "evt_bad",
                "transaction": {
                    "id": "txn_bad",
                    "amount": "10.00",
                    "currency_code": "USD",
                    "merchant_name": "Synthetic Vendor",
                    "user_transaction_time": "2026-11-15T00:00:00Z",
                },
            }
        ).encode()
        bad = client.post(
            "/api/v1/webhooks/ramp",
            content=raw,
            headers={
                "Idempotency-Key": "bad",
                "X-Ramp-Signature": "sha256=nope",
                "Content-Type": "application/json",
            },
        )
        assert bad.status_code == 401
        assert bad.json()["error"]["code"] == "BAD_SIGNATURE"
        replayed = client.post(
            "/api/v1/webhooks/ramp",
            content=raw,
            headers={
                "Idempotency-Key": "bad",
                "X-Ramp-Signature": "sha256=nope",
                "Content-Type": "application/json",
            },
        )
        assert replayed.status_code == 401
        assert replayed.json()["error"]["code"] == "BAD_SIGNATURE"
        not_json = client.post(
            "/api/v1/webhooks/ramp",
            content=b"not-json",
            headers={
                "Idempotency-Key": "bad-json",
                "X-Ramp-Signature": "sha256=nope",
                "Content-Type": "application/json",
            },
        )
        assert not_json.status_code == 401
        assert not_json.json()["error"]["code"] == "BAD_SIGNATURE"
        assert _count(client, SourceEvent) == 0
        assert _count(client, Purchase) == 0
        assert _count(client, Job) == 0
        assert _event(client, "evt_bad") is None
        digest = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        good = client.post(
            "/api/v1/webhooks/ramp",
            content=raw,
            headers={
                "Idempotency-Key": "good",
                "X-Ramp-Signature": f"sha256={digest}",
                "Content-Type": "application/json",
            },
        )
        assert good.status_code == 200, good.text
        same_event = client.post(
            "/api/v1/webhooks/ramp",
            content=raw,
            headers={
                "Idempotency-Key": "good-again",
                "X-Ramp-Signature": f"sha256={digest}",
                "Content-Type": "application/json",
            },
        )
        assert same_event.status_code == 200, same_event.text
        assert same_event.json()["purchase_id"] == good.json()["purchase_id"]
        assert same_event.json()["duplicate"] is True
        assert _count(client, Purchase) == 1
        assert _count(client, Job) == 1
        stored = _event(client, "evt_bad")
        assert stored is not None
        assert stored.signature_result == "OK"
        assert poll_once(client.app.state.ctx) == 1
        login(client, "login-hook")
        detail = client.get(f"/api/v1/purchases/{good.json()['purchase_id']}").json()
        assert detail["determinations"] == []
        assert detail["queue"] == "held"


def _count(client: TestClient, model: type[object]) -> int:
    session = _open(client)
    try:
        found = session.scalar(select(func.count()).select_from(model))
        return int(found or 0)
    finally:
        session.close()


def _event(client: TestClient, event_id: str) -> SourceEvent | None:
    session = _open(client)
    try:
        return session.scalar(
            select(SourceEvent).where(SourceEvent.external_event_id == event_id)
        )
    finally:
        session.close()


def _open(client: TestClient) -> Session:
    return client.app.state.ctx.session_factory()
