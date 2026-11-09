"""Money contract: decimal strings on the wire, never binary float (D8)."""

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.contracts import Money


def test_money_serializes_as_decimal_string() -> None:
    money = Money(value=Decimal("10.00"), currency="USD")
    assert json.loads(money.model_dump_json()) == {"value": "10.00", "currency": "USD"}


def test_money_preserves_trailing_zeros() -> None:
    money = Money(value="10.00", currency="USD")
    assert money.value == Decimal("10.00")
    assert str(money.value) == "10.00"


def test_money_rejects_binary_float() -> None:
    with pytest.raises(ValidationError):
        Money(value=0.1, currency="USD")


def test_money_defaults_to_usd() -> None:
    assert Money(value="1.00").currency == "USD"
