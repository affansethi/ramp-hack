"""Scenario printed fields pass the extractor schema and stay labeled replay.

The schema check runs in memory. Cases 1, 4, 6, and 7 do not need a recording.
A forbidden key, including rate, is refused before any fact or amount is written.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.adapters.extractor import FORBIDDEN_KEYS, _normalize_key
from app.config import load_settings
from app.demo.scenarios import Scenario
from app.domain import pipeline
from app.main import create_app
from app.models import Determination, Document, Fact, Hold, Purchase, PurchaseLine
from app.worker.poller import poll_once
from tests.api.test_flow import _activate

GOOD_ENV = {
    "DATA_MODE": "synthetic",
    "ENGINE_MODE": "mock_reviewed_ruleset",
    "DEMO_MODE": "true",
    "LLM_MODE": "replay",
}


def _headers(key: str | None = None) -> dict[str, str]:
    headers = {"X-Requested-With": "taxleak"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    rules = tmp_path / "rules"
    _activate(rules)
    database = tmp_path / "admit.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(rules),
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as session:
        session.headers["X-Requested-With"] = "taxleak"
        login = session.post("/api/v1/demo/login", json={}, headers=_headers("login"))
        assert login.status_code == 200, login.text
        token = login.json().get("csrf_token")
        if isinstance(token, str):
            session.headers["X-CSRF-Token"] = token
        yield session


def _block_recordings(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("scenario admission must not read a recording")

    monkeypatch.setattr("app.adapters.extractor.ReplayExtractor.extract", fail)
    monkeypatch.setattr("app.adapters.extractor.extract_bounded", fail)


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    seen: list[dict[str, object]] = []
    real = pipeline.parse_extraction

    def spy(payload: object) -> object:
        assert isinstance(payload, dict)
        seen.append(payload)
        return real(payload)

    monkeypatch.setattr(pipeline, "parse_extraction", spy)
    return seen


def _keys(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(str(key))
            found.extend(_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_keys(item))
    return found


def _assert_payload_is_evidence(payload: dict[str, object]) -> None:
    for key in _keys(payload):
        assert _normalize_key(key) not in FORBIDDEN_KEYS
    call_a = payload["call_a"]
    assert isinstance(call_a, dict)
    lines = call_a["lines"]
    assert isinstance(lines, list) and len(lines) == 1
    line = lines[0]
    assert isinstance(line, dict)
    amount = line["line_amount"]
    passages = json.dumps(call_a["evidence"])
    if amount == "1440.00":
        assert line["tax_charged"] == "0.00"
        assert call_a["ship_to"] == "Seattle, WA"
        assert "Printed location Seattle, WA" in passages
        assert "Printed seat count 10" in passages
        assert "printed_tax_jurisdiction" not in line
    elif amount == "962.32":
        assert line["tax_charged"] == "83.00"
        assert line["printed_tax_jurisdiction"] == "CA"
        assert "Tax jurisdiction printed as CA" in passages
        assert "Printed seat count 4" in passages
    elif amount == "2000.00":
        assert line["tax_charged"] == "0.00"
        assert call_a["ship_to"] is None
        assert "Printed seat count 12" in passages
        assert "Printed location" not in passages
    else:
        raise AssertionError(f"unexpected line amount {amount}")


def _assert_replay(body: dict[str, object]) -> None:
    assert body["llm_mode"] == "replay"
    assert body["replay"] is True
    assert body["extraction_mode"] == "replay"
    assert body["llm_mode"] != "live"
    assert body["extraction_mode"] != "live"
    facts = body["facts"]
    assert isinstance(facts, list)
    for fact in facts:
        assert isinstance(fact, dict)
        assert fact["model_output"] == "replay"
        assert fact["model_output"] != "live"


def _run(client: TestClient, scenario: str, key: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/demo/events",
        json={"scenario": scenario},
        headers=_headers(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert body["llm_mode"] == "replay"
    assert body["replay"] is True
    assert poll_once(client.app.state.ctx) == 1 or body["duplicate"] is True
    return body


def _detail(client: TestClient, purchase_id: object) -> dict[str, object]:
    response = client.get(
        f"/api/v1/purchases/{purchase_id}",
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _fact(detail: dict[str, object], field: str) -> dict[str, object]:
    facts = detail["facts"]
    assert isinstance(facts, list)
    found = [
        fact for fact in facts if isinstance(fact, dict) and fact["field"] == field
    ]
    assert len(found) == 1
    fact = found[0]
    assert isinstance(fact, dict)
    return fact


def _count(client: TestClient, model: type[object]) -> int:
    session = client.app.state.ctx.session_factory()
    try:
        found = session.scalar(select(func.count()).select_from(model))
        return int(found or 0)
    finally:
        session.close()


def test_cases_1_4_6_and_7_stay_labeled_replay(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_recordings(monkeypatch)
    seen = _capture(monkeypatch)

    case1 = _run(client, "case1", "case1")
    detail1 = _detail(client, case1["purchase_id"])
    assert detail1["queue"] == "ready"
    _assert_replay(detail1)
    assert _fact(detail1, "tax_charged")["value"] == "0.00"
    assert _fact(detail1, "user_location")["value"] == "WA"
    assert _fact(detail1, "seat_count")["value"] == "10"

    case4 = _run(client, "case4", "case4")
    detail4 = _detail(client, case4["purchase_id"])
    assert detail4["queue"] == "needs_human_review"
    _assert_replay(detail4)
    determinations = detail4["determinations"]
    assert isinstance(determinations, list) and len(determinations) == 1
    determination = determinations[0]
    assert isinstance(determination, dict)
    assert determination["nhr_reason"] == "RULE_TRANSITION_UNRESOLVED"
    assert determination["engine_amount"] is None
    assert _fact(detail4, "tax_charged_jurisdiction")["value"] == "CA"

    case6 = _run(client, "case6", "case6")
    detail6 = _detail(client, case6["purchase_id"])
    assert detail6["queue"] == "held"
    _assert_replay(detail6)
    assert detail6["determinations"] == []
    holds = detail6["holds"]
    assert isinstance(holds, list) and holds[0]["named_fact"] == "user_location"
    assert _fact(detail6, "seat_count")["value"] == "12"
    assert _fact(detail6, "tax_charged")["value"] == "0.00"

    case7 = _run(client, "case7", "case7")
    assert case7["duplicate"] is True
    assert case7["purchase_id"] == case1["purchase_id"]
    assert case7["llm_mode"] == "replay"
    assert case7["replay"] is True
    listing = client.get("/api/v1/purchases", headers=_headers()).json()
    assert len(listing["purchases"]) == 3
    replayed = _detail(client, case7["purchase_id"])
    _assert_replay(replayed)

    assert len(seen) == 4
    for payload in seen:
        _assert_payload_is_evidence(payload)


@pytest.mark.parametrize("forbidden_key", ["rate", "label"])
def test_forbidden_key_aborts_before_admission(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, forbidden_key: str
) -> None:
    _block_recordings(monkeypatch)
    original = pipeline._printed_extraction

    def with_forbidden_key(scenario: Scenario) -> dict[str, object]:
        payload = original(scenario)
        call_a = payload["call_a"]
        assert isinstance(call_a, dict)
        call_a[forbidden_key] = "0.00"
        return payload

    monkeypatch.setattr(pipeline, "_printed_extraction", with_forbidden_key)

    def writer_ran(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("forbidden payload reached the fact writer")

    monkeypatch.setattr(pipeline, "_write_facts", writer_ran)

    response = client.post(
        "/api/v1/demo/events",
        json={"scenario": "case1"},
        headers=_headers(f"forbid-{forbidden_key}"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["purchase_id"] is None
    assert body["duplicate"] is False
    assert body["llm_mode"] == "replay"
    assert body["replay"] is True
    assert "0.00" not in response.text
    job_id = body["job_id"]
    assert isinstance(job_id, str)

    job = client.get(f"/api/v1/jobs/{job_id}", headers=_headers())
    assert job.status_code == 200, job.text
    job_body = job.json()
    assert job_body["error_code"] == "FORBIDDEN_FIELD"
    assert job_body["status"] == "FAILED"
    assert job_body["purchase_id"] is None
    assert job_body["llm_mode"] == "replay"
    assert job_body["replay"] is True
    assert "0.00" not in job.text
    assert "live" not in job_body["llm_mode"]

    assert poll_once(client.app.state.ctx) == 0
    reread = client.get(f"/api/v1/jobs/{job_id}", headers=_headers())
    assert reread.json()["error_code"] == "FORBIDDEN_FIELD"
    for model in (Purchase, PurchaseLine, Fact, Determination, Hold, Document):
        assert _count(client, model) == 0
