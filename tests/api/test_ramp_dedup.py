"""Exact invoice match merges a card and a bill. Anything else keeps both."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import Determination, Purchase, PurchaseLine, SourceEvent
from app.worker.poller import poll_once
from tests.api.test_flow import _headers, _run, signed_client  # noqa: F401


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
    tax: str | None = None,
) -> None:
    simulator = client.app.state.ctx.ramp
    record = dict(
        entity_amount=amount,
        currency_code="USD",
        merchant_name=vendor,
        vendor=vendor,
        invoice_number=invoice,
        invoice_date=date,
        receipts=[key] if tax is not None else [],
    )
    if tax is not None:
        simulator.receipts[key] = {"ocr": {"taxes": [{"tax_amount": tax}]}}
    if bill:
        simulator.bills[key] = record
    else:
        simulator.transactions[key] = record


def _approve(client: TestClient, purchase_id: str) -> str:
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    determination = detail["determinations"][0]
    approved = client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed the engine result",
            "expected_version": determination["version"],
        },
        headers=_headers(f"approve-{purchase_id}"),
    )
    assert approved.status_code == 200, approved.text
    totals = client.get("/api/v1/totals").json()
    assert totals["approved_liabilities"]["by_jurisdiction"] == [
        {
            "jurisdiction": "WA",
            "period": "2026Q4",
            "total": {"value": "151.92", "currency": "USD"},
            "line_count": 1,
        }
    ]
    return str(determination["id"])


def _after_hash(event: dict[str, object]) -> str:
    identity = {
        "object_type": event["object_type"],
        "object_id": event["object_id"],
        "object_version": event["object_version"],
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


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


def test_blank_or_invalid_dates_stay_two_and_are_named(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    dates = ("", "   ", "not-a-date", "2026-02-31")
    for index, invoice_date in enumerate(dates):
        invoice = f"INV-DATE-{index}"
        _put(
            client,
            key=f"card-date-{index}",
            bill=False,
            invoice=invoice,
            date=invoice_date,
        )
        _put(
            client,
            key=f"bill-date-{index}",
            bill=True,
            invoice=invoice,
            date=invoice_date,
        )
        _send(
            client,
            f"evt-date-card-{index}",
            "transactions.receipt_added",
            f"card-date-{index}",
        )
        _send(client, f"evt-date-bill-{index}", "bills.created", f"bill-date-{index}")
    _put(client, key="card-mix", bill=False, invoice="INV-MIX", date="2026-11-15")
    _put(client, key="bill-mix", bill=True, invoice="INV-MIX", date="")
    _send(client, "evt-mix-card", "transactions.receipt_added", "card-mix")
    _send(client, "evt-mix-bill", "bills.created", "bill-mix")
    with client.app.state.ctx.session_factory() as session:
        rows = list(session.scalars(select(Purchase)).all())
        assert len(rows) == len(dates) * 2 + 2
    for row in rows:
        detail = client.get(f"/api/v1/purchases/{row.id}").json()
        assert detail["queue"] != "needs_human_review"
        assert detail["possible_duplicate_ids"]
        other = client.get(
            f"/api/v1/purchases/{detail['possible_duplicate_ids'][0]}"
        ).json()
        assert row.id in other["possible_duplicate_ids"]


def test_later_bill_ocr_supersedes_an_approved_card(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    created = _run(client, "case1", "case1-bill-ocr")
    purchase_id = str(created["purchase_id"])
    determination_id = _approve(client, purchase_id)
    _put(
        client,
        key="bill-ocr",
        bill=True,
        invoice="SYN-1001",
        amount="1440.00",
        date="2026-11-15",
        vendor="Synthetic Cloud Co",
        tax="1.00",
    )
    _send(client, "evt-bill-ocr", "bills.created", "bill-ocr")
    _assert_receipt_invalidated(client, purchase_id, determination_id)
    with client.app.state.ctx.session_factory() as session:
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        assert purchase.ramp_transaction_id == "evt_case1"
        lines = session.scalars(
            select(PurchaseLine).where(PurchaseLine.purchase_id == purchase_id)
        ).all()
        assert [line.description for line in lines] == ["Remote-access seats, November"]
        linked = {
            event.external_event_id
            for event in session.scalars(
                select(SourceEvent).where(SourceEvent.purchase_id == purchase_id)
            )
        }
        assert linked == {"evt_case1", "evt-bill-ocr"}
    events = client.get(
        "/api/v1/audit-events", params={"object_id": purchase_id}
    ).json()["audit_events"]
    dedup = [event for event in events if event["command"] == "deduplicate"]
    # Version 1: the new canonical purchase. Version 2: the merged bill.
    assert [event["object_version"] for event in dedup] == [1, 2]
    dedup = dedup[1:]
    assert dedup[0]["after_hash"] == _after_hash(dedup[0])
    _send(client, "evt-bill-ocr", "bills.created", "bill-ocr")
    with client.app.state.ctx.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 1
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        assert purchase.facts_version == 2
        assert (
            len(
                session.scalars(
                    select(PurchaseLine).where(PurchaseLine.purchase_id == purchase_id)
                ).all()
            )
            == 1
        )


def test_same_ocr_on_an_exact_bill_keeps_the_approval(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    created = _run(client, "case1", "case1-same-ocr")
    purchase_id = str(created["purchase_id"])
    determination_id = _approve(client, purchase_id)
    _put(
        client,
        key="bill-same",
        bill=True,
        invoice="SYN-1001",
        amount="1440.00",
        date="2026-11-15",
        vendor="Synthetic Cloud Co",
        tax="0.00",
    )
    _send(client, "evt-bill-same", "bills.created", "bill-same")
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    assert detail["facts_version"] == 1
    assert detail["determinations"][0]["id"] == determination_id
    assert detail["determinations"][0]["unit_status"] == "APPROVED"
    assert detail["possible_duplicate_ids"] == []
    totals = client.get("/api/v1/totals").json()
    assert (
        totals["approved_liabilities"]["by_jurisdiction"][0]["total"]["value"]
        == "151.92"
    )
    with client.app.state.ctx.session_factory() as session:
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        assert purchase.ramp_ocr_taxes == "0.00"
        assert session.scalar(select(func.count()).select_from(Purchase)) == 1
        assert (
            len(
                session.scalars(
                    select(PurchaseLine).where(PurchaseLine.purchase_id == purchase_id)
                ).all()
            )
            == 1
        )


def test_bill_then_card_keeps_the_later_receipt(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _put(
        client,
        key="bill-first-ocr",
        bill=True,
        invoice="SYN-1001",
        amount="1440.00",
        date="2026-11-15",
        vendor="Synthetic Cloud Co",
        tax="0.00",
    )
    _send(client, "evt-bill-first-ocr", "bills.created", "bill-first-ocr")
    client.app.state.ctx.ramp.receipts["evt_case1"]["ocr"] = {
        "taxes": [{"tax_amount": "1.00"}]
    }
    _send(client, "evt-card-later", "transactions.receipt_added", "evt_case1")
    with client.app.state.ctx.session_factory() as session:
        rows = list(session.scalars(select(Purchase)).all())
        assert len(rows) == 1
        purchase = rows[0]
        assert purchase.ramp_ocr_taxes == "1.00"
        assert purchase.ramp_transaction_id == "bill:bill-first-ocr"
        assert purchase.facts_version == 2
        lines = session.scalars(
            select(PurchaseLine).where(PurchaseLine.purchase_id == purchase.id)
        ).all()
        assert [line.description for line in lines] == ["Simulated Ramp record"]
        linked = {
            event.external_event_id
            for event in session.scalars(
                select(SourceEvent).where(SourceEvent.purchase_id == purchase.id)
            )
        }
        assert linked == {"evt-bill-first-ocr", "evt-card-later"}
    detail = client.get(f"/api/v1/purchases/{purchase.id}").json()
    assert detail["possible_duplicate_ids"] == []
    assert detail["queue"] != "needs_human_review"


def test_same_transaction_receipt_change_supersedes_approval(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    created = _run(client, "case1", "case1-receipt")
    purchase_id = str(created["purchase_id"])
    determination_id = _approve(client, purchase_id)
    client.app.state.ctx.ramp.receipts["evt_case1"]["ocr"] = {
        "taxes": [{"tax_amount": "1.00"}]
    }
    _send(client, "evt-cleared-ocr", "transactions.cleared", "evt_case1")
    _assert_receipt_invalidated(client, purchase_id, determination_id)
    with client.app.state.ctx.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Purchase)) == 1
        lines = session.scalars(
            select(PurchaseLine).where(PurchaseLine.purchase_id == purchase_id)
        ).all()
        assert len(lines) == 1
        linked = {
            event.external_event_id
            for event in session.scalars(
                select(SourceEvent).where(SourceEvent.purchase_id == purchase_id)
            )
        }
        assert linked == {"evt_case1", "evt-cleared-ocr"}


def test_ambiguous_invoices_keep_one_audit_chain(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _put(client, key="chain-1", bill=False, invoice="INV-CHAIN", date="2026-11-15")
    _send(client, "evt-chain-1", "transactions.receipt_added", "chain-1")
    with client.app.state.ctx.session_factory() as session:
        first = session.scalar(select(Purchase))
        assert first is not None
        first_id = first.id
    edited = client.post(
        f"/api/v1/purchases/{first_id}/facts",
        json={
            "field": "vendor_name",
            "value": "Synthetic Vendor",
            "source": "Synthetic invoice",
            "reason": "Version the purchase before the next invoice",
            "expected_version": 1,
        },
        headers=_headers("chain-fact"),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["facts_version"] == 2
    for index, invoice_date in enumerate(("2026-11-16", "2026-11-17"), start=2):
        _put(
            client,
            key=f"chain-{index}",
            bill=False,
            invoice="INV-CHAIN",
            date=invoice_date,
        )
        _send(
            client,
            f"evt-chain-{index}",
            "transactions.receipt_added",
            f"chain-{index}",
        )
    _send(client, "evt-chain-2", "transactions.receipt_added", "chain-2")
    with client.app.state.ctx.session_factory() as session:
        rows = list(session.scalars(select(Purchase)).all())
        assert len(rows) == 3
        assert all(session.get(Purchase, row.id) is not None for row in rows)
    events = client.get("/api/v1/audit-events", params={"object_id": first_id}).json()[
        "audit_events"
    ]
    # Pipeline stage events (#53) come first and share the same chain.
    start = [event["command"] for event in events].index("add_fact")
    assert all(event["actor_id"] == "system" for event in events[:start])
    assert events[0]["before_hash"] == ""
    for previous, current in zip(events, events[1:], strict=False):
        assert current["before_hash"] == previous["after_hash"]
    for event in events:
        assert event["after_hash"] == _after_hash(event)
    tail = events[start:]
    matches = [
        event
        for event in tail
        if event["command"].startswith("ambiguous_invoice_match:")
    ]
    assert len(matches) == 2
    assert matches[0]["command"] != matches[1]["command"]
    for event in tail:
        assert event["object_version"] == 2
    detail = client.get(f"/api/v1/purchases/{first_id}").json()
    assert len(detail["possible_duplicate_ids"]) == 2
    assert detail["queue"] != "needs_human_review"


def _assert_receipt_invalidated(
    client: TestClient, purchase_id: str, determination_id: str
) -> None:
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    assert detail["facts_version"] == 2
    assert detail["queue"] == "held"
    assert detail["determinations"] == []
    assert detail["possible_duplicate_ids"] == []
    tax = next(fact for fact in detail["facts"] if fact["field"] == "tax_charged")
    assert tax["value"] == "0.00"
    assert tax["confirmation"] == "UNCONFIRMED"
    totals = client.get("/api/v1/totals").json()
    assert totals["approved_liabilities"]["by_jurisdiction"] == []
    assert totals["approved_liabilities"]["message"] == "No approved liabilities"
    with client.app.state.ctx.session_factory() as session:
        row = session.get(Determination, determination_id)
        assert row is not None
        assert row.is_current is False
        assert row.unit_status == "SUPERSEDED"
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        assert purchase.ramp_ocr_taxes == "1.00"


def test_duplicate_lookup_does_not_treat_underscores_as_wildcards(
    signed_client: TestClient,  # noqa: F811
) -> None:
    from uuid import uuid4

    from app.clock import utcnow
    from app.models import AuditEvent

    created = _run(signed_client, "case1", "like-case1")
    purchase_id = created["purchase_id"]
    with signed_client.app.state.ctx.session_factory() as session:
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        session.add(
            AuditEvent(
                id=str(uuid4()),
                org_id=purchase.org_id,
                actor_id="system",
                command="ambiguousXinvoiceXmatch:not-a-purchase",
                object_type="purchase",
                object_id=purchase_id,
                object_version=purchase.facts_version,
                before_hash="",
                after_hash="lookalike",
                created_at=utcnow(),
            )
        )
        session.commit()
    detail = signed_client.get(f"/api/v1/purchases/{purchase_id}").json()
    assert detail["possible_duplicate_ids"] == []
