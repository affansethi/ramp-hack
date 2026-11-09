"""Decimal helpers. Facts keep their printed strings. Only the engine quantizes."""

from decimal import Decimal

CENT = Decimal("0.01")
ONE_CENT = Decimal("0.01")


def parse_decimal(value: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError("money must be a decimal string, never a binary float")
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError("money is not a finite decimal")
    return amount


def emit_cents(amount: Decimal) -> str | None:
    """Two-decimal string when the amount is already an exact cent. Never rounds."""
    if not amount.is_finite():
        return None
    cents = amount.quantize(CENT)
    if cents != amount:
        return None
    return f"{cents:.2f}"


def differs_by_more_than_one_cent(left: str, right: str) -> bool:
    return abs(parse_decimal(left) - parse_decimal(right)) > ONE_CENT


def money_json(value: str) -> dict[str, str]:
    return {"value": value, "currency": "USD"}
