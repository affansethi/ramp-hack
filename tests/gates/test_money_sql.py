"""D13: money is VARCHAR decimal strings, so SQL never does money math.

No query may sum, average, take min/max of, compare or sort a money column.
Money arithmetic happens in Python ``Decimal`` after the row is read.
"""

import ast
import re
from pathlib import Path

from sqlalchemy import String

import app.models  # noqa: F401
from app.db import Base

ROOT = Path(__file__).resolve().parents[2]
_MONEY_NAME = re.compile(r"(amount|tax|total|price|shipping)", re.IGNORECASE)
_AGGREGATES = {"sum", "avg", "min", "max"}
_SQL_MONEY = re.compile(
    r"\b(SUM|AVG|MIN|MAX)\s*\(|ORDER\s+BY\s+\w*(amount|tax|total)", re.IGNORECASE
)


def money_columns() -> set[str]:
    """Model attributes that hold money as strings, e.g. engine_amount."""
    found: set[str] = set()
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, String) and _MONEY_NAME.search(column.name):
                found.add(column.name)
    return found


def _money_attr(node: ast.AST, money: set[str]) -> str | None:
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and child.attr in money
            and isinstance(child.value, ast.Name)
            and child.value.id[:1].isupper()
        ):
            return f"{child.value.id}.{child.attr}"
    return None


def money_sql_violations(source: str, money: set[str]) -> list[str]:
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            owner = node.func.value
            is_aggregate = (
                name in _AGGREGATES
                and isinstance(owner, ast.Name)
                and owner.id == "func"
            )
            if is_aggregate or name in {"order_by", "group_by"}:
                for arg in node.args:
                    hit = _money_attr(arg, money)
                    if hit:
                        found.append(f"{name}({hit})")
        if isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            for side in sides:
                if (
                    isinstance(side, ast.Attribute)
                    and side.attr in money
                    and isinstance(side.value, ast.Name)
                    and side.value.id[:1].isupper()
                ):
                    found.append(f"compare({side.value.id}.{side.attr})")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _SQL_MONEY.search(node.value)
        ):
            found.append(f"sql({node.value[:40]!r})")
    return found


def test_money_columns_are_found() -> None:
    money = money_columns()
    assert {"engine_amount", "expected_tax", "line_amount", "amount"} <= money


def test_app_code_never_does_money_math_in_sql() -> None:
    money = money_columns()
    problems: dict[str, list[str]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        hits = money_sql_violations(path.read_text(encoding="utf-8"), money)
        if hits:
            problems[str(path.relative_to(ROOT))] = hits
    assert problems == {}


def test_planted_money_sql_is_caught() -> None:
    money = money_columns()
    planted = [
        "q = select(func.sum(LiabilityLine.amount))",
        "q = select(LiabilityLine).order_by(LiabilityLine.amount)",
        "q = select(Determination).where(Determination.engine_amount > '10.00')",
        "q = text('SELECT SUM(amount) FROM liability_lines')",
        "q = text('SELECT * FROM liability_lines ORDER BY amount')",
    ]
    for source in planted:
        assert money_sql_violations(source, money), source


def test_python_decimal_math_on_a_read_row_is_allowed() -> None:
    money = money_columns()
    allowed = "total = sum(parse_decimal(row.amount) for row in rows)"
    assert money_sql_violations(allowed, money) == []
