"""WA workpaper: draft with no tax zero, frozen ready export, separate refund list."""

from collections.abc import Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import load_settings
from app.demo.scenarios import get_scenario
from app.main import create_app
from app.models import (
    AuditEvent,
    Fact,
    Purchase,
    PurchaseLine,
    Workpaper,
    WorkpaperLine,
)
from app.worker.poller import poll_once
from tests.api.test_flow import GOOD_ENV, _activate


@dataclass
class Api:
    client: TestClient
    csrf: str | None

    def headers(self, key: str) -> dict[str, str]:
        headers = {"Idempotency-Key": key, "X-Requested-With": "taxleak"}
        if self.csrf is not None:
            headers["X-CSRF-Token"] = self.csrf
        return headers


def _open_api(tmp_path: Path, *, unmatched_ny_rate: bool) -> Iterator[Api]:
    rules = tmp_path / "rules"
    _activate(rules)
    if unmatched_ny_rate:
        rates = rules / "rates.yaml"
        rates.write_text(
            rates.read_text(encoding="utf-8").replace(
                "location: New York City, NY", "location: Unmatched City, NY"
            ),
            encoding="utf-8",
        )
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + (tmp_path / "app.db").resolve().as_posix(),
        rules_dir=str(rules),
        ramp_webhook_secret="test-secret",
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/demo/login", json={}, headers=_headers("login", None)
        )
        assert login.status_code == 200, login.text
        token = login.json().get("csrf_token")
        yield Api(client, token if isinstance(token, str) else None)


def _headers(key: str, csrf: str | None) -> dict[str, str]:
    headers = {"Idempotency-Key": key, "X-Requested-With": "taxleak"}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return headers


@pytest.fixture
def api(tmp_path: Path) -> Iterator[Api]:
    yield from _open_api(tmp_path, unmatched_ny_rate=False)


@pytest.fixture
def api_without_ny_rate(tmp_path: Path) -> Iterator[Api]:
    yield from _open_api(tmp_path, unmatched_ny_rate=True)


def _run(api: Api, scenario: str, key: str) -> dict[str, object]:
    response = api.client.post(
        "/api/v1/demo/events",
        json={"scenario": scenario},
        headers=api.headers(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert poll_once(api.client.app.state.ctx) == 1 or body["duplicate"] is True
    return body


def _detail(api: Api, purchase_id: str, key: str) -> dict[str, object]:
    response = api.client.get(
        f"/api/v1/purchases/{purchase_id}", headers=api.headers(key)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _decide(
    api: Api,
    purchase_id: str,
    key: str,
    *,
    action: str,
    approval_path: str | None = None,
) -> dict[str, object]:
    detail = _detail(api, purchase_id, key + "-read")
    determinations = detail["determinations"]
    assert isinstance(determinations, list) and determinations
    determination = determinations[0]
    assert isinstance(determination, dict)
    body: dict[str, object] = {
        "action": action,
        "reason": "Reviewed the engine result",
        "expected_version": determination["version"],
    }
    if approval_path is not None:
        body["approval_path"] = approval_path
    response = api.client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json=body,
        headers=api.headers(key),
    )
    assert response.status_code == 200, response.text
    return determination


def _create(
    api: Api, key: str, *, jurisdiction: str = "WA", period: str = "2026Q4"
) -> dict[str, object]:
    response = api.client.post(
        "/api/v1/workpapers",
        json={"jurisdiction": jurisdiction, "period": period},
        headers=api.headers(key),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _mark(
    api: Api,
    workpaper_id: str,
    key: str,
    *,
    version: int,
    fingerprint: str | None = None,
) -> Response:
    body: dict[str, object] = {
        "expected_version": version,
        "reason": "Freeze the simulated workpaper",
    }
    if fingerprint is not None:
        body["fingerprint"] = fingerprint
    return api.client.post(
        f"/api/v1/workpapers/{workpaper_id}/mark-ready",
        json=body,
        headers=api.headers(key),
    )


def _export(api: Api, workpaper_id: str, export_format: str, key: str) -> Response:
    return api.client.get(
        f"/api/v1/workpapers/{workpaper_id}/export",
        params={"format": export_format},
        headers=api.headers(key),
    )


def _session(api: Api) -> Session:
    return api.client.app.state.ctx.session_factory()


def _stored(api: Api, workpaper_id: str) -> dict[str, object]:
    session = _session(api)
    try:
        row = session.get(Workpaper, workpaper_id)
        assert row is not None
        lines = session.scalars(
            select(WorkpaperLine).where(WorkpaperLine.workpaper_id == workpaper_id)
        ).all()
        audits = session.scalars(select(AuditEvent.id)).all()
        return {
            "status": row.status,
            "version": row.version,
            "fingerprint": row.fingerprint,
            "frozen_at": row.frozen_at,
            "snapshot": row.snapshot,
            "lines": sorted(line.liability_line_id for line in lines),
            "audits": sorted(audits),
        }
    finally:
        session.close()


def _keys(value: object, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)


def test_empty_draft_shows_counts_and_never_a_tax_zero(api: Api) -> None:
    created = _run(api, "case6", "case6")
    purchase_id = str(created["purchase_id"])
    session = _session(api)
    try:
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        purchase.invoice_number = "=1+1"
        session.commit()
    finally:
        session.close()

    draft = _create(api, "draft")
    assert draft["status"] == "DRAFT"
    assert draft["approved_liabilities_message"] == "No approved liabilities"
    assert draft["approved_liabilities"] == []
    assert draft["held_count"] == 1
    assert draft["open_count"] == 0
    assert draft["excluded"] == [{"invoice": "=1+1", "reason": "HELD"}]
    assert draft["created_after_ready"] is False
    assert (
        "0.00"
        not in api.client.get(
            f"/api/v1/workpapers/{draft['id']}/export",
            params={"format": "json"},
            headers=api.headers("empty-json-scan"),
        ).text
    )
    detail = _detail(api, purchase_id, "case6-detail")
    purchase_amount = detail["purchase_amount"]
    assert isinstance(purchase_amount, dict)
    assert purchase_amount["value"] not in str(draft)

    before = _stored(api, str(draft["id"]))
    refused = _mark(api, str(draft["id"]), "refuse", version=int(draft["version"]))
    assert refused.status_code == 422
    payload = refused.json()
    assert payload["error"]["code"] == "NO_APPROVED_LIABILITY_LINES"
    message = payload["error"]["message"].lower()
    assert "filed" not in message
    assert "paid" not in message
    assert _stored(api, str(draft["id"])) == before

    csv_body = _export(api, str(draft["id"]), "csv", "csv")
    json_body = _export(api, str(draft["id"]), "json", "json")
    assert csv_body.status_code == 200
    assert json_body.status_code == 200
    assert csv_body.headers["content-type"].startswith("text/csv")
    assert json_body.headers["content-type"].startswith("application/json")
    for body in (csv_body.text, json_body.text):
        assert "Synthetic demo data" in body
        assert "DRAFT / SIMULATED" in body
        assert "No approved liabilities" in body
        assert "0.00" not in body
        assert "filed" not in body.lower()
        assert "paid" not in body.lower()
    assert "'=1+1" in csv_body.text
    parsed = json_body.json()
    assert parsed["excluded"] == [{"invoice": "=1+1", "reason": "HELD"}]
    assert "amount" not in parsed["excluded"][0]
    unknown = _export(api, str(draft["id"]), "xlsx", "xlsx")
    missing = api.client.get(
        f"/api/v1/workpapers/{draft['id']}/export",
        headers=api.headers("missing-format"),
    )
    assert unknown.status_code == 422
    assert missing.status_code == 422


def test_ready_export_stays_frozen_when_a_later_approval_opens_a_draft(
    api: Api,
) -> None:
    created = _run(api, "case1", "case1")
    purchase_id = str(created["purchase_id"])
    determination = _decide(
        api, purchase_id, "approve", action="approve", approval_path="LIABILITY"
    )
    engine_amount = determination["engine_amount"]
    assert isinstance(engine_amount, dict)

    draft = _create(api, "draft")
    assert draft["created_after_ready"] is False
    assert draft["approved_liabilities"] == [
        {"invoice": "SYN-1001", "amount": engine_amount}
    ]
    workpaper_id = str(draft["id"])
    version = int(draft["version"])
    fingerprint = str(draft["fingerprint"])

    stale = _mark(api, workpaper_id, "stale-version", version=version + 5)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "STALE_VERSION"
    assert _stored(api, workpaper_id)["status"] == "DRAFT"

    mismatch = _mark(
        api,
        workpaper_id,
        "stale-fingerprint",
        version=version,
        fingerprint="client-supplied",
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "STALE_VERSION"
    assert _stored(api, workpaper_id)["fingerprint"] == fingerprint
    assert _stored(api, workpaper_id)["status"] == "DRAFT"

    ready = _mark(api, workpaper_id, "ready", version=version, fingerprint=fingerprint)
    assert ready.status_code == 200, ready.text
    ready_body = ready.json()
    assert ready_body["id"] == workpaper_id
    assert ready_body["status"] == "READY_TO_FILE_SIMULATED"
    assert ready_body["fingerprint"] == fingerprint
    assert ready_body["created_after_ready"] is False
    assert ready_body["approved_liabilities"][0]["amount"] == engine_amount

    again = _mark(
        api,
        workpaper_id,
        "ready-again",
        version=version,
        fingerprint=fingerprint,
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == workpaper_id

    csv_before = _export(api, workpaper_id, "csv", "csv-before")
    json_before = _export(api, workpaper_id, "json", "json-before")
    assert csv_before.status_code == 200
    assert json_before.status_code == 200
    for body in (csv_before.text, json_before.text):
        assert "Synthetic demo data" in body
        assert "DRAFT / SIMULATED" in body
        assert engine_amount["value"] in body
    parsed = json_before.json()
    assert parsed["approved_liabilities"][0]["amount"] == {
        "value": engine_amount["value"],
        "currency": "USD",
    }
    assert parsed["status"] == "READY_TO_FILE_SIMULATED"
    frozen = _stored(api, workpaper_id)

    detail = _detail(api, purchase_id, "before-fact")
    corrected = api.client.post(
        f"/api/v1/purchases/{purchase_id}/facts",
        json={
            "field": "user_location",
            "value": "Seattle, WA",
            "source": "Seat roster, synthetic",
            "reason": "Confirm the Seattle seats again",
            "expected_version": detail["facts_version"],
        },
        headers=api.headers("fact"),
    )
    assert corrected.status_code == 200, corrected.text
    assert poll_once(api.client.app.state.ctx) == 1
    revised = _decide(
        api, purchase_id, "approve-again", action="approve", approval_path="LIABILITY"
    )
    revised_amount = revised["engine_amount"]
    assert isinstance(revised_amount, dict)

    csv_after = _export(api, workpaper_id, "csv", "csv-after")
    json_after = _export(api, workpaper_id, "json", "json-after")
    assert csv_after.content == csv_before.content
    assert json_after.content == json_before.content
    assert "SUPERSEDED" not in json_before.text
    still = _stored(api, workpaper_id)
    assert still["status"] == "READY_TO_FILE_SIMULATED"
    assert still["lines"] == frozen["lines"]
    assert still["snapshot"] == frozen["snapshot"]

    refused = _mark(
        api,
        workpaper_id,
        "ready-after-change",
        version=version,
        fingerprint=fingerprint,
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "STALE_VERSION"
    still_exported = _export(api, workpaper_id, "json", "json-still")
    assert still_exported.content == json_before.content

    new_draft = _create(api, "next-draft")
    assert new_draft["id"] != workpaper_id
    assert int(new_draft["version"]) > version
    assert new_draft["status"] == "DRAFT"
    assert new_draft["created_after_ready"] is True
    assert new_draft["approved_liabilities"] == [
        {"invoice": "SYN-1001", "amount": revised_amount}
    ]
    superseded = [
        row
        for row in new_draft["excluded"]
        if isinstance(row, dict) and row.get("reason") == "SUPERSEDED"
    ]
    assert superseded
    assert all(set(row) == {"invoice", "reason"} for row in superseded)
    exported = _export(api, str(new_draft["id"]), "json", "new-json")
    assert exported.status_code == 200
    assert exported.json()["created_after_ready"] is True
    assert _stored(api, workpaper_id)["status"] == "READY_TO_FILE_SIMULATED"
    assert _stored(api, workpaper_id)["lines"] == frozen["lines"]


def test_excluded_section_lists_each_reason_without_an_amount(
    api_without_ny_rate: Api,
) -> None:
    api = api_without_ny_rate
    held = _run(api, "case6", "case6")
    nhr = _run(api, "case4", "case4")
    error = _run(api, "case5", "case5")
    rejected = _run(api, "case2", "case2")
    unreviewed = _run(api, "case1", "case1")
    _decide(api, str(rejected["purchase_id"]), "reject", action="reject")

    details = {
        "held": _detail(api, str(held["purchase_id"]), "held"),
        "nhr": _detail(api, str(nhr["purchase_id"]), "nhr"),
        "error": _detail(api, str(error["purchase_id"]), "error"),
        "rejected": _detail(api, str(rejected["purchase_id"]), "rejected"),
        "unreviewed": _detail(api, str(unreviewed["purchase_id"]), "unreviewed"),
    }
    draft = _create(api, "draft")
    assert draft["approved_liabilities_message"] == "No approved liabilities"
    assert draft["approved_liabilities"] == []
    assert draft["held_count"] == 1
    assert draft["open_count"] == 3
    pairs = {
        (row["invoice"], row["reason"])
        for row in draft["excluded"]
        if isinstance(row, dict)
    }
    assert (details["held"]["invoice_number"], "HELD") in pairs
    assert (details["nhr"]["invoice_number"], "NHR") in pairs
    assert (details["error"]["invoice_number"], "ERROR") in pairs
    assert (details["rejected"]["invoice_number"], "REJECTED") in pairs
    assert (details["unreviewed"]["invoice_number"], "UNREVIEWED") in pairs
    assert all(
        set(row) == {"invoice", "reason"}
        for row in draft["excluded"]
        if isinstance(row, dict)
    )
    rendered = str(draft)
    assert "0.00" not in rendered
    purchase_amount = details["held"]["purchase_amount"]
    assert isinstance(purchase_amount, dict)
    assert purchase_amount["value"] not in rendered
    engine_amount = details["unreviewed"]["determinations"][0]["engine_amount"]
    assert isinstance(engine_amount, dict)
    assert engine_amount["value"] not in rendered
    csv_body = _export(api, str(draft["id"]), "csv", "csv").text
    excluded = csv_body.split("section,excluded", maxsplit=1)[1].split(
        "section,not recovered cash", maxsplit=1
    )[0]
    assert "amount" not in excluded
    assert purchase_amount["value"] not in excluded
    assert engine_amount["value"] not in excluded


def test_refunds_stay_off_liabilities_and_charged_correctly_has_no_amount(
    api: Api,
) -> None:
    liability = _run(api, "case1", "case1")
    refund = _run(api, "case3", "case3")
    correct = _run(api, "case2", "case2")
    liability_row = _decide(
        api,
        str(liability["purchase_id"]),
        "approve-liability",
        action="approve",
        approval_path="LIABILITY",
    )
    refund_row = _decide(
        api,
        str(refund["purchase_id"]),
        "approve-refund",
        action="approve",
        approval_path="REFUND_PURSUIT",
    )
    _decide(
        api,
        str(correct["purchase_id"]),
        "approve-correct",
        action="approve",
        approval_path="CHARGED_CORRECTLY",
    )
    liability_amount = liability_row["engine_amount"]
    refund_amount = refund_row["engine_amount"]
    assert isinstance(liability_amount, dict)
    assert isinstance(refund_amount, dict)

    draft = _create(api, "draft")
    assert draft["approved_liabilities"] == [
        {"invoice": "SYN-1001", "amount": liability_amount}
    ]
    invoices = [row["invoice"] for row in draft["approved_liabilities"]]
    assert "SYN-1003" not in invoices
    assert draft["refund_candidates"]["label"] == "not recovered cash"
    assert draft["refund_candidates"]["lines"] == [
        {"invoice": "SYN-1003", "amount": refund_amount}
    ]
    assert draft["charged_correctly_count"] == 1
    exported = _export(api, str(draft["id"]), "json", "json")
    assert exported.status_code == 200
    body = exported.json()
    text_body = exported.text + _export(api, str(draft["id"]), "csv", "csv").text
    assert "not recovered cash" in text_body
    assert "savings" not in text_body.lower()
    found: set[str] = set()
    _keys(body, found)
    assert {"net", "savings", "recovered"}.isdisjoint(found)
    gap = Decimal(str(liability_amount["value"])) - Decimal(str(refund_amount["value"]))
    difference = f"{gap:.2f}"
    assert difference not in {liability_amount["value"], refund_amount["value"]}
    assert difference not in text_body
    charged = text_body.split("section,charged correctly", maxsplit=1)[1]
    assert "count,1" in charged
    assert "amount" not in charged
    assert "0.00" not in text_body


def test_period_follows_invoice_date_not_the_service_period(api: Api) -> None:
    created = _run(api, "case1", "case1")
    purchase_id = str(created["purchase_id"])
    determination = _decide(
        api, purchase_id, "approve", action="approve", approval_path="LIABILITY"
    )
    engine_amount = determination["engine_amount"]
    scenario = get_scenario("case1")
    assert scenario is not None
    session = _session(api)
    try:
        purchase = session.get(Purchase, purchase_id)
        line = session.scalar(
            select(PurchaseLine).where(PurchaseLine.purchase_id == purchase_id)
        )
        assert purchase is not None and line is not None
        assert line.service_period_start == scenario.lines[0].service_start
        assert line.service_period_start == "2026-11-01"
    finally:
        session.close()

    def belongs(day: str, period: str) -> bool:
        session = _session(api)
        try:
            purchase = session.get(Purchase, purchase_id)
            assert purchase is not None
            purchase.invoice_date = day
            session.commit()
        finally:
            session.close()
        draft = _create(api, f"period-{day}-{period}", period=period)
        invoices = [row["invoice"] for row in draft["approved_liabilities"]]
        if "SYN-1001" not in invoices:
            assert draft["approved_liabilities_message"] == "No approved liabilities"
            return False
        assert draft["approved_liabilities"] == [
            {"invoice": "SYN-1001", "amount": engine_amount}
        ]
        return True

    assert belongs("2026-09-30", "2026Q4") is False
    assert belongs("2027-01-01", "2026Q4") is False
    assert belongs("2026-10-01", "2026Q4") is True
    assert belongs("2026-12-31", "2026Q4") is True

    session = _session(api)
    try:
        purchase = session.get(Purchase, purchase_id)
        line = session.scalar(
            select(PurchaseLine).where(PurchaseLine.purchase_id == purchase_id)
        )
        assert purchase is not None and line is not None
        purchase.invoice_date = "2026-11-15"
        line.service_period_start = "2027-01-01"
        line.service_period_end = "2027-01-31"
        for fact in session.scalars(
            select(Fact).where(
                Fact.purchase_id == purchase_id,
                Fact.field_key == "service_period",
                Fact.is_current.is_(True),
            )
        ).all():
            fact.value = "2027-01-01/2027-01-31"
        session.commit()
    finally:
        session.close()
    assert belongs("2026-11-15", "2026Q4") is True
    assert belongs("2026-11-15", "2027Q1") is False


def test_ready_fingerprint_index_ignores_drafts(api: Api) -> None:
    created = _run(api, "case1", "case1")
    _decide(
        api,
        str(created["purchase_id"]),
        "approve",
        action="approve",
        approval_path="LIABILITY",
    )
    draft = _create(api, "draft")
    ready = _mark(
        api,
        str(draft["id"]),
        "ready",
        version=int(draft["version"]),
        fingerprint=str(draft["fingerprint"]),
    )
    assert ready.status_code == 200, ready.text

    session = _session(api)
    try:
        index_sql = session.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE name = 'uq_workpaper_ready_fingerprint'"
            )
        ).scalar_one()
        assert "UNIQUE" in index_sql.upper()
        assert "READY_TO_FILE_SIMULATED" in index_sql
        ready_row = session.get(Workpaper, str(draft["id"]))
        assert ready_row is not None
        session.add(
            Workpaper(
                id=str(uuid4()),
                org_id=ready_row.org_id,
                entity_name=ready_row.entity_name,
                jurisdiction=ready_row.jurisdiction,
                period=ready_row.period,
                version=ready_row.version + 3,
                status="DRAFT",
                fingerprint=ready_row.fingerprint,
                frozen_at=None,
                created_after_ready=True,
                snapshot=None,
                created_at=ready_row.created_at,
            )
        )
        session.flush()
        session.add(
            Workpaper(
                id=str(uuid4()),
                org_id=ready_row.org_id,
                entity_name=ready_row.entity_name,
                jurisdiction=ready_row.jurisdiction,
                period=ready_row.period,
                version=ready_row.version + 4,
                status="READY_TO_FILE_SIMULATED",
                fingerprint=ready_row.fingerprint,
                frozen_at=ready_row.frozen_at,
                created_after_ready=False,
                snapshot=ready_row.snapshot,
                created_at=ready_row.created_at,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
    finally:
        session.rollback()
        session.close()


def test_startup_upgrades_existing_workpaper_table(tmp_path: Path) -> None:
    from app.db import make_engine
    from app.migrations import upgrade_workpapers
    from app.seed import ORG_ID

    database = make_engine("sqlite:///" + (tmp_path / "app.db").resolve().as_posix())
    with database.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE workpapers ("
                "id VARCHAR(36) PRIMARY KEY, org_id VARCHAR(36), "
                "entity_name VARCHAR(200), jurisdiction VARCHAR(8), "
                "period VARCHAR(16), version INTEGER, status VARCHAR(40), "
                "fingerprint VARCHAR(64), frozen_at VARCHAR(32), "
                "created_at VARCHAR(32))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO workpapers ("
                "id, org_id, entity_name, jurisdiction, period, version, "
                "status, fingerprint, frozen_at, created_at"
                ") VALUES ("
                "'legacy-workpaper', :org, 'Legacy Entity', 'CA', '1999Q1', "
                "1, 'DRAFT', NULL, NULL, '2026-01-01T00:00:00Z')"
            ),
            {"org": ORG_ID},
        )
    database.dispose()
    opened = _open_api(tmp_path, unmatched_ny_rate=False)
    try:
        api = next(opened)
        draft = _create(api, "upgraded")
        assert draft["status"] == "DRAFT"
        assert draft["created_after_ready"] is False
        assert draft["approved_liabilities_message"] == "No approved liabilities"
        session = _session(api)
        try:
            legacy = session.get(Workpaper, "legacy-workpaper")
            assert legacy is not None
            assert legacy.created_after_ready is False
            assert legacy.snapshot is None
            columns = {
                column["name"]
                for column in inspect(session.get_bind()).get_columns("workpapers")
            }
            assert "created_after_ready" in columns
            assert "snapshot" in columns
            index_sql = session.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE name = 'uq_workpaper_ready_fingerprint'"
                )
            ).scalar_one()
            assert isinstance(index_sql, str)
            assert "UNIQUE" in index_sql.upper()
            assert "READY_TO_FILE_SIMULATED" in index_sql
        finally:
            session.close()
        upgrade_workpapers(api.client.app.state.ctx.session_factory.kw["bind"])
        session = _session(api)
        try:
            legacy = session.get(Workpaper, "legacy-workpaper")
            assert legacy is not None
            assert legacy.created_after_ready is False
            assert legacy.snapshot is None
        finally:
            session.close()
        assert _create(api, "upgraded-again")["id"] == draft["id"]
    finally:
        opened.close()


def test_refund_approval_after_ready_opens_a_flagged_draft(api: Api) -> None:
    liability = _run(api, "case1", "case1")
    refund = _run(api, "case3", "case3")
    liability_row = _decide(
        api,
        str(liability["purchase_id"]),
        "approve-liability",
        action="approve",
        approval_path="LIABILITY",
    )
    liability_detail = _detail(api, str(liability["purchase_id"]), "liability-invoice")
    refund_detail = _detail(api, str(refund["purchase_id"]), "refund-invoice")
    liability_invoice = str(liability_detail["invoice_number"])
    refund_invoice = str(refund_detail["invoice_number"])
    liability_amount = liability_row["engine_amount"]
    assert isinstance(liability_amount, dict)

    draft = _create(api, "draft")
    assert {
        "invoice": refund_invoice,
        "reason": "UNREVIEWED",
    } in draft["excluded"]
    assert draft["refund_candidates"]["lines"] == []
    workpaper_id = str(draft["id"])
    ready = _mark(
        api,
        workpaper_id,
        "ready",
        version=int(draft["version"]),
        fingerprint=str(draft["fingerprint"]),
    )
    assert ready.status_code == 200, ready.text
    frozen = _export(api, workpaper_id, "json", "json-before")
    assert frozen.status_code == 200
    frozen_body = frozen.json()
    assert frozen_body["refund_candidates"]["lines"] == []
    assert {
        "invoice": refund_invoice,
        "reason": "UNREVIEWED",
    } in frozen_body["excluded"]
    stored = _stored(api, workpaper_id)

    refund_row = _decide(
        api,
        str(refund["purchase_id"]),
        "approve-refund",
        action="approve",
        approval_path="REFUND_PURSUIT",
    )
    refund_amount = refund_row["engine_amount"]
    assert isinstance(refund_amount, dict)
    assert _export(api, workpaper_id, "json", "json-after-approval").content == (
        frozen.content
    )

    opened = _create(api, "next-draft")
    assert opened["id"] != workpaper_id
    assert opened["status"] == "DRAFT"
    assert int(opened["version"]) > int(draft["version"])
    assert opened["created_after_ready"] is True
    assert opened["fingerprint"] != draft["fingerprint"]
    assert opened["approved_liabilities"] == [
        {"invoice": liability_invoice, "amount": liability_amount}
    ]
    assert opened["refund_candidates"]["label"] == "not recovered cash"
    assert opened["refund_candidates"]["lines"] == [
        {"invoice": refund_invoice, "amount": refund_amount}
    ]
    assert all(
        set(row) == {"invoice", "reason"}
        for row in opened["excluded"]
        if isinstance(row, dict)
    )
    assert {"invoice": refund_invoice, "reason": "UNREVIEWED"} not in opened["excluded"]
    found: set[str] = set()
    _keys(opened, found)
    assert {"net", "savings", "recovered"}.isdisjoint(found)
    exported = _export(api, str(opened["id"]), "json", "next-json")
    assert exported.status_code == 200
    assert exported.json()["created_after_ready"] is True
    assert _export(api, workpaper_id, "json", "json-still").content == frozen.content
    assert _stored(api, workpaper_id)["status"] == "READY_TO_FILE_SIMULATED"
    assert _stored(api, workpaper_id)["lines"] == stored["lines"]
    assert _stored(api, workpaper_id)["snapshot"] == stored["snapshot"]
    assert _stored(api, workpaper_id)["fingerprint"] == stored["fingerprint"]


def test_mark_ready_freezes_one_selection(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.domain.commands as commands

    created = _run(api, "case1", "case1")
    _decide(
        api,
        str(created["purchase_id"]),
        "approve",
        action="approve",
        approval_path="LIABILITY",
    )
    draft = _create(api, "draft")
    calls = {"n": 0}
    original = commands._read_snapshot

    def diverge(
        session: Session, org_id: str, jurisdiction: str, period: str
    ) -> commands._Selection:
        calls["n"] += 1
        selection = original(session, org_id, jurisdiction, period)
        if calls["n"] == 1:
            return selection
        extra = commands._Sections(
            message=selection.sections.message,
            liabilities=[*selection.sections.liabilities, ("SYN-RACE", "9.99")],
            held_count=selection.sections.held_count,
            open_count=selection.sections.open_count,
            excluded=selection.sections.excluded,
            refunds=selection.sections.refunds,
            charged_correctly_count=selection.sections.charged_correctly_count,
        )
        return commands._Selection(extra, (*selection.liability_ids, "race-line"))

    monkeypatch.setattr(commands, "_read_snapshot", diverge)
    workpaper_id = str(draft["id"])
    ready = _mark(
        api,
        workpaper_id,
        "ready",
        version=int(draft["version"]),
        fingerprint=str(draft["fingerprint"]),
    )
    assert calls["n"] == 1
    assert ready.status_code == 200, ready.text
    assert "SYN-RACE" not in ready.text
    body = ready.json()
    stored = _stored(api, workpaper_id)
    assert len(body["approved_liabilities"]) == len(stored["lines"])
    assert "race-line" not in stored["lines"]
    monkeypatch.setattr(commands, "_read_snapshot", original)
    again = _mark(
        api,
        workpaper_id,
        "ready-again",
        version=int(draft["version"]),
        fingerprint=str(body["fingerprint"]),
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == workpaper_id
