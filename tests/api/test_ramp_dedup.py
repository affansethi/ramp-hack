"""Exact invoice match merges a card and a bill. Anything else keeps both."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Purchase
from app.worker.poller import poll_once
from tests.api.test_flow import signed_client  # noqa: F401


def _send(client: TestClient, event: str, kind: str, object_id: str) -> None:
    raw = json.dumps({"id": event, "type": kind, "data": {"id": object_id}}).encode()
    signature = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/api/v1/webhooks/ramp",
        content=raw,
        headers={
            "Idempotency-Key": event,
            "X-Ramp-Signature": signature,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    ctx = client.app.state.ctx
    poll_once(ctx)
    poll_once(ctx)


def _put(
    client: TestClient,
    *,
    key: str,
    bill: bool,
    invoice: str = "INV-26",
    amount: str = "50.00",
    date: str = "2026-11-15",
    vendor: str = "Synthetic Vendor",
) -> None:
    simulator = client.app.state.ctx.ramp
    record = dict(
        entity_amount=amount,
        currency_code="USD",
        merchant_name=vendor,
        vendor=vendor,
        invoice_number=invoice,
        invoice_date=date,
        receipts=[],
    )
    if bill:
        simulator.bills[key] = record
    else:
        simulator.transactions[key] = record


def test_card_then_bill_exact_match_is_one_purchase(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _put(client, key="card-26", bill=False)
    _put(client, key="bill-26", bill=True)
    _send(client, "evt-card", "transactions.receipt_added", "card-26")
    _send(client, "evt-bill", "bills.created", "bill-26")
    with client.app.state.ctx.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 1
        purchase = session.scalar(select(Purchase))
        assert purchase is not None
    _send(client, "evt-card", "transactions.receipt_added", "card-26")
    with client.app.state.ctx.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 1
    detail = client.get(f"/api/v1/purchases/{purchase.id}").json()
    assert detail["possible_duplicate_ids"] == []
    assert detail["queue"] != "needs_human_review"


def test_bill_then_card_uses_the_same_rule(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _put(client, key="bill-first", bill=True)
    _put(client, key="card-second", bill=False)
    _send(client, "evt-bill-first", "bills.created", "bill-first")
    _send(client, "evt-card-second", "transactions.cleared", "card-second")
    with client.app.state.ctx.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 1


def test_different_invoice_numbers_stay_two_purchases(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _put(client, key="card-a", bill=False, invoice="INV-A")
    _put(client, key="bill-b", bill=True, invoice="INV-B")
    _send(client, "evt-a", "transactions.receipt_added", "card-a")
    _send(client, "evt-b", "bills.created", "bill-b")
    with client.app.state.ctx.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 2


def test_ambiguous_date_keeps_both_and_names_them(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _put(client, key="card-d", bill=False, date="2026-11-15")
    _put(client, key="bill-d", bill=True, date="2026-11-16")
    _send(client, "evt-d1", "transactions.receipt_added", "card-d")
    _send(client, "evt-d2", "bills.created", "bill-d")
    with client.app.state.ctx.session_factory() as session:
        rows = list(session.scalars(select(Purchase)).all())
        assert len(rows) == 2
        ids = {row.id for row in rows}
    first = client.get(f"/api/v1/purchases/{rows[0].id}").json()
    second = client.get(f"/api/v1/purchases/{rows[1].id}").json()
    assert set(first["possible_duplicate_ids"]) == ids - {rows[0].id}
    assert set(second["possible_duplicate_ids"]) == ids - {rows[1].id}
    assert first["queue"] != "needs_human_review"
    assert second["queue"] != "needs_human_review"


def test_ambiguous_amount_keeps_both(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _put(client, key="card-amt", bill=False, amount="50.00")
    _put(client, key="bill-amt", bill=True, amount="50.01")
    _send(client, "evt-amt1", "transactions.receipt_added", "card-amt")
    _send(client, "evt-amt2", "bills.created", "bill-amt")
    with client.app.state.ctx.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 2
