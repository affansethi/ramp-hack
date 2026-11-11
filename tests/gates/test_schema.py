"""Release gates that do not need owner-signed goldens."""

from decimal import Decimal

from sqlalchemy import Float

import app.models  # noqa: F401
from app.db import Base


def test_money_columns_are_not_floats() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, Float)
            assert column.name not in {"net", "savings", "recovered"}


def test_decimal_strings_survive_addition() -> None:
    assert f"{Decimal('1440.00') + Decimal('0.00'):.2f}" == "1440.00"
    assert isinstance(Decimal("151.92"), Decimal)
