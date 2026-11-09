"""Hold check against a signed catalog. A draft catalog admits nothing."""

from dataclasses import dataclass

from app.domain.moneyutil import parse_decimal
from app.domain.ruleset import CatalogEntry, catalog_is_signed

DECLARED = frozenset({"CA", "WA", "NY"})


@dataclass(frozen=True)
class FactView:
    key: str
    value: str | None
    present: bool
    confirmation: str


@dataclass(frozen=True)
class HoldDecision:
    reason: str
    named_fact: str


def decide_hold(
    category: str, facts: dict[str, FactView], catalog: tuple[CatalogEntry, ...]
) -> HoldDecision | None:
    signed = [
        entry
        for entry in catalog
        if entry.category == category and catalog_is_signed(entry)
    ]
    if not signed:
        return HoldDecision("UNCONFIRMED_REQUIRED_FACT", "required_fact_catalog")
    required = signed[0].required_keys
    for key in required:
        fact = facts.get(key)
        if fact is None or not fact.present or fact.value is None:
            return HoldDecision("MISSING_REQUIRED_FACT", key)
        if fact.confirmation != "CONFIRMED":
            return HoldDecision("UNCONFIRMED_REQUIRED_FACT", key)
    tax = facts.get("tax_charged")
    if (
        tax is not None
        and tax.present
        and tax.value is not None
        and tax.confirmation == "CONFIRMED"
        and parse_decimal(tax.value) > 0
    ):
        jurisdiction = facts.get("tax_charged_jurisdiction")
        if (
            jurisdiction is None
            or not jurisdiction.present
            or jurisdiction.confirmation != "CONFIRMED"
        ):
            return HoldDecision("MISSING_REQUIRED_FACT", "tax_charged_jurisdiction")
    return None


def location_confirmation(
    *,
    rung: int | None,
    seats: int | None,
    seats_covered: int | None,
    named_single_office: bool,
    from_bill_to: bool,
    low_confidence: bool = False,
    recorded_confirmation: bool = False,
    business_location: bool = False,
    conflicting_sources: bool = False,
) -> str:
    """Bill-to never confirms use. Low confidence needs a recorded confirmation."""
    if from_bill_to or conflicting_sources or rung is None:
        return "UNCONFIRMED"
    if seats is None or seats_covered is None or seats <= 0 or seats_covered != seats:
        return "UNCONFIRMED"
    if low_confidence and not recorded_confirmation:
        return "UNCONFIRMED"
    if rung in (4, 5):
        return "CONFIRMED" if named_single_office else "UNCONFIRMED"
    if rung == 3:
        return "CONFIRMED" if business_location else "UNCONFIRMED"
    if rung in (1, 2):
        return "CONFIRMED"
    return "UNCONFIRMED"


def confirmed_states(facts: dict[str, FactView]) -> list[str]:
    states: list[str] = []
    location = facts.get("user_location")
    if (
        location is not None
        and location.present
        and location.confirmation == "CONFIRMED"
        and location.value
    ):
        states.append(location.value)
    delivery = facts.get("delivery_evidence")
    if (
        delivery is not None
        and delivery.present
        and delivery.confirmation == "CONFIRMED"
        and delivery.value
        and delivery.value not in states
    ):
        states.append(delivery.value)
    tax_state = facts.get("tax_charged_jurisdiction")
    tax = facts.get("tax_charged")
    if (
        tax is not None
        and tax.present
        and tax.value is not None
        and tax.confirmation == "CONFIRMED"
        and parse_decimal(tax.value) > 0
        and tax_state is not None
        and tax_state.present
        and tax_state.confirmation == "CONFIRMED"
        and tax_state.value
        and tax_state.value not in states
    ):
        states.append(tax_state.value)
    return states


def undeclared(states: list[str]) -> bool:
    return any(state not in DECLARED for state in states)
