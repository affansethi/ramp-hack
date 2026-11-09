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


def test_bill_to_never_confirms_use() -> None:
    assert (
        location_confirmation(
            rung=1,
            seats=10,
            seats_covered=10,
            named_single_office=False,
            from_bill_to=True,
        )
        == "UNCONFIRMED"
    )
