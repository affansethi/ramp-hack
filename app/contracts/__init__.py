"""Typed API/domain contracts: money, closed enums, AI output schemas.

Closed enums are contract shapes: adding a value is a contract change
needing both owners (AF and RJ). AI schemas must never carry rate,
tax-decision amount, treatment, label, direction or verdict fields.
"""

from app.contracts.enums import (
    DataMode,
    DemoMode,
    Direction,
    EngineMode,
    FactConfirmation,
    FactOrigin,
    HoldReason,
    Label,
    LLMMode,
    NHRReason,
    WorkpaperStatus,
)
from app.contracts.money import USD, Money

__all__ = [
    "USD",
    "DataMode",
    "DemoMode",
    "Direction",
    "EngineMode",
    "FactConfirmation",
    "FactOrigin",
    "HoldReason",
    "Label",
    "LLMMode",
    "Money",
    "NHRReason",
    "WorkpaperStatus",
]
