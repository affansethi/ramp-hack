"""Every review refusal: decided, path, actor, version, and refer-only-NHR.

Uses the signed temp-rules copy pattern from test_flow (a temp copy of
rules/, never an edit of the repo YAML). Each test gets a fresh database.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import load_settings
from app.main import create_app
from app.models import (
    AuditEvent,
    JournalEntryDraft,
    LiabilityLine,
    Person,
    RefundCandidate,
    RefundLetterDraft,
    ReviewDecision,
)
from app.seed import PERSON_ID
from app.worker.poller import poll_once
from tests.api.test_flow import GOOD_ENV, _activate, _headers, _run, login

_LEDGER = (
    ReviewDecision,
    LiabilityLine,
    RefundCandidate,
    JournalEntryDraft,
    RefundLetterDraft,
)


@pytest.fixture
def refusal_client(tmp_path: Path) -> TestClient:
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


def _open(client: TestClient) -> Session:
    return client.app.state.ctx.session_factory()


def _count(client: TestClient, model: type[object]) -> int:
    session = _open(client)
    try:
        found = session.scalar(select(func.count()).select_from(model))
        return int(found or 0)
    finally:
        session.close()


def _ledger_counts(client: TestClient) -> dict[str, int]:
    return {model.__name__: _count(client, model) for model in _LEDGER}


def _audit_count(client: TestClient) -> int:
    return _count(client, AuditEvent)


def _detail(client: TestClient, purchase_id: object) -> dict[str, object]:
    response = client.get(f"/api/v1/purchases/{purchase_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _first_determination(detail: dict[str, object]) -> dict[str, object]:
    determinations = detail["determinations"]
    assert isinstance(determinations, list) and len(determinations) == 1
    determination = determinations[0]
    assert isinstance(determination, dict)
    return determination


def _totals(client: TestClient) -> dict[str, object]:
    response = client.get("/api/v1/totals")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _review_response(
    client: TestClient,
    determination_id: object,
    key: str,
    *,
    action: str = "approve",
    approval_path: object = "LIABILITY",
    version: object,
    reason: str = "Reviewed the engine result",
):
    return client.post(
        f"/api/v1/determinations/{determination_id}/review",
        json={
            "action": action,
            "approval_path": approval_path,
            "reason": reason,
            "expected_version": version,
        },
        headers=_headers(key),
    )


def _refer_response(
    client: TestClient,
    determination_id: object,
    key: str,
    *,
    action: str = "leave_open",
    version: object,
    note: str = "Advisor follow-up",
):
    return client.post(
        f"/api/v1/determinations/{determination_id}/refer",
        json={"action": action, "note": note, "expected_version": version},
        headers=_headers(key),
    )


def _assert_refusal(response, code: str, status: int) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert body["error"]["code"] == code
    assert "amount" not in json.dumps(body)


def _set_person(
    client: TestClient, *, name: str | None = None, role: str | None = None
) -> None:
    session = _open(client)
    try:
        person = session.get(Person, PERSON_ID)
        assert person is not None
        if name is not None:
            person.name = name
        if role is not None:
            person.role = role
        session.commit()
    finally:
        session.close()


def test_already_decided_refuses_a_second_review(
    refusal_client: TestClient,
) -> None:
    created = _run(refusal_client, "case1", "case1")
    detail = _detail(refusal_client, created["purchase_id"])
    determination = _first_determination(detail)
    first = _review_response(
        refusal_client,
        determination["id"],
        "approve-case1",
        version=determination["version"],
    )
    assert first.status_code == 200, first.text
    totals = _totals(refusal_client)
    _assert_refusal(
        _review_response(
            refusal_client,
            determination["id"],
            "approve-case1-again",
            version=determination["version"],
        ),
        "ALREADY_DECIDED",
        422,
    )
    _assert_refusal(
        _review_response(
            refusal_client,
            determination["id"],
            "reject-case1-after-approve",
            action="reject",
            version=determination["version"],
        ),
        "ALREADY_DECIDED",
        422,
    )
    assert _count(refusal_client, ReviewDecision) == 1
    assert _count(refusal_client, LiabilityLine) == 1
    assert _totals(refusal_client) == totals
    replayed = _review_response(
        refusal_client,
        determination["id"],
        "approve-case1",
        version=determination["version"],
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == first.json()
    assert _count(refusal_client, ReviewDecision) == 1
    assert _count(refusal_client, LiabilityLine) == 1


def test_path_mismatch_refuses_a_wrong_approval_path(
    refusal_client: TestClient,
) -> None:
    created = _run(refusal_client, "case1", "case1")
    detail = _detail(refusal_client, created["purchase_id"])
    determination = _first_determination(detail)
    for index, path in enumerate(["REFUND_PURSUIT", "CHARGED_CORRECTLY", None]):
        _assert_refusal(
            _review_response(
                refusal_client,
                determination["id"],
                f"path-{index}",
                approval_path=path,
                version=determination["version"],
            ),
            "PATH_MISMATCH",
            422,
        )
        assert (
            _first_determination(_detail(refusal_client, created["purchase_id"]))[
                "unit_status"
            ]
            == "READY_FOR_REVIEW"
        )
    assert _ledger_counts(refusal_client) == {
        "ReviewDecision": 0,
        "LiabilityLine": 0,
        "RefundCandidate": 0,
        "JournalEntryDraft": 0,
        "RefundLetterDraft": 0,
    }


def test_unnamed_actor_refuses_review_and_refer(
    refusal_client: TestClient,
) -> None:
    created = _run(refusal_client, "case1", "case1")
    nhr = _run(refusal_client, "case4", "case4")
    detail = _detail(refusal_client, created["purchase_id"])
    determination = _first_determination(detail)
    nhr_determination = _first_determination(
        _detail(refusal_client, nhr["purchase_id"])
    )
    totals = _totals(refusal_client)
    before = _ledger_counts(refusal_client)
    audits = _audit_count(refusal_client)
    _set_person(refusal_client, name="")
    _assert_refusal(
        _review_response(
            refusal_client,
            determination["id"],
            "approve-blank-name",
            version=determination["version"],
        ),
        "FORBIDDEN",
        403,
    )
    _assert_refusal(
        _review_response(
            refusal_client,
            determination["id"],
            "reject-blank-name",
            action="reject",
            version=determination["version"],
        ),
        "FORBIDDEN",
        403,
    )
    _assert_refusal(
        _refer_response(
            refusal_client,
            nhr_determination["id"],
            "refer-blank-name",
            version=nhr_determination["version"],
        ),
        "FORBIDDEN",
        403,
    )
    _set_person(refusal_client, name="Avery Chen", role="viewer")
    _assert_refusal(
        _review_response(
            refusal_client,
            determination["id"],
            "approve-viewer",
            version=determination["version"],
        ),
        "FORBIDDEN",
        403,
    )
    _assert_refusal(
        _review_response(
            refusal_client,
            determination["id"],
            "reject-viewer",
            action="reject",
            version=determination["version"],
        ),
        "FORBIDDEN",
        403,
    )
    _assert_refusal(
        _refer_response(
            refusal_client,
            nhr_determination["id"],
            "refer-viewer",
            version=nhr_determination["version"],
        ),
        "FORBIDDEN",
        403,
    )
    assert _ledger_counts(refusal_client) == before
    assert _audit_count(refusal_client) == audits
    assert _totals(refusal_client) == totals
    _set_person(refusal_client, role="reviewer")


def test_stale_version_refuses_review(refusal_client: TestClient) -> None:
    created = _run(refusal_client, "case1", "case1")
    detail = _detail(refusal_client, created["purchase_id"])
    determination = _first_determination(detail)
    _assert_refusal(
        _review_response(
            refusal_client,
            determination["id"],
            "approve-wrong-version",
            version=int(determination["version"]) + 1,
        ),
        "STALE_VERSION",
        409,
    )
    edited = refusal_client.post(
        f"/api/v1/purchases/{created['purchase_id']}/facts",
        json={
            "field": "user_location",
            "value": "Seattle, WA",
            "source": "seat export",
            "reason": "correct location",
            "expected_version": detail["facts_version"],
        },
        headers=_headers("supersede-fact"),
    )
    assert edited.status_code == 200, edited.text
    assert poll_once(refusal_client.app.state.ctx) == 1
    _assert_refusal(
        _review_response(
            refusal_client,
            determination["id"],
            "approve-superseded",
            version=determination["version"],
        ),
        "STALE_VERSION",
        409,
    )
    assert _count(refusal_client, LiabilityLine) == 0


def test_refer_accepts_only_a_current_nhr_result(
    refusal_client: TestClient,
) -> None:
    supported = _run(refusal_client, "case1", "case1")
    opened = _run(refusal_client, "case4", "case4")
    supported_determination = _first_determination(
        _detail(refusal_client, supported["purchase_id"])
    )
    before = _first_determination(_detail(refusal_client, opened["purchase_id"]))
    watched = {
        key: before[key]
        for key in ("engine_amount", "label", "direction", "engine_treatment")
    }
    left_open = _refer_response(
        refusal_client,
        before["id"],
        "refer-leave-open",
        version=before["version"],
    )
    assert left_open.status_code == 200, left_open.text
    sent = _refer_response(
        refusal_client,
        before["id"],
        "refer-send-to-advisor",
        action="send_to_advisor",
        version=before["version"],
    )
    assert sent.status_code == 200, sent.text
    after = _first_determination(_detail(refusal_client, opened["purchase_id"]))
    assert after["unit_status"] == "SENT_TO_ADVISOR"
    for key, value in watched.items():
        assert after[key] == value
    assert _count(refusal_client, ReviewDecision) == 0
    assert _count(refusal_client, LiabilityLine) == 0
    assert _count(refusal_client, RefundCandidate) == 0
    _assert_refusal(
        _refer_response(
            refusal_client,
            supported_determination["id"],
            "refer-supported",
            version=supported_determination["version"],
        ),
        "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE",
        422,
    )
    _assert_refusal(
        _refer_response(
            refusal_client,
            before["id"],
            "refer-wrong-version",
            version=int(before["version"]) + 1,
        ),
        "STALE_VERSION",
        409,
    )
    _assert_refusal(
        _review_response(
            refusal_client,
            before["id"],
            "approve-nhr",
            version=before["version"],
        ),
        "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE",
        422,
    )
    _assert_refusal(
        _review_response(
            refusal_client,
            before["id"],
            "reject-nhr",
            action="reject",
            version=before["version"],
        ),
        "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE",
        422,
    )
