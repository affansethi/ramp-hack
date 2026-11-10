"""Ramp receipt fields stay sourced evidence. card_holder never confirms use."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Evidence, Fact, Hold
from app.worker.poller import poll_once
from tests.api.test_flow import signed_client  # noqa: F401
from tests.api.test_ramp_fetch import _record, _send


def test_receipt_and_ocr_items_are_decimal_strings(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _record(client)
    simulator = client.app.state.ctx.ramp
    simulator.transactions["synthetic-id"]["merchant_data"] = {
        "receipt": [{"amount": "123.450", "description": "Seat"}]
    }
    simulator.receipts["receipt-25"]["ocr"]["line_items"] = [
        {"tax_amount": "7.250", "tax_rate": "999"}
    ]
    simulator.transactions["synthetic-id"]["bill_to"] = "Bill-to City"
    _send(client, "evt-receipts", "transactions.cleared", "synthetic-id")
    poll_once(client.app.state.ctx)
    with client.app.state.ctx.session_factory() as session:
        amounts = list(
            session.scalars(select(Fact).where(Fact.field_key == "line_amount")).all()
        )
        taxes = list(
            session.scalars(select(Fact).where(Fact.field_key == "tax_charged")).all()
        )
        assert any(fact.value == "123.450" for fact in amounts)
        assert any(fact.value == "7.250" for fact in taxes)
        assert all(fact.origin == "RAMP_RECORD" for fact in amounts + taxes)
        evidence = list(session.scalars(select(Evidence)).all())
        assert evidence
        holder = session.scalar(
            select(Fact).where(Fact.field_key == "user_location")
        )
        assert holder is not None
        assert holder.confirmation == "UNCONFIRMED"
        assert holder.evidence_rung == 5
        mismatch = session.scalar(
            select(Fact).where(Fact.field_key == "merchant_location_mismatch")
        )
        assert mismatch is not None
        bill = session.scalar(select(Fact).where(Fact.field_key == "bill_to"))
        assert bill is not None
        assert bill.confirmation == "UNCONFIRMED"
        hold = session.scalar(select(Hold).where(Hold.active.is_(True)))
        assert hold is None or hold.named_fact != "released_by_card_holder"
