"""Task 7 / D16: failed fetches and ambiguous duplicates stay visible."""

import json

from fastapi.testclient import TestClient

from app.worker.poller import poll_once
from tests.api.test_flow import signed_client as signed_client  # noqa: F401
from tests.api.test_ramp_dedup import _put
from tests.api.test_ramp_dedup import _send as _send_and_poll
from tests.api.test_ramp_fetch import _send


def test_failed_fetch_is_listed_with_a_reason_and_no_amount(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _send(client, "evt-missing", "transactions.receipt_added", "missing-record")
    for _ in range(3):
        poll_once(client.app.state.ctx)
    body = client.get("/api/v1/jobs", params={"status": "FAILED"}).json()
    [failed] = body["jobs"]
    assert failed["stage"] == "fetch"
    assert failed["error_code"] == "FETCH_FAILED"
    assert failed["purchase_id"] is None
    assert failed["ramp_object_id"] == "missing-record"
    assert "no purchase was created" in failed["reason"]
    text = json.dumps(failed)
    assert "amount" not in text and "0.00" not in text
    totals = client.get("/api/v1/totals").json()
    assert totals["approved_liabilities"]["by_jurisdiction"] == []
    assert (
        client.get("/api/v1/jobs", params={"status": "SUCCEEDED"}).json()["jobs"] == []
    )


def test_unknown_job_status_is_refused(signed_client: TestClient) -> None:  # noqa: F811
    response = signed_client.get("/api/v1/jobs", params={"status": "LOST"})
    assert response.status_code == 422


def test_ambiguous_invoices_are_flagged_on_the_review_list(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _put(client, key="amb-1", bill=False, invoice="INV-AMB", date="2026-11-15")
    _send_and_poll(client, "evt-amb-1", "transactions.receipt_added", "amb-1")
    _put(client, key="amb-2", bill=False, invoice="INV-AMB", date="2026-11-16")
    _send_and_poll(client, "evt-amb-2", "transactions.receipt_added", "amb-2")
    rows = client.get("/api/v1/purchases").json()["purchases"]
    assert len(rows) == 2
    first, second = rows
    assert first["possible_duplicate_ids"] == [second["id"]]
    assert second["possible_duplicate_ids"] == [first["id"]]
