"""IDs-only intake and durable fetch use synthetic in-process records exclusively."""

import hashlib
import hmac
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Determination, Fact, Hold, Job, Purchase, SourceEvent
from app.worker.poller import poll_once
from tests.api.test_flow import _headers, _run, signed_client  # noqa: F401


def _send(client: TestClient, event: str, kind: str, object_id: str, **data: str):
    raw = json.dumps(
        {"id": event, "type": kind, "data": {"id": object_id, **data}}
    ).encode()
    signature = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/webhooks/ramp",
        content=raw,
        headers={
            "Idempotency-Key": event,
            "X-Ramp-Signature": signature,
            "Content-Type": "application/json",
        },
    )


def _record(client: TestClient, *, bill: bool = False, currency: str = "USD"):
    simulator = client.app.state.ctx.ramp
    records = simulator.bills if bill else simulator.transactions
    records["synthetic-id"] = dict(
        entity_amount="123.450",
        amount="999.99",
        currency_code=currency,
        merchant_name="Synthetic Vendor",
        invoice_number="SYN-25",
        user_transaction_time="2026-11-15T00:00:00Z",
        receipts=["receipt-25"],
        card_holder={"location_name": "Seattle"},
        merchant_location={"state": "WA"},
        merchant_category_code="5734",
        sk_category_name="Software",
    )
    simulator.receipts["receipt-25"] = {
        "ocr": {"taxes": [{"tax_amount": "7.250", "tax_rate": "999"}]}
    }


@pytest.mark.parametrize(
    "kind", ["transactions.receipt_added", "transactions.cleared", "bills.created"]
)
def test_fetch_only_in_worker(
    signed_client: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
):
    client = signed_client
    _record(client, bill=kind == "bills.created")
    ctx = client.app.state.ctx
    fetch = Mock(wraps=ctx.ramp.fetch)
    monkeypatch.setattr(ctx.ramp, "fetch", fetch)
    response = _send(client, "evt-fetch", kind, "synthetic-id")
    assert response.status_code == 200
    assert response.json()["source"] == "simulated Ramp webhook"
    fetch.assert_not_called()
    with ctx.session_factory() as session:
        assert session.scalar(select(Purchase)) is None
        assert session.scalar(select(SourceEvent)) is not None
        job = session.get(Job, response.json()["job_id"])
        assert job.kind == "fetch" and job.purchase_id is None
    assert poll_once(ctx) == 1
    fetch.assert_called_once()
    with ctx.session_factory() as session:
        purchase = session.scalar(select(Purchase))
        assert purchase.ramp_entity_amount == "123.450"
        assert purchase.ramp_ocr_taxes == "7.250"
        assert (purchase.vendor, purchase.invoice_number, purchase.invoice_date) == (
            "Synthetic Vendor",
            "SYN-25",
            "2026-11-15",
        )
        assert purchase.category is None
        assert not session.scalars(
            select(Fact).where(
                Fact.field_key.in_(["user_location", "category", "rate"])
            )
        ).all()
    assert _send(client, "evt-fetch", kind, "synthetic-id").json() == response.json()


@pytest.mark.parametrize("currency", ["EUR", "CAD"])
def test_non_usd_has_no_purchase(
    signed_client: TestClient,  # noqa: F811
    currency: str,
):
    _record(signed_client, currency=currency)
    _send(signed_client, "evt-currency", "transactions.cleared", "synthetic-id")
    ctx = signed_client.app.state.ctx
    assert poll_once(ctx) == 1
    with ctx.session_factory() as session:
        assert session.scalar(select(Purchase)) is None
        assert session.scalar(select(Determination)) is None
        assert session.scalar(select(Hold)) is None
        assert session.scalar(select(Job)).status == "SUCCEEDED"


def test_fetch_failure_exhausts_attempts_without_results(
    signed_client: TestClient,  # noqa: F811
):
    _send(signed_client, "evt-missing", "transactions.receipt_added", "missing")
    ctx = signed_client.app.state.ctx
    for attempt in range(1, 4):
        assert poll_once(ctx) == 0
        with ctx.session_factory() as session:
            job = session.scalar(select(Job))
            assert job.attempts == attempt
            assert job.status == ("FAILED" if attempt == 3 else "QUEUED")
            assert job.error_code == "FETCH_FAILED"
            assert job.purchase_id is None
            for model in (Purchase, Hold, Determination):
                assert session.scalar(select(model)) is None
    assert poll_once(ctx) == 0


@pytest.mark.parametrize(
    "kinds",
    [
        ("transactions.cleared", "transactions.receipt_added"),
        ("transactions.receipt_added", "transactions.cleared"),
    ],
)
def test_transaction_events_share_purchase(
    signed_client: TestClient,  # noqa: F811
    kinds: tuple[str, str],
):
    _record(signed_client)
    ctx = signed_client.app.state.ctx
    ctx.ramp.transactions["synthetic-id"]["receipts"] = [
        "receipt-25",
        "second-receipt",
        "receipt-25",
    ]
    ctx.ramp.receipts["receipt-25"]["ocr"]["taxes"] = [{"tax_amount": "2.00"}]
    ctx.ramp.receipts["second-receipt"] = {"ocr": {"taxes": [{"tax_amount": "3.00"}]}}
    for index, kind in enumerate(kinds):
        data = (
            {"receipt_id": "second-receipt"} if kind.endswith("receipt_added") else {}
        )
        _send(signed_client, f"evt-{index}", kind, "synthetic-id", **data)
        assert poll_once(ctx) == 1
        with ctx.session_factory() as session:
            assert session.scalar(select(Purchase)).ramp_ocr_taxes == "5.00"
        # Same record: no second advance. A changed record queues one recheck.
        assert poll_once(ctx) == (0 if index else 1)
    with ctx.session_factory() as session:
        purchases = session.scalars(select(Purchase)).all()
        assert len(purchases) == 1
        assert purchases[0].facts_version == 1
        assert purchases[0].ramp_entity_amount == "123.450"
        assert {
            event.purchase_id for event in session.scalars(select(SourceEvent))
        } == {purchases[0].id}


def test_amount_in_webhook_is_refused(
    signed_client: TestClient,  # noqa: F811
):
    response = _send(
        signed_client,
        "evt-amount",
        "transactions.cleared",
        "synthetic-id",
        amount="10.00",
    )
    assert response.status_code == 400
    with signed_client.app.state.ctx.session_factory() as session:
        assert session.scalar(select(SourceEvent)) is None
        assert session.scalar(select(Job)) is None


@pytest.mark.parametrize("tax", [None, "0.00"])
def test_receipt_id_preserves_absent_tax_versus_zero(
    signed_client: TestClient,  # noqa: F811
    tax: str | None,
):
    _record(signed_client)
    ctx = signed_client.app.state.ctx
    ctx.ramp.transactions["synthetic-id"]["receipts"] = []
    ctx.ramp.receipts["explicit-receipt"] = {
        "ocr": {"taxes": [] if tax is None else [{"tax_amount": tax}]}
    }
    _send(
        signed_client,
        "evt-receipt",
        "transactions.receipt_added",
        "synthetic-id",
        receipt_id="explicit-receipt",
    )
    assert poll_once(ctx) == 1
    with ctx.session_factory() as session:
        assert session.scalar(select(Purchase)).ramp_ocr_taxes == tax


def test_later_cleared_event_refreshes_entity_amount(
    signed_client: TestClient,  # noqa: F811
) -> None:
    _record(signed_client)
    ctx = signed_client.app.state.ctx
    _send(
        signed_client,
        "evt-receipt-amount",
        "transactions.receipt_added",
        "synthetic-id",
    )
    assert poll_once(ctx) == 1
    assert poll_once(ctx) == 1
    ctx.ramp.transactions["synthetic-id"]["entity_amount"] = "150.00"
    ctx.ramp.transactions["synthetic-id"]["amount"] = "1.00"
    _send(signed_client, "evt-cleared-amount", "transactions.cleared", "synthetic-id")
    assert poll_once(ctx) == 1
    assert poll_once(ctx) == 1
    with ctx.session_factory() as session:
        purchases = session.scalars(select(Purchase)).all()
        assert len(purchases) == 1
        purchase = purchases[0]
        assert purchase.ramp_entity_amount == "150.00"
        assert purchase.facts_version == 2
        amount = session.scalar(
            select(Fact).where(
                Fact.purchase_id == purchase.id,
                Fact.field_key == "line_amount",
                Fact.is_current.is_(True),
            )
        )
        assert amount is not None
        assert amount.value == "123.450"
        assert amount.confirmation == "UNCONFIRMED"
        assert session.scalar(select(Determination)) is None


def test_changed_ocr_supersedes_approval_and_rechecks(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "case1-ocr")
    purchase_id = created["purchase_id"]
    detail = signed_client.get(f"/api/v1/purchases/{purchase_id}").json()
    assert detail["facts_version"] == 1
    determination = detail["determinations"][0]
    approved = signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed the engine result",
            "expected_version": determination["version"],
        },
        headers=_headers("approve-ocr"),
    )
    assert approved.status_code == 200, approved.text
    ctx = signed_client.app.state.ctx
    ctx.ramp.receipts["evt_case1"]["ocr"]["taxes"][0]["tax_amount"] = "10.00"
    cleared = _send(
        signed_client, "evt-cleared-ocr", "transactions.cleared", "evt_case1"
    )
    assert cleared.status_code == 200
    assert cleared.json()["source"] == "simulated Ramp webhook"
    assert poll_once(ctx) == 1
    assert poll_once(ctx) == 1
    detail = signed_client.get(f"/api/v1/purchases/{purchase_id}").json()
    assert detail["facts_version"] == 2
    assert detail["queue"] == "held"
    assert detail["holds"][0]["named_fact"] == "tax_charged"
    assert detail["determinations"] == []
    totals = signed_client.get("/api/v1/totals").json()
    assert totals["approved_liabilities"]["by_jurisdiction"] == []
    assert totals["approved_liabilities"]["message"] == "No approved liabilities"
    assert "151.92" not in json.dumps(totals["approved_liabilities"])
    with ctx.session_factory() as session:
        purchases = session.scalars(select(Purchase)).all()
        assert len(purchases) == 1
        assert purchases[0].id == purchase_id
        assert purchases[0].ramp_ocr_taxes == "10.00"
        assert purchases[0].ramp_entity_amount == "1440.00"
        rows = session.scalars(
            select(Determination).where(Determination.purchase_id == purchase_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].is_current is False
        assert rows[0].unit_status == "SUPERSEDED"
        tax = session.scalar(
            select(Fact).where(
                Fact.purchase_id == purchase_id,
                Fact.field_key == "tax_charged",
                Fact.is_current.is_(True),
            )
        )
        assert tax is not None
        assert tax.value == "0.00"
        assert tax.confirmation == "UNCONFIRMED"


def test_unchanged_later_event_keeps_the_approval(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "case1-same")
    purchase_id = created["purchase_id"]
    detail = signed_client.get(f"/api/v1/purchases/{purchase_id}").json()
    determination = detail["determinations"][0]
    approved = signed_client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed the engine result",
            "expected_version": determination["version"],
        },
        headers=_headers("approve-same"),
    )
    assert approved.status_code == 200, approved.text
    ctx = signed_client.app.state.ctx
    cleared = _send(
        signed_client, "evt-cleared-same", "transactions.cleared", "evt_case1"
    )
    assert cleared.status_code == 200
    assert poll_once(ctx) == 1
    assert poll_once(ctx) == 0
    detail = signed_client.get(f"/api/v1/purchases/{purchase_id}").json()
    assert detail["facts_version"] == 1
    assert detail["queue"] == "decided"
    totals = signed_client.get("/api/v1/totals").json()
    assert totals["approved_liabilities"]["by_jurisdiction"] == [
        {
            "jurisdiction": "WA",
            "period": "2026Q4",
            "total": {"value": "151.92", "currency": "USD"},
            "line_count": 1,
        }
    ]


def test_fetch_recovers_from_persisted_input_after_restart(
    signed_client: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
):
    from app.context import build_context

    ctx = signed_client.app.state.ctx
    response = _send(signed_client, "evt-restart", "transactions.cleared", "evt_case1")
    monkeypatch.setattr(
        ctx.ramp, "fetch", Mock(side_effect=RuntimeError("synthetic failure"))
    )
    assert poll_once(ctx) == 0
    restarted = build_context(ctx.settings)
    assert poll_once(restarted) == 1
    with restarted.session_factory() as session:
        job = session.get(Job, response.json()["job_id"])
        assert job.status == "SUCCEEDED" and job.attempts == 2
        assert job.purchase_id is not None
        assert len(session.scalars(select(Purchase)).all()) == 1
