"""Purchase and workpaper trail reads: past results, facts, answers, decisions."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routes import COOKIE
from app.clock import utcnow
from app.models import (
    DemoSession,
    LiabilityLine,
    Organization,
    Person,
    PurchaseLine,
)
from app.worker.poller import poll_once
from tests.api.test_flow import _headers, _open, _run, signed_client  # noqa: F401


def _detail(client: TestClient, purchase_id: str) -> dict:
    response = client.get(f"/api/v1/purchases/{purchase_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _approve(
    client: TestClient,
    purchase_id: str,
    key: str,
    approval_path: str,
) -> dict:
    detail = _detail(client, purchase_id)
    determination = detail["determinations"][0]
    response = client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": approval_path,
            "reason": "Reviewed the engine result",
            "expected_version": determination["version"],
        },
        headers=_headers(key),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_case3_approval_then_january_edit_keeps_trail(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case3", "case3-trail")
    purchase_id = str(created["purchase_id"])
    approved = _approve(signed_client, purchase_id, "approve-3", "REFUND_PURSUIT")
    current = approved["determinations"][0]
    assert current["direction"] == "REFUND_CANDIDATE"
    assert current["unit_status"] == "APPROVED_FOR_PURSUIT"
    refund_amount = current["engine_amount"]
    assert isinstance(refund_amount, dict)
    assert approved["approver_name"] == "Avery Chen"
    assert isinstance(approved["approved_at"], str)
    assert current["approver_name"] == "Avery Chen"
    assert current["approved_at"] == approved["approved_at"]

    # The January service-period edit versions the facts and supersedes the
    # approved refund candidate; the engine timing column follows the line.
    line_id = approved["lines"][0]["id"]
    edited = signed_client.post(
        f"/api/v1/purchases/{purchase_id}/facts",
        json={
            "field": "service_period",
            "value": "2027-01-01/2027-01-31",
            "source": "January seat export, synthetic",
            "reason": "January service period",
            "expected_version": approved["facts_version"],
            "line_id": line_id,
        },
        headers=_headers("january-edit"),
    )
    assert edited.status_code == 200, edited.text
    session = _open(signed_client)
    try:
        line = session.scalar(
            select(PurchaseLine).where(PurchaseLine.purchase_id == purchase_id)
        )
        assert line is not None
        line.service_period_start = "2027-01-01"
        line.service_period_end = "2027-01-31"
        session.commit()
    finally:
        session.close()
    assert poll_once(signed_client.app.state.ctx) == 1

    detail = _detail(signed_client, purchase_id)
    past = detail["past_determinations"]
    assert isinstance(past, list) and len(past) == 1
    superseded = past[0]
    assert superseded["unit_status"] == "SUPERSEDED"
    assert superseded["direction"] == "REFUND_CANDIDATE"
    assert superseded["engine_amount"] == refund_amount
    assert superseded["rule_reference"] == "CA-RAS-001"
    assert superseded["approver_name"] == "Avery Chen"
    assert isinstance(superseded["approved_at"], str)
    assert superseded["engine_response"]["adjustment_amount"] == refund_amount["value"]

    decisions = detail["decisions"]
    assert isinstance(decisions, list) and len(decisions) == 1
    decision = decisions[0]
    assert decision["determination_id"] == superseded["id"]
    assert decision["action"] == "approve"
    assert decision["approval_path"] == "REFUND_PURSUIT"
    assert decision["actor_id"]
    assert decision["actor_name"] == "Avery Chen"

    periods = [
        fact for fact in detail["fact_history"] if fact["field"] == "service_period"
    ]
    assert len(periods) == 2
    assert periods[0]["value"] == "2026-12-01/2026-12-31"
    assert periods[1]["value"] == "2027-01-01/2027-01-31"
    assert periods[1]["supersedes"] == periods[0]["id"]
    assert periods[0]["origin"] == "AI_EXTRACTED"
    assert periods[1]["origin"] == "HUMAN_SUPPLIED"
    assert all(isinstance(fact["evidence"], list) for fact in periods)

    now = detail["determinations"][0]
    assert now["label"] == "NEEDS_HUMAN_REVIEW"
    assert now["nhr_reason"] == "RULE_TRANSITION_UNRESOLVED"
    assert now["engine_amount"] is None
    assert now["expected_tax"] is None
    assert now["approver_name"] is None
    assert now["approved_at"] is None


def test_rejected_item_shows_reason_and_name_without_amount(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "case1-reject")
    purchase_id = str(created["purchase_id"])
    detail = _detail(signed_client, purchase_id)
    determination = detail["determinations"][0]
    engine_value = determination["engine_amount"]["value"]
    assert engine_value == "151.92"
    refused = signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "reject",
            "reason": "Duplicate charge, not pursuing",
            "expected_version": determination["version"],
        },
        headers=_headers("reject-case1"),
    )
    assert refused.status_code == 200, refused.text

    detail = _detail(signed_client, purchase_id)
    rejected = detail["determinations"][0]
    assert rejected["unit_status"] == "REJECTED"
    assert rejected["engine_amount"] is None
    assert rejected["expected_tax"] is None
    assert rejected["rule_reference"] is None
    assert rejected["rate"] is None
    assert rejected["location_code"] is None
    assert rejected["direction"] is None
    assert rejected["approver_name"] is None
    assert rejected["approved_at"] is None
    # The frozen engine snapshot still carries the computed figure.
    assert rejected["engine_response"]["adjustment_amount"] == engine_value

    decisions = detail["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["action"] == "reject"
    assert decisions[0]["reason"] == "Duplicate charge, not pursuing"
    assert decisions[0]["actor_name"] == "Avery Chen"

    redacted = {
        key: value
        for key, value in detail.items()
        if key not in ("determinations", "past_determinations")
    }
    redacted["determinations"] = [
        {
            key: value
            for key, value in rejected.items()
            if key not in ("engine_request", "engine_response")
        }
    ]
    assert engine_value not in json.dumps(redacted)


def test_workpaper_read_links_lines_to_determinations(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "case1-paper")
    purchase_id = str(created["purchase_id"])
    approved = _approve(signed_client, purchase_id, "approve-paper", "LIABILITY")
    determination_id = str(approved["determinations"][0]["id"])
    engine_amount = approved["determinations"][0]["engine_amount"]
    assert isinstance(engine_amount, dict)

    created_paper = signed_client.post(
        "/api/v1/workpapers",
        json={"jurisdiction": "WA", "period": "2026Q4"},
        headers=_headers("paper"),
    )
    assert created_paper.status_code == 200, created_paper.text
    workpaper_id = str(created_paper.json()["id"])

    read = signed_client.get(f"/api/v1/workpapers/{workpaper_id}")
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["jurisdiction"] == "WA"
    assert body["period"] == "2026Q4"
    lines = body["lines"]
    assert isinstance(lines, list) and len(lines) == 1
    line = lines[0]
    assert line["determination_id"] == determination_id
    assert line["purchase_id"] == purchase_id
    assert line["jurisdiction"] == "WA"
    assert line["period"] == "2026Q4"
    assert line["amount"] == engine_amount
    assert line["approver_name"] == "Avery Chen"
    assert isinstance(line["approved_at"], str)

    session = _open(signed_client)
    try:
        stored = session.scalar(
            select(LiabilityLine).where(
                LiabilityLine.determination_id == determination_id
            )
        )
        assert stored is not None
        assert line["liability_line_id"] == stored.id
        assert line["amount"]["value"] == stored.amount
        assert stored.amount == engine_amount["value"]
    finally:
        session.close()

    versions = body["versions"]
    assert isinstance(versions, list) and len(versions) >= 1
    current = next(row for row in versions if row["id"] == workpaper_id)
    assert set(current) == {"id", "version", "status", "fingerprint", "frozen_at"}
    assert current["version"] == body["version"]
    assert current["status"] == body["status"]
    assert current["fingerprint"] == body["fingerprint"]


def test_cross_org_trail_reads_are_404(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "case1-cross")
    purchase_id = str(created["purchase_id"])
    _approve(signed_client, purchase_id, "approve-cross", "LIABILITY")
    created_paper = signed_client.post(
        "/api/v1/workpapers",
        json={"jurisdiction": "WA", "period": "2026Q4"},
        headers=_headers("paper-cross"),
    )
    assert created_paper.status_code == 200, created_paper.text
    workpaper_id = str(created_paper.json()["id"])

    session = _open(signed_client)
    try:
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
    finally:
        session.close()

    signed_client.cookies.set(COOKIE, "other-session")
    assert signed_client.get(f"/api/v1/purchases/{purchase_id}").status_code == 404
    assert signed_client.get(f"/api/v1/workpapers/{workpaper_id}").status_code == 404
