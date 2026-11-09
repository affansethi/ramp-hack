"""Decimal helpers. Facts keep their printed strings. Only the engine quantizes."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ONE_CENT = Decimal("0.01")


def parse_decimal(value: str) -> Decimal:
    if isinstance(value, float):
        raise ValueError("money must be a decimal string, never a binary float")
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError("money is not a finite decimal")
    return amount


def emit_cents(amount: Decimal) -> str:
    """The engine's one rounding step: half-up to a two-decimal USD string."""
    return f"{amount.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def differs_by_more_than_one_cent(left: str, right: str) -> bool:
    return abs(parse_decimal(left) - parse_decimal(right)) > ONE_CENT


def money_json(value: str) -> dict[str, str]:
    return {"value": value, "currency": "USD"}
