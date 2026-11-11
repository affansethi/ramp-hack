"""Hold check: a draft catalog admits nothing, and a missing fact is not a zero."""

from app.domain.holds import FactView, decide_hold, location_confirmation
from app.domain.ruleset import CatalogEntry


def _catalog(status: str, signed_off_by: str | None) -> tuple[CatalogEntry, ...]:
    return (
        CatalogEntry(
            category="REMOTE_ACCESS_SAAS",
            required_keys=("vendor_name", "user_location", "tax_charged"),
            status=status,
            signed_off_by=signed_off_by,
        ),
    )


def _facts(location: FactView | None) -> dict[str, FactView]:
    facts = {
        "vendor_name": FactView("vendor_name", "Synthetic Cloud Co", True, "CONFIRMED"),
        "tax_charged": FactView("tax_charged", "0.00", True, "CONFIRMED"),
    }
    if location is not None:
        facts["user_location"] = location
    return facts


def test_unsigned_catalog_holds_without_an_amount() -> None:
    decision = decide_hold(
        "REMOTE_ACCESS_SAAS", _facts(None), _catalog("candidate", None)
    )
    assert decision is not None
    assert decision.named_fact == "required_fact_catalog"
    assert decision.reason == "UNCONFIRMED_REQUIRED_FACT"


def test_signed_catalog_holds_a_missing_location() -> None:
    decision = decide_hold("REMOTE_ACCESS_SAAS", _facts(None), _catalog("signed", "AF"))
    assert decision is not None
    assert decision.reason == "MISSING_REQUIRED_FACT"
    assert decision.named_fact == "user_location"


def _location(**overrides: object) -> str:
    values: dict[str, object] = {
        "rung": 1,
        "seats": 10,
        "seats_covered": 10,
        "named_single_office": False,
        "from_bill_to": False,
    }
    values.update(overrides)
    return location_confirmation(**values)  # type: ignore[arg-type]


def test_bill_to_never_confirms_use() -> None:
    assert _location(from_bill_to=True) == "UNCONFIRMED"


def test_partial_coverage_and_conflicts_stay_unconfirmed() -> None:
    assert _location(seats_covered=4) == "UNCONFIRMED"
    assert _location(conflicting_sources=True) == "UNCONFIRMED"


def test_rungs_4_and_5_need_a_named_single_office() -> None:
    assert _location(rung=4) == "UNCONFIRMED"
    assert _location(rung=5, named_single_office=True) == "CONFIRMED"


def test_rung_3_confirms_only_for_business_location() -> None:
    assert _location(rung=3) == "UNCONFIRMED"
    assert _location(rung=3, business_location=True) == "CONFIRMED"


def test_low_confidence_needs_recorded_confirmation() -> None:
    assert _location(low_confidence=True) == "UNCONFIRMED"
    assert _location(low_confidence=True, recorded_confirmation=True) == "CONFIRMED"


def test_omitted_confidence_on_a_full_seat_export_confirms() -> None:
    assert _location() == "CONFIRMED"
