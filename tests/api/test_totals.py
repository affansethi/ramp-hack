"""GET /totals keeps liabilities and refund candidates apart."""

import json

from fastapi.testclient import TestClient

from tests.api.test_flow import _headers, _run


def test_case_1_liability_total_copies_the_engine_string(
    signed_client: TestClient,
) -> None:
    created = _run(signed_client, "case1", "case1")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    determination = detail["determinations"][0]
    engine = determination["engine_amount"]["value"]
    signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed",
            "expected_version": determination["version"],
        },
        headers=_headers("approve-case1"),
    )
    totals = signed_client.get("/api/v1/totals").json()
    rows = totals["approved_liabilities"]["by_jurisdiction"]
    assert rows == [
        {
            "jurisdiction": "WA",
            "period": "2026Q4",
            "total": {"value": engine, "currency": "USD"},
            "line_count": 1,
        }
    ]
    assert rows[0]["total"]["value"] != "0.00" or engine == "0.00"
    assert totals["refund_candidates"]["count"] == 0
    assert totals["refund_candidates"]["total"] is None
    assert totals["refund_candidates"]["label"] == "not recovered cash"


def test_unreviewed_refund_amount_stays_off_totals(signed_client: TestClient) -> None:
    created = _run(signed_client, "case3", "case3")
    detail = signed_client.get(f"/api/v1/purchases/{created['purchase_id']}").json()
    determination = detail["determinations"][0]
    assert determination["direction"] == "REFUND_CANDIDATE"
    assert determination["engine_amount"] is not None
    totals = signed_client.get("/api/v1/totals").json()
    assert totals["refund_candidates"]["count"] == 0
    assert totals["refund_candidates"]["total"] is None
    assert totals["approved_liabilities"]["message"] == "No approved liabilities"
    assert "0.00" not in json.dumps(totals["approved_liabilities"])
    directions = {
        row["direction"]: row["count"] for row in totals["unreviewed"]["by_direction"]
    }
    assert directions.get("REFUND_CANDIDATE") == 1
    assert "amount" not in totals["unreviewed"]["by_direction"][0]


def test_approved_refund_does_not_reduce_liabilities(
    signed_client: TestClient,
) -> None:
    case1 = _run(signed_client, "case1", "liability")
    liability = signed_client.get(f"/api/v1/purchases/{case1['purchase_id']}").json()[
        "determinations"
    ][0]
    signed_client.post(
        f"/api/v1/determinations/{liability['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed",
            "expected_version": liability["version"],
        },
        headers=_headers("approve-liability"),
    )
    before = signed_client.get("/api/v1/totals").json()
    liability_total = before["approved_liabilities"]["by_jurisdiction"][0]["total"][
        "value"
    ]
    case3 = _run(signed_client, "case3", "refund")
    refund = signed_client.get(f"/api/v1/purchases/{case3['purchase_id']}").json()[
        "determinations"
    ][0]
    reviewed = signed_client.post(
        f"/api/v1/determinations/{refund['id']}/review",
        json={
            "action": "approve",
            "approval_path": "REFUND_PURSUIT",
            "reason": "Pursue",
            "expected_version": refund["version"],
        },
        headers=_headers("approve-refund"),
    )
    assert reviewed.status_code == 200, reviewed.text
    detail = reviewed.json()
    letter_id = detail["determinations"][0]["refund_letter_id"]
    assert letter_id
    letter = signed_client.get(f"/api/v1/drafts/refund-letters/{letter_id}").json()
    assert letter["basis_text"] == ""
    assert letter["status"] == "DRAFT"
    totals = signed_client.get("/api/v1/totals").json()
    assert (
        totals["approved_liabilities"]["by_jurisdiction"][0]["total"]["value"]
        == liability_total
    )
    assert totals["refund_candidates"]["count"] == 1
    refund_total = totals["refund_candidates"]["total"]
    assert isinstance(refund_total, dict)
    assert refund_total["value"] == refund["engine_amount"]["value"]
    assert totals["refund_candidates"]["rows"][0]["refund_letter_id"] == letter_id
    journals = signed_client.get("/api/v1/drafts/journal-entries").json()["drafts"]
    assert all(row["purchase_id"] != case3["purchase_id"] for row in journals)
    blob = json.dumps(totals)
    assert "savings" not in blob
    assert "recovered" not in blob or "not recovered cash" in blob


def test_held_omits_purchase_amount_when_empty(signed_client: TestClient) -> None:
    totals = signed_client.get("/api/v1/totals").json()
    assert totals["held"]["count"] == 0
    assert "purchase_amount" not in totals["held"]
    assert totals["held"]["label"] == "purchase amount, not tax"
    assert totals["open_needs_human_review"] == {"count": 0}
    assert totals["determination_errors"] == {"count": 0}
