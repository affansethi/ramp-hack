"""Command audit hashes and permission-scoped, read-only history."""

import json
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import COOKIE
from app.clock import utcnow
from app.domain.commands import _audit
from app.models import AuditEvent, DemoSession, Organization, Person
from app.seed import ORG_ID, PERSON_ID
from tests.api.test_contracts import _client, _review_body
from tests.api.test_flow import _headers, _run, login


def _login(client: TestClient) -> None:
    login(client)


def test_fact_versions_chain_and_review_keeps_version(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        created = _run(client, "case1", "create")
        purchase_id = created["purchase_id"]
        detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
        determination = detail["determinations"][0]
        reviewed = client.post(
            f"/api/v1/determinations/{determination['id']}/review",
            json=_review_body(determination["version"], "Reviewed source"),
            headers=_headers("review"),
        )
        assert reviewed.status_code == 200, reviewed.text
        assert (
            reviewed.json()["determinations"][0]["version"] == determination["version"]
        )
        for index in range(2):
            edited = client.post(
                f"/api/v1/purchases/{purchase_id}/facts",
                json={
                    "field": "user_location",
                    "value": "WA",
                    "source": "Synthetic location evidence",
                    "reason": "Confirm location",
                    "expected_version": detail["facts_version"],
                },
                headers=_headers(f"edit-{index}"),
            )
            assert edited.status_code == 200, edited.text
            updated = edited.json()
            assert updated["facts_version"] == detail["facts_version"] + 1
            detail = updated
        rows = [
            row
            for row in client.get(
                "/api/v1/audit-events", params={"object_id": purchase_id}
            ).json()["audit_events"]
            if row["actor_id"] != "system"
        ]
        assert len(rows) == 2
        assert rows[1]["before_hash"] == rows[0]["after_hash"]
        assert rows[1]["after_hash"] != rows[0]["after_hash"]
        for row in rows:
            identity = {
                key: row[key] for key in ("object_type", "object_id", "object_version")
            }
            expected = sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            assert row["after_hash"] == expected


def test_hash_identity_and_pending_writes_are_object_and_org_scoped(
    tmp_path: Path,
) -> None:
    with (
        _client(tmp_path) as client,
        client.app.state.ctx.session_factory() as session,
    ):
        for org, kind, obj, version in (
            (ORG_ID, "purchase", "object", 1),
            (ORG_ID, "purchase", "object", 1),
            (ORG_ID, "purchase", "object", 2),
            (ORG_ID, "determination", "object", 1),
            (ORG_ID, "purchase", "other", 1),
            ("other-org", "purchase", "object", 1),
        ):
            _audit(session, org, PERSON_ID, "test", kind, obj, version)
        session.flush()
        rows = list(
            session.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )
        assert rows[0].before_hash == ""
        assert rows[1].before_hash == rows[0].after_hash
        assert rows[1].after_hash == rows[0].after_hash
        assert rows[2].before_hash == rows[1].after_hash
        assert rows[2].after_hash != rows[1].after_hash
        assert all(row.before_hash == "" for row in rows[3:])
        assert rows[3].after_hash != rows[0].after_hash
        assert rows[4].after_hash != rows[0].after_hash
        assert rows[5].after_hash == rows[0].after_hash


def test_legacy_hashes_are_not_backfilled(tmp_path: Path) -> None:
    with (
        _client(tmp_path) as client,
        client.app.state.ctx.session_factory() as session,
    ):
        legacy = AuditEvent(
            id="legacy",
            org_id=ORG_ID,
            actor_id=PERSON_ID,
            command="legacy",
            object_type="purchase",
            object_id="object",
            object_version=1,
            created_at="2099-01-01T00:00:00Z",
        )
        session.add(legacy)
        _audit(session, ORG_ID, PERSON_ID, "test", "purchase", "object", 2)
        _audit(session, ORG_ID, PERSON_ID, "test", "purchase", "object", 3)
        session.flush()
        rows = list(
            session.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )
        assert rows[0].id == legacy.id
        assert legacy.before_hash is None and legacy.after_hash is None
        assert rows[1].before_hash == ""
        assert rows[2].before_hash == rows[1].after_hash


def test_pagination_legacy_rows_and_cross_org_denial(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/api/v1/audit-events").status_code == 401
        _login(client)
        with client.app.state.ctx.session_factory() as session:
            # Equal timestamps exercise the id tie-breaker at the page boundary.
            timestamp = utcnow()
            for index in range(205):
                session.add(
                    AuditEvent(
                        id=f"event-{index:03}",
                        org_id=ORG_ID,
                        actor_id=PERSON_ID,
                        command="legacy",
                        object_type="purchase",
                        object_id="object",
                        object_version=1,
                        created_at=timestamp,
                    )
                )
            session.add(Organization(id="other-org", name="Synthetic other org"))
            session.add(
                Person(
                    id="other-person",
                    org_id="other-org",
                    name="Synthetic viewer",
                    role="viewer",
                )
            )
            session.flush()
            session.add(
                DemoSession(
                    id="other-session",
                    person_id="other-person",
                    org_id="other-org",
                    created_at=utcnow(),
                    csrf_token="other-session-csrf",
                )
            )
            session.commit()
        ids = []
        cursor = None
        for count in (100, 100, 5):
            params = {"object_id": "object"}
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get("/api/v1/audit-events", params=params)
            assert response.status_code == 200
            body = response.json()
            rows = body["audit_events"]
            assert len(rows) == count
            assert all(row["after_hash"] is None for row in rows)
            ids.extend(row["id"] for row in rows)
            cursor = body["next_cursor"]
        assert cursor is None
        assert ids == [f"event-{index:03}" for index in range(205)]
        assert (
            client.get(
                "/api/v1/audit-events",
                params={"object_id": "missing", "cursor": ids[0]},
            ).status_code
            == 404
        )
        client.cookies.clear()
        client.cookies.set(COOKIE, "other-session")
        for params in ({}, {"object_id": "object"}):
            assert (
                client.get("/api/v1/audit-events", params=params).json()["audit_events"]
                == []
            )
        assert (
            client.get(
                "/api/v1/audit-events", params={"object_id": "object", "cursor": ids[0]}
            ).status_code
            == 404
        )


def test_audit_route_rejects_mutations(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        for method in ("POST", "PATCH", "DELETE"):
            response = client.request(
                method, "/api/v1/audit-events", json={}, headers=_headers(method)
            )
            assert response.status_code == 405
        assert client.get("/api/v1/audit-events").json()["audit_events"] == []


def test_startup_upgrades_existing_audit_table(tmp_path: Path) -> None:
    from sqlalchemy import inspect, text

    from app.db import make_engine

    database = make_engine(f"sqlite:///{tmp_path / 'contracts.db'}")
    with database.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE audit_events (id VARCHAR(36) PRIMARY KEY, "
                "org_id VARCHAR(36), actor_id VARCHAR(36), command VARCHAR(80), "
                "object_type VARCHAR(80), object_id VARCHAR(36), "
                "object_version INTEGER, created_at VARCHAR(32))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO audit_events VALUES "
                "('legacy', :org, :actor, 'legacy', 'purchase', 'object', 1, "
                "'2026-01-01T00:00:00Z')"
            ),
            {"org": ORG_ID, "actor": PERSON_ID},
        )
    database.dispose()
    with _client(tmp_path) as client:
        _login(client)
        rows = client.get("/api/v1/audit-events").json()["audit_events"]
        assert len(rows) == 1
        assert rows[0]["before_hash"] is None
        assert rows[0]["after_hash"] is None
        factory = client.app.state.ctx.session_factory
        with factory() as session:
            columns = {
                column["name"]: column
                for column in inspect(session.get_bind()).get_columns("audit_events")
            }
            assert columns["before_hash"]["nullable"]
            assert columns["after_hash"]["nullable"]
            _audit(session, ORG_ID, PERSON_ID, "test", "purchase", "object", 2)
            session.commit()
        # Running the upgrade again is harmless and preserves the new history.
        from app.migrations import upgrade_audit_hashes

        upgrade_audit_hashes(factory.kw["bind"])
        rows = client.get("/api/v1/audit-events").json()["audit_events"]
        assert len(rows) == 2
        assert rows[0]["after_hash"] is None
        assert rows[1]["before_hash"] == ""
        assert rows[1]["after_hash"]


def test_concurrent_first_referrals_chain_through_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import app.domain.commands as commands

    with _client(tmp_path) as client:
        _login(client)
        created = _run(client, "case4", "create-concurrent")
        detail = client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
        determination = detail["determinations"][0]
        assert determination["label"] == "NEEDS_HUMAN_REVIEW"
        barrier = Barrier(2)
        original = commands._audit

        def overlapping_audit(*args: object, **kwargs: object) -> None:
            barrier.wait(timeout=10)
            original(*args, **kwargs)

        monkeypatch.setattr(commands, "_audit", overlapping_audit)

        def refer(index: int) -> int:
            response = client.post(
                f"/api/v1/determinations/{determination['id']}/refer",
                json={"action": "leave_open"},
                headers=_headers(f"concurrent-refer-{index}"),
            )
            assert response.status_code == 200, response.text
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(refer, range(2))) == [200, 200]
        rows = client.get(
            "/api/v1/audit-events", params={"object_id": determination["id"]}
        ).json()["audit_events"]
        assert len(rows) == 2
        assert rows[0]["before_hash"] == ""
        assert rows[1]["before_hash"] == rows[0]["after_hash"]
        assert rows[0]["after_hash"] == rows[1]["after_hash"]
