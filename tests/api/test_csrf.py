"""Login CSRF token, origin allowlist, and the shared mutation guard."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from starlette.routing import Mount

from app.api.guards import CSRF_HEADER, _equal
from app.api.routes import COOKIE, LoginBody
from app.api.serialize import meta
from app.config import load_settings
from app.db import init_database, make_engine
from app.main import create_app
from app.models import (
    AuditEvent,
    DemoSession,
    Fact,
    IdempotencyKey,
    JournalEntryDraft,
    LiabilityLine,
    Person,
    Purchase,
    RefundCandidate,
    ReviewDecision,
    Workpaper,
)
from app.seed import ORG_ID, PERSON_ID
from app.worker.poller import poll_once
from tests.api.test_contracts import _review_body
from tests.api.test_flow import (  # noqa: F401
    GOOD_ENV,
    ROOT,
    _count,
    _headers,
    _open,
    _run,
    login,
    signed_client,
)

_LEDGER = (
    Purchase,
    ReviewDecision,
    LiabilityLine,
    JournalEntryDraft,
    RefundCandidate,
    Fact,
    AuditEvent,
    IdempotencyKey,
    Workpaper,
)


def _app(database: Path, origin_allowlist: tuple[str, ...] = ()) -> FastAPI:
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(ROOT / "rules"),
        origin_allowlist=origin_allowlist,
    )
    return create_app(settings, start_worker=False)


def _flags(header: str) -> set[str]:
    return {part.strip().split("=", 1)[0].lower() for part in header.split(";")[1:]}


def _has_key(client: TestClient, key: str) -> bool:
    session = _open(client)
    try:
        found = session.scalar(
            select(IdempotencyKey.id).where(IdempotencyKey.key == key)
        )
        return found is not None
    finally:
        session.close()


def _snapshot(client: TestClient) -> dict[type[object], int]:
    return {model: _count(client, model) for model in _LEDGER}


def _forbid(
    client: TestClient, path: str, key: str, body: dict[str, object] | None = None
) -> None:
    kwargs: dict[str, object] = {"headers": _headers(key, csrf=False)}
    if body is not None:
        kwargs["json"] = body
    response = client.post(path, **kwargs)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert "CSRF" in response.json()["error"]["message"]
    assert _has_key(client, key) is False


def _install_probe(app: FastAPI, endpoint: Callable[[], dict[str, bool]]) -> None:
    app.add_api_route(
        "/api/v1/probes/mutation", endpoint, methods=["POST"], include_in_schema=False
    )
    added = app.router.routes.pop()
    for index, route in enumerate(app.router.routes):
        if isinstance(route, Mount):
            app.router.routes.insert(index, added)
            return
    app.router.routes.append(added)


def test_missing_token_writes_no_decision_ledger_or_idempotency(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "case1")
    nhr = _run(signed_client, "case4", "case4")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    nhr_detail = signed_client.get(f"/api/v1/purchases/{nhr['purchase_id']}").json()
    determination = detail["determinations"][0]
    referred = nhr_detail["determinations"][0]
    assert signed_client.get("/api/v1/purchases").status_code == 200
    assert signed_client.get("/api/v1/totals").status_code == 200
    assert signed_client.get("/api/v1/demo/scenarios").status_code == 200
    assert signed_client.get(f"/api/v1/jobs/{created['job_id']}").status_code == 200
    before = _snapshot(signed_client)
    purchase_id = str(created["purchase_id"])
    _forbid(
        signed_client,
        "/api/v1/demo/events",
        "no-token-event",
        {"scenario": "case6"},
    )
    _forbid(
        signed_client,
        f"/api/v1/purchases/{purchase_id}/facts",
        "no-token-fact",
        {
            "field": "user_location",
            "value": "WA",
            "source": "Synthetic location evidence",
            "reason": "Confirm location",
            "expected_version": detail["facts_version"],
        },
    )
    review_body = _review_body(
        int(determination["version"]), "Reviewed the engine result"
    )
    _forbid(
        signed_client,
        f"/api/v1/determinations/{determination['id']}/review",
        "no-token-review",
        review_body,
    )
    _forbid(
        signed_client,
        f"/api/v1/determinations/{referred['id']}/refer",
        "no-token-refer",
        {"action": "leave_open"},
    )
    _forbid(signed_client, "/api/v1/demo/reset", "no-token-reset")
    assert _snapshot(signed_client) == before

    cookie = signed_client.cookies.get(COOKIE)
    assert isinstance(cookie, str)
    posed_as_cookie = signed_client.post(
        "/api/v1/demo/events",
        json={"scenario": "case6"},
        headers={
            "Idempotency-Key": "cookie-as-token",
            "X-Requested-With": "taxleak",
            CSRF_HEADER: cookie,
        },
    )
    assert posed_as_cookie.status_code == 403
    assert _has_key(signed_client, "cookie-as-token") is False
    assert _equal("caf\u00e9", "caf\u00e9") is False
    token = _headers("x")[CSRF_HEADER]
    no_header = signed_client.post(
        "/api/v1/demo/events",
        json={"scenario": "case6"},
        headers={"Idempotency-Key": "no-demo-header", CSRF_HEADER: token},
    )
    assert no_header.status_code == 403
    assert "X-Requested-With" in no_header.json()["error"]["message"]
    assert _snapshot(signed_client) == before

    approved = signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json=review_body,
        headers=_headers("approve-with-token"),
    )
    assert approved.status_code == 200, approved.text
    _forbid(
        signed_client,
        "/api/v1/workpapers",
        "no-token-workpaper",
        {"jurisdiction": "WA", "period": "2026Q4"},
    )
    assert _count(signed_client, Workpaper) == 0
    created_paper = signed_client.post(
        "/api/v1/workpapers",
        json={"jurisdiction": "WA", "period": "2026Q4"},
        headers=_headers("workpaper"),
    )
    assert created_paper.status_code == 200, created_paper.text
    paper_id = created_paper.json()["id"]
    ready_body = {
        "expected_version": created_paper.json()["version"],
        "reason": "Freeze",
    }
    _forbid(
        signed_client,
        f"/api/v1/workpapers/{paper_id}/mark-ready",
        "no-token-ready",
        ready_body,
    )
    session = _open(signed_client)
    try:
        paper = session.get(Workpaper, paper_id)
        assert paper is not None
        assert paper.status == "DRAFT"
        assert paper.frozen_at is None
    finally:
        session.close()
    ready = signed_client.post(
        f"/api/v1/workpapers/{paper_id}/mark-ready",
        json=ready_body,
        headers=_headers("mark-ready"),
    )
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "READY_TO_FILE_SIMULATED"
    purchases = _count(signed_client, Purchase)
    _forbid(signed_client, "/api/v1/demo/reset", "no-token-reset-late")
    assert _count(signed_client, Purchase) == purchases
    assert _has_key(signed_client, "no-token-reset-late") is False


def test_replay_with_the_token_does_not_apply_twice(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "replay-case")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    determination = detail["determinations"][0]
    body = _review_body(int(determination["version"]), "Reviewed the engine result")
    headers = _headers("approve-replay")
    path = f"/api/v1/determinations/{determination['id']}/review"
    first = signed_client.post(path, json=body, headers=headers)
    assert first.status_code == 200, first.text
    second = signed_client.post(path, json=body, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert _count(signed_client, ReviewDecision) == 1
    assert _count(signed_client, LiabilityLine) == 1


def test_added_route_requires_the_token_through_the_shared_guard(
    signed_client: TestClient,  # noqa: F811
) -> None:
    called = {"n": 0}

    def probe() -> dict[str, bool]:
        called["n"] += 1
        return {"ok": True}

    _install_probe(signed_client.app, probe)
    missing = signed_client.post(
        "/api/v1/probes/mutation",
        headers={"Idempotency-Key": "probe", "X-Requested-With": "taxleak"},
    )
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "FORBIDDEN"
    assert called["n"] == 0
    allowed = signed_client.post(
        "/api/v1/probes/mutation",
        headers=_headers("probe-ok"),
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"ok": True}
    assert called["n"] == 1


def test_person_id_and_tax_headers_do_not_change_the_actor(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "actor-case")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    determination = detail["determinations"][0]
    other = "00000000-0000-4000-8000-000000000099"
    headers = _headers("actor-review")
    headers.update(
        {
            "person_id": other,
            "X-Person-Id": other,
            "treatment": "exempt",
            "label": "NEEDS_HUMAN_REVIEW",
            "direction": "REFUND_CANDIDATE",
            "engine_amount": "0.00",
            "expected_tax": "0.00",
            "rate": "0",
            "approval_path": "REFUND_PURSUIT",
        }
    )
    body = _review_body(int(determination["version"]), "Reviewed the engine result")
    path = f"/api/v1/determinations/{determination['id']}/review"
    approved = signed_client.post(path, json=body, headers=headers)
    assert approved.status_code == 200, approved.text
    decision_body = approved.json()["determinations"][0]
    assert decision_body["engine_amount"] == {"value": "151.92", "currency": "USD"}
    assert decision_body["label"] == "ENGINE_SUPPORTED"
    assert decision_body["direction"] == "LIABILITY"
    session = _open(signed_client)
    try:
        decision = session.scalars(select(ReviewDecision)).one()
        assert decision.actor_id == PERSON_ID
        liability = session.scalars(select(LiabilityLine)).one()
        assert liability.approver_id == PERSON_ID
        assert liability.amount == "151.92"
        journal = session.scalars(select(JournalEntryDraft)).one()
        assert journal.amount == "151.92"
    finally:
        session.close()
    refused = signed_client.post(
        path,
        json={**body, "person_id": other},
        headers=_headers("actor-body"),
    )
    assert refused.status_code == 422
    session = _open(signed_client)
    try:
        decision = session.scalars(select(ReviewDecision)).one()
        assert decision.actor_id == PERSON_ID
    finally:
        session.close()


def test_login_cookie_is_httponly_and_secure_follows_scheme(tmp_path: Path) -> None:
    app = _app(tmp_path / "cookie.db")
    with TestClient(app, base_url="http://testserver") as client:
        first = client.post(
            "/api/v1/demo/login", json={}, headers=_headers("login-http")
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert "session_id" not in body
        assert isinstance(body["csrf_token"], str)
        flags = _flags(first.headers["set-cookie"])
        assert "httponly" in flags
        assert "secure" not in flags
        cookie = client.cookies.get(COOKIE)
        assert isinstance(cookie, str) and cookie != body["csrf_token"]
        assert cookie not in first.text
        again = client.post(
            "/api/v1/demo/login", json={}, headers=_headers("login-http")
        )
        assert again.json()["csrf_token"] == body["csrf_token"]
        assert "session_id" not in again.json()
        assert _count(client, DemoSession) == 1
        client.cookies.clear()
        replay = client.post(
            "/api/v1/demo/login", json={}, headers=_headers("login-http")
        )
        assert replay.status_code == 200
        assert replay.json()["csrf_token"] == body["csrf_token"]
        assert client.get("/api/v1/demo/scenarios").status_code == 200

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/demo/login", json={}, headers=_headers("login-https")
        )
        assert response.status_code == 200, response.text
        flags = _flags(response.headers["set-cookie"])
        assert "httponly" in flags
        assert "secure" in flags
        assert "session_id" not in response.json()
        assert client.cookies.get(COOKIE)
        assert client.get("/api/v1/demo/scenarios").status_code == 200


def test_origin_allowlist_is_exact_and_unset_accepts_a_missing_origin(
    tmp_path: Path,
) -> None:
    assert load_settings(GOOD_ENV).origin_allowlist == ()
    assert load_settings({**GOOD_ENV, "ORIGIN_ALLOWLIST": "  "}).origin_allowlist == ()
    listed = load_settings(
        {
            **GOOD_ENV,
            "ORIGIN_ALLOWLIST": "https://taxleak.example, https://preview.example",
        }
    )
    assert listed.origin_allowlist == (
        "https://taxleak.example",
        "https://preview.example",
    )
    with TestClient(_app(tmp_path / "unset.db")) as client:
        login(client, "login-unset")
        headers = _headers("event-foreign")
        headers["Origin"] = "https://anywhere.example"
        allowed = client.post(
            "/api/v1/demo/events", json={"scenario": "case6"}, headers=headers
        )
        assert allowed.status_code == 200, allowed.text

    allowlist = ("https://taxleak.example", "https://preview.example")
    with TestClient(_app(tmp_path / "listed.db", allowlist)) as client:
        missing = client.post(
            "/api/v1/demo/login", json={}, headers=_headers("login-missing")
        )
        assert missing.status_code == 403
        assert _count(client, DemoSession) == 0
        for key, origin in (
            ("login-prefix", "https://taxleak.example.evil"),
            ("login-slash", "https://taxleak.example/"),
        ):
            refused = client.post(
                "/api/v1/demo/login",
                json={},
                headers={**_headers(key), "Origin": origin},
            )
            assert refused.status_code == 403
        assert _count(client, DemoSession) == 0
        ok = client.post(
            "/api/v1/demo/login",
            json={},
            headers={**_headers("login-ok"), "Origin": "https://taxleak.example"},
        )
        assert ok.status_code == 200, ok.text
        token = ok.json()["csrf_token"]
        event_headers = {
            "Idempotency-Key": "event-preview",
            "X-Requested-With": "taxleak",
            CSRF_HEADER: token,
            "Origin": "https://preview.example",
        }
        event = client.post(
            "/api/v1/demo/events", json={"scenario": "case6"}, headers=event_headers
        )
        assert event.status_code == 200, event.text
        assert poll_once(client.app.state.ctx) == 1
        assert poll_once(client.app.state.ctx) == 1
        blocked = client.post(
            "/api/v1/demo/events",
            json={"scenario": "case1"},
            headers={
                "Idempotency-Key": "event-blocked",
                "X-Requested-With": "taxleak",
                CSRF_HEADER: token,
                "Origin": "https://evil.example",
            },
        )
        assert blocked.status_code == 403
        absent = client.post(
            "/api/v1/demo/events",
            json={"scenario": "case1"},
            headers={
                "Idempotency-Key": "event-absent",
                "X-Requested-With": "taxleak",
                CSRF_HEADER: token,
            },
        )
        assert absent.status_code == 403
        assert _count(client, Purchase) == 1
        assert _has_key(client, "event-blocked") is False
        assert _has_key(client, "event-absent") is False


def test_startup_adds_csrf_token_without_rewriting_old_sessions(tmp_path: Path) -> None:
    url = "sqlite:///" + (tmp_path / "old.db").resolve().as_posix()
    engine = make_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sessions (id VARCHAR(36) PRIMARY KEY, "
                "person_id VARCHAR(36), org_id VARCHAR(36), created_at VARCHAR(32))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, person_id, org_id, created_at) "
                "VALUES ('old', 'person', 'org', '2026-01-01T00:00:00Z')"
            )
        )
    engine.dispose()
    factory = init_database(url)
    bind = factory.kw["bind"]
    from app.migrations import upgrade_session_csrf

    upgrade_session_csrf(bind)
    columns = {column["name"] for column in inspect(bind).get_columns("sessions")}
    assert "csrf_token" in columns
    with factory() as session:
        row = session.get(DemoSession, "old")
        assert row is not None
        assert row.csrf_token is None
    bind.dispose()


def test_legacy_login_replay_persists_csrf_and_restores_the_cookie(
    tmp_path: Path,
) -> None:
    """A pre-CSRF Idempotency-Key stored session_id. Replay upgrades that record."""
    app = _app(tmp_path / "legacy-login.db")
    session_id = "00000000-0000-4000-8000-0000000000aa"
    with TestClient(app, base_url="http://testserver") as client:
        db = _open(client)
        try:
            person = db.get(Person, PERSON_ID)
            assert person is not None
            person_name = person.name
            legacy = {
                **meta(client.app.state.settings),
                "person_id": person.id,
                "person_name": person_name,
                "session_id": session_id,
            }
            canonical = LoginBody().model_dump_json()
            db.add(
                DemoSession(
                    id=session_id,
                    person_id=person.id,
                    org_id=ORG_ID,
                    created_at="2026-01-01T00:00:00Z",
                    csrf_token=None,
                )
            )
            db.add(
                IdempotencyKey(
                    id="00000000-0000-4000-8000-0000000000ab",
                    org_id=ORG_ID,
                    key="legacy-login",
                    body_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    status_code=200,
                    response_body=json.dumps(legacy),
                    created_at="2026-01-01T00:00:00Z",
                )
            )
            db.commit()
        finally:
            db.close()

        client.cookies.clear()
        replay = client.post(
            "/api/v1/demo/login",
            json={},
            headers=_headers("legacy-login"),
        )
        assert replay.status_code == 200, replay.text
        body = replay.json()
        assert "session_id" not in body
        assert session_id not in replay.text
        token = body["csrf_token"]
        assert isinstance(token, str) and token
        assert token != session_id
        assert body["person_id"] == PERSON_ID
        assert body["person_name"] == person_name
        flags = _flags(replay.headers["set-cookie"])
        assert "httponly" in flags
        assert "secure" not in flags
        assert client.cookies.get(COOKIE) == session_id
        assert client.get("/api/v1/demo/scenarios").status_code == 200

        db = _open(client)
        try:
            row = db.get(DemoSession, session_id)
            assert row is not None
            assert row.csrf_token == token
            cached = db.scalar(
                select(IdempotencyKey).where(IdempotencyKey.key == "legacy-login")
            )
            assert cached is not None
            saved = json.loads(cached.response_body)
            assert saved == body
            assert "session_id" not in saved
            assert (
                cached.body_hash
                == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            )
        finally:
            db.close()
        assert _count(client, DemoSession) == 1

        client.cookies.clear()
        assert client.get("/api/v1/demo/scenarios").status_code == 401
        again = client.post(
            "/api/v1/demo/login",
            json={},
            headers=_headers("legacy-login"),
        )
        assert again.status_code == 200, again.text
        assert again.json() == body
        assert "session_id" not in again.json()
        again_flags = _flags(again.headers["set-cookie"])
        assert "httponly" in again_flags
        assert "secure" not in again_flags
        assert client.cookies.get(COOKIE) == session_id
        assert client.get("/api/v1/demo/scenarios").status_code == 200
        assert _count(client, DemoSession) == 1

        db = _open(client)
        try:
            row = db.get(DemoSession, session_id)
            assert row is not None
            assert row.csrf_token == token
        finally:
            db.close()


def test_legacy_login_replay_reuses_existing_token_and_sets_secure_on_https(
    tmp_path: Path,
) -> None:
    """A session that already has a token keeps it. HTTPS marks the cookie Secure."""
    app = _app(tmp_path / "legacy-https.db")
    session_id = "00000000-0000-4000-8000-0000000000ac"
    existing = "legacy-csrf-token-value"
    with TestClient(app, base_url="https://testserver") as client:
        db = _open(client)
        try:
            person = db.get(Person, PERSON_ID)
            assert person is not None
            legacy = {
                **meta(client.app.state.settings),
                "person_id": person.id,
                "person_name": person.name,
                "session_id": session_id,
            }
            canonical = LoginBody().model_dump_json()
            db.add(
                DemoSession(
                    id=session_id,
                    person_id=person.id,
                    org_id=ORG_ID,
                    created_at="2026-01-01T00:00:00Z",
                    csrf_token=existing,
                )
            )
            db.add(
                IdempotencyKey(
                    id="00000000-0000-4000-8000-0000000000ad",
                    org_id=ORG_ID,
                    key="legacy-https",
                    body_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    status_code=200,
                    response_body=json.dumps(legacy),
                    created_at="2026-01-01T00:00:00Z",
                )
            )
            db.commit()
        finally:
            db.close()

        client.cookies.clear()
        replay = client.post(
            "/api/v1/demo/login",
            json={},
            headers=_headers("legacy-https"),
        )
        assert replay.status_code == 200, replay.text
        body = replay.json()
        assert body["csrf_token"] == existing
        assert "session_id" not in body
        assert session_id not in replay.text
        flags = _flags(replay.headers["set-cookie"])
        assert "httponly" in flags
        assert "secure" in flags
        assert client.cookies.get(COOKIE) == session_id
        assert client.get("/api/v1/demo/scenarios").status_code == 200
        db = _open(client)
        try:
            row = db.get(DemoSession, session_id)
            assert row is not None
            assert row.csrf_token == existing
            cached = db.scalar(
                select(IdempotencyKey).where(IdempotencyKey.key == "legacy-https")
            )
            assert cached is not None
            saved = json.loads(cached.response_body)
            assert "session_id" not in saved
            assert saved["csrf_token"] == existing
        finally:
            db.close()
        assert _count(client, DemoSession) == 1
