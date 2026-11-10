"""Refund letters use only a basis written for a vendor refund letter (#51)."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.commands import _filing_basis_text

ROOT = Path(__file__).resolve().parents[2]


def _basis(jurisdiction: str, category: str) -> str:
    purchase: Any = SimpleNamespace(category=category)
    determination: Any = SimpleNamespace(jurisdiction=jurisdiction)
    return _filing_basis_text(str(ROOT / "rules"), purchase, determination)


def test_ca_saas_refund_gets_the_signed_vendor_letter_basis() -> None:
    text = _basis("CA", "REMOTE_ACCESS_SAAS")
    assert text.startswith("This is a DRAFT vendor letter")
    assert "was sent" not in text.lower()


@pytest.mark.parametrize(
    ("jurisdiction", "category"),
    [
        ("NY", "COMPUTER_HARDWARE"),
        ("NY", "REMOTE_ACCESS_SAAS"),
        ("WA", "REMOTE_ACCESS_SAAS"),
        ("CA", "COMPUTER_HARDWARE"),
    ],
)
def test_other_refunds_keep_an_empty_basis(jurisdiction: str, category: str) -> None:
    assert _basis(jurisdiction, category) == ""


def test_missing_basis_file_keeps_an_empty_basis(tmp_path: Path) -> None:
    purchase: Any = SimpleNamespace(category="REMOTE_ACCESS_SAAS")
    determination: Any = SimpleNamespace(jurisdiction="CA")
    assert _filing_basis_text(str(tmp_path), purchase, determination) == ""
