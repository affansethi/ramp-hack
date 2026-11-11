"""Ramp receipt fields stay sourced evidence. card_holder never confirms use."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Evidence, Fact, Hold, Purchase, PurchaseLine
from app.worker.poller import poll_once
from tests.api.test_flow import signed_client  # noqa: F401
from tests.api.test_ramp_fetch import _record, _send


def _set_receipt(client: TestClient, *, total: str, tax: str) -> None:
    simulator = client.app.state.ctx.ramp
    simulator.transactions["synthetic-id"]["merchant_data"] = {
        "receipt": {
            "items": [
                {
                    "description": "Seat",
                    "quantity": "1",
                    "unit_cost": total,
                    "tax": tax,
                    "total": total,
                }
            ]
        }
    }
    simulator.receipts["receipt-25"]["ocr"]["line_items"] = [
        {
            "description": "Seat (OCR)",
            "amount": total,
            "tax_amount": tax,
            "tax_rate": "999",
        }
    ]


def _current(client: TestClient, purchase_id: str) -> dict[str, list[Fact]]:
    with client.app.state.ctx.session_factory() as session:
        rows = session.scalars(
            select(Fact).where(
                Fact.purchase_id == purchase_id, Fact.is_current.is_(True)
            )
        ).all()
    found: dict[str, list[Fact]] = {}
    for row in rows:
        found.setdefault(row.field_key, []).append(row)
    return found


def _only_purchase(client: TestClient) -> Purchase:
    with client.app.state.ctx.session_factory() as session:
        purchase = session.scalars(select(Purchase)).one()
        session.expunge(purchase)
        return purchase


def test_documented_receipt_items_and_ocr_lines_are_linked_decimal_strings(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _record(client)
    _set_receipt(client, total="123.450", tax="7.250")
    client.app.state.ctx.ramp.transactions["synthetic-id"]["bill_to"] = "Bill-to City"
    _send(client, "evt-receipts", "transactions.cleared", "synthetic-id")
    poll_once(client.app.state.ctx)
    purchase = _only_purchase(client)
    facts = _current(client, purchase.id)
    with client.app.state.ctx.session_factory() as session:
        line_id = session.scalars(
            select(PurchaseLine.id).where(PurchaseLine.purchase_id == purchase.id)
        ).first()
    assert line_id is not None
    expected = {
        "receipt_line_description": "Seat",
        "receipt_line_amount": "123.450",
        "receipt_line_tax": "7.250",
        "ocr_line_description": "Seat (OCR)",
        "ocr_line_amount": "123.450",
        "ocr_line_tax": "7.250",
    }
    for key, value in expected.items():
        [fact] = facts[key]
        assert fact.value == value
        assert fact.line_id == line_id
        assert fact.origin == "RAMP_RECORD"
        assert fact.confirmation == "UNCONFIRMED"
    # Receipt and OCR items never become an admitted line_amount or tax_charged.
    admitted = [
        fact.id for key in ("line_amount", "tax_charged") for fact in facts.get(key, [])
    ]
    with client.app.state.ctx.session_factory() as session:
        passages = session.scalars(
            select(Evidence.passage).where(Evidence.fact_id.in_(admitted))
        ).all()
    assert not any(
        passage.startswith(("Ramp merchant_data.receipt", "Ramp receipt OCR"))
        for passage in passages
    )
    with client.app.state.ctx.session_factory() as session:
        ramp_ids = [fact.id for group in facts.values() for fact in group]
        evidence = session.scalars(
            select(Evidence).where(Evidence.fact_id.in_(ramp_ids))
        ).all()
        assert {row.fact_id for row in evidence} >= {
            fact.id
            for group in facts.values()
            for fact in group
            if fact.origin == "RAMP_RECORD"
        }
    [holder] = [fact for fact in facts["user_location"] if fact.origin == "RAMP_RECORD"]
    assert holder.value == "Seattle"
    assert holder.confirmation == "UNCONFIRMED"
    assert holder.evidence_rung == 5
    [mismatch] = facts["merchant_location_mismatch"]
    assert mismatch.value == "true"
    [bill] = facts["bill_to"]
    assert bill.confirmation == "UNCONFIRMED"


def test_card_holder_and_bill_to_never_release_a_location_hold(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _record(client)
    client.app.state.ctx.ramp.transactions["synthetic-id"]["bill_to"] = "Seattle HQ"
    _send(client, "evt-holder", "transactions.cleared", "synthetic-id")
    poll_once(client.app.state.ctx)
    poll_once(client.app.state.ctx)
    purchase = _only_purchase(client)
    detail = client.get(f"/api/v1/purchases/{purchase.id}").json()
    assert detail["determinations"] == []
    assert detail["queue"] in {"held", "needs_human_review"}
    with client.app.state.ctx.session_factory() as session:
        active = session.scalars(
            select(Hold).where(Hold.purchase_id == purchase.id, Hold.active.is_(True))
        ).all()
    assert all(hold.named_fact != "released_by_card_holder" for hold in active)


def test_later_event_stores_new_receipt_evidence_and_versions_the_purchase(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    ctx = client.app.state.ctx
    _record(client)
    _send(client, "evt-first", "transactions.cleared", "synthetic-id")
    poll_once(ctx)
    poll_once(ctx)
    purchase = _only_purchase(client)
    assert "receipt_line_amount" not in _current(client, purchase.id)
    _set_receipt(client, total="123.450", tax="7.250")
    _send(client, "evt-receipt", "transactions.receipt_added", "synthetic-id")
    poll_once(ctx)
    later = _only_purchase(client)
    assert later.id == purchase.id
    assert later.facts_version == purchase.facts_version + 1
    facts = _current(client, purchase.id)
    assert [fact.value for fact in facts["receipt_line_amount"]] == ["123.450"]
    assert [fact.value for fact in facts["ocr_line_tax"]] == ["7.250"]


def test_changed_receipt_supersedes_and_unchanged_replay_writes_nothing(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    ctx = client.app.state.ctx
    _record(client)
    _set_receipt(client, total="123.450", tax="7.250")
    _send(client, "evt-a", "transactions.cleared", "synthetic-id")
    poll_once(ctx)
    poll_once(ctx)
    first = _only_purchase(client)
    _send(client, "evt-b", "transactions.receipt_added", "synthetic-id")
    poll_once(ctx)
    assert _only_purchase(client).facts_version == first.facts_version
    _set_receipt(client, total="123.450", tax="8.000")
    _send(client, "evt-c", "transactions.receipt_added", "synthetic-id")
    poll_once(ctx)
    changed = _only_purchase(client)
    assert changed.facts_version == first.facts_version + 1
    facts = _current(client, first.id)
    [tax] = facts["receipt_line_tax"]
    assert tax.value == "8.000"
    assert tax.supersedes is not None
    with ctx.session_factory() as session:
        old = session.get(Fact, tax.supersedes)
        assert old is not None
        assert old.value == "7.250"
        assert old.is_current is False
