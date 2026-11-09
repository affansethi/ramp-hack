"""Money on the wire: {"value": "10.00", "currency": "USD"} (D8).

Decimal strings end to end; binary floats are rejected on every money
path. Engine decimal strings are copied byte-for-byte, never rounded,
recomputed or filled in.
"""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

USD: Literal["USD"] = "USD"


def _reject_binary_float(value: Any) -> Any:
    if isinstance(value, float):
        raise ValueError("money must be a decimal string, never a binary float")
    return value


MoneyValue = Annotated[Decimal, Field(json_schema_extra={"example": "10.00"})]


class Money(BaseModel):
    """API money shape. ``value`` is a decimal string on the wire."""

    model_config = ConfigDict(frozen=True)

    value: MoneyValue
    currency: Literal["USD"] = USD

    @field_validator("value", mode="before")
    @classmethod
    def _no_float(cls, value: Any) -> Any:
        return _reject_binary_float(value)
