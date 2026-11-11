"""Ramp simulator record parsing (#25 review follow-up)."""

from app.adapters.ramp import _invoice_date, _transaction_date


def test_null_transaction_time_falls_back_to_invoice_date() -> None:
    record = {"invoice_date": "2026-11-15", "user_transaction_time": None}
    assert _invoice_date(record) == "2026-11-15"


def test_null_transaction_time_without_invoice_date_is_missing() -> None:
    assert _invoice_date({"user_transaction_time": None}) == ""
    assert _invoice_date({}) == ""


def test_card_time_is_a_transaction_date_not_an_invoice_date() -> None:
    record = {"user_transaction_time": "2026-11-15T08:30:00Z"}
    assert _invoice_date(record) == ""
    assert _transaction_date(record) == "2026-11-15"
    assert _transaction_date({"user_transaction_time": None}) == ""
