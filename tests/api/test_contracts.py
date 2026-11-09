"""Idempotency, stale version, org scope, and the demo header (#12)."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import load_settings
from app.main import create_app
from app.models import Determination, Organization, Purchase, ReviewDecision
from tests.api.test_flow import GOOD_ENV, _activate, _headers, _run


def _client(tmp_path: Path) -> TestClient:
    rules = tmp_path / "rules"
    _activate(rules)
    database = tmp_path / "contracts.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(rules),
        ramp_webhook_secret="test-secret",
    )
    return TestClient(create_app(settings, start_worker=False))


def _count(session: Session, model: type) -> int:
    found = session.scalar(select(func.count()).select_from(model))
    return int(found or 0)


def _review_body(version: int, reason: str) -> dict[str, object]:
    return {
        "action": "approve",
        "approval_path": "LIABILITY",
        "reason": reason,
        "expected_version": version,
    }


def test_reads_skip_the_demo_header_and_mutations_write_nothing_without_it(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        login = client.post(
            "/api/v1/demo/login", json={}, headers=_headers("login-reads")
        )
        assert login.status_code == 200
        created = _run(client, "case1", "case1-reads")
        purchase_id = created["purchase_id"]
        detail = client.get(f"/api/v1/purchases/{purchase_id}")
        listing = client.get("/api/v1/purchases")
        assert detail.status_code == 200
        assert detail.json()["queue"] == "ready"
        assert listing.status_code == 200
        assert len(listing.json()["purchases"]) == 1
        refused = client.post(
            "/api/v1/demo/events",
            json={"scenario": "case6"},
            headers={"Idempotency-Key": "no-header"},
        )
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "FORBIDDEN"
        session = client.app.state.ctx.session_factory()
        try:
            assert _count(session, Purchase) == 1
            assert _count(session, ReviewDecision) == 0
        finally:
            session.close()


def test_same_review_key_replays_and_a_different_body_conflicts(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert (
            client.post(
                "/api/v1/demo/login", json={}, headers=_headers("login-idem")
            ).status_code
            == 200
        )
        created = _run(client, "case1", "case1-idem")
        detail = client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
        determination = detail["determinations"][0]
        body = _review_body(int(determination["version"]), "Reviewed the engine result")
        headers = _headers("approve-once")
        path = f"/api/v1/determinations/{determination['id']}/review"
        first = client.post(path, json=body, headers=headers)
        assert first.status_code == 200, first.text
        second = client.post(path, json=body, headers=headers)
        assert second.status_code == 200, second.text
        assert second.json() == first.json()
        session = client.app.state.ctx.session_factory()
        try:
            assert _count(session, ReviewDecision) == 1
        finally:
            session.close()
        conflict = client.post(
            path,
            json=_review_body(int(determination["version"]), "A different reason"),
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        session = client.app.state.ctx.session_factory()
        try:
            assert _count(session, ReviewDecision) == 1
        finally:
            session.close()


def test_stale_expected_version_writes_nothing(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert (
            client.post(
                "/api/v1/demo/login", json={}, headers=_headers("login-stale")
            ).status_code
            == 200
        )
        created = _run(client, "case1", "case1-stale")
        detail = client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
        determination = detail["determinations"][0]
        stale = client.post(
            f"/api/v1/determinations/{determination['id']}/review",
            json=_review_body(int(determination["version"]) + 1, "stale"),
            headers=_headers("stale-review"),
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "STALE_VERSION"
        session = client.app.state.ctx.session_factory()
        try:
            assert _count(session, ReviewDecision) == 0
            row = session.get(Determination, determination["id"])
            assert row is not None
            assert row.unit_status == "READY_FOR_REVIEW"
            assert row.version == determination["version"]
        finally:
            session.close()


def test_other_org_determination_is_not_found(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert (
            client.post(
                "/api/v1/demo/login", json={}, headers=_headers("login-org")
            ).status_code
            == 200
        )
        determination_id = _insert_other_org(client)
        refused = client.post(
            f"/api/v1/determinations/{determination_id}/review",
            json=_review_body(1, "not our org"),
            headers=_headers("other-org"),
        )
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "NOT_FOUND"
        session = client.app.state.ctx.session_factory()
        try:
            assert _count(session, ReviewDecision) == 0
            row = session.get(Determination, determination_id)
            assert row is not None
            assert row.unit_status == "READY_FOR_REVIEW"
        finally:
            session.close()


def _insert_other_org(client: TestClient) -> str:
    session = client.app.state.ctx.session_factory()
    try:
        org_id = "00000000-0000-4000-8000-000000000099"
        purchase_id = str(uuid4())
        determination_id = str(uuid4())
        session.add(Organization(id=org_id, name="Other Synthetic Org"))
        session.add(
            Purchase(
                id=purchase_id,
                org_id=org_id,
                vendor="Other Vendor",
                invoice_number="INV-OTHER",
                invoice_date="2026-11-01",
                entity_name="Other Synthetic",
                currency="USD",
                item_status="OPEN",
                extraction_mode="synthetic",
                facts_version=1,
                created_at=utcnow(),
            )
        )
        session.add(
            Determination(
                id=determination_id,
                org_id=org_id,
                purchase_id=purchase_id,
                jurisdiction="WA",
                facts_version=1,
                ruleset_version="test",
                label="ENGINE_SUPPORTED",
                direction="LIABILITY",
                derived_direction=False,
                unit_status="READY_FOR_REVIEW",
                version=1,
                is_current=True,
                engine_amount="1.00",
            )
        )
        session.commit()
        return determination_id
    finally:
        session.close()
