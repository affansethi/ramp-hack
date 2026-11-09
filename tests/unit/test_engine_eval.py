"""Evaluator: unsigned rules never emit an amount. A reviewed rule's string is exact."""

from datetime import date
from pathlib import Path

from app.domain.evaluate import EngineLineRequest, evaluate_line
from app.domain.ruleset import (
    RateEntry,
    RuleEntry,
    Ruleset,
    load_ruleset,
    rate_is_active,
    rule_is_active,
)


def _wa_request(**overrides: object) -> EngineLineRequest:
    base = EngineLineRequest(
        jurisdiction="WA",
        category="REMOTE_ACCESS_SAAS",
        invoice_date=date(2026, 11, 15),
        service_period_start=date(2026, 11, 1),
        delivery_date=None,
        line_amount="1440.00",
        tax_charged="0.00",
        shipping=None,
        location_name="Seattle, WA",
        state_count=1,
    )
    data = {**base.__dict__, **overrides}
    return EngineLineRequest(**data)


def _reviewed_ruleset() -> Ruleset:
    rule = RuleEntry(
        id="WA-RAS-001",
        jurisdiction="WA",
        category="REMOTE_ACCESS_SAAS",
        treatment="Retail sale. Buyer owes use tax when the vendor charged none",
        calculation="net",
        applies_when="single_state",
        effective_from=date(2009, 7, 26),
        effective_to=None,
        timing_basis="service_period_start",
        primary_source_url="https://example.test/wa",
        review_status="reviewed",
        reviewed_by="AF",
        reviewed_at="2026-11-01",
    )
    rate = RateEntry(
        jurisdiction="WA",
        location="Seattle, WA",
        location_code="1726",
        rate="0.1055",
        quarter="2026Q4",
        source_url="https://example.test/rate",
        review_status="reviewed",
        reviewed_by="AF",
    )
    return Ruleset(rules=(rule,), rates=(rate,), catalog=(), version="test")


def test_shipped_rules_are_inactive() -> None:
    ruleset = load_ruleset(Path("rules"))
    assert ruleset.rules
    assert not any(rule_is_active(rule) for rule in ruleset.rules)
    assert not any(rate_is_active(rate) for rate in ruleset.rates)


def test_inactive_rules_emit_no_amount() -> None:
    result = evaluate_line(load_ruleset(Path("rules")), _wa_request())
    assert result.outcome == "unsupported"
    assert result.expected_tax is None
    assert result.adjustment_amount is None
    assert result.direction is None


def test_reviewed_rule_copies_a_decimal_string() -> None:
    result = evaluate_line(_reviewed_ruleset(), _wa_request())
    assert result.outcome == "supported"
    assert result.expected_tax == "151.92"
    assert result.adjustment_amount == "151.92"
    assert result.direction == "LIABILITY"
    assert result.rule_reference == "WA-RAS-001"
    assert result.rate == "0.1055"
    assert result.location_code == "1726"
    assert isinstance(result.expected_tax, str)


def test_january_2027_transition_is_nhr_with_no_amount() -> None:
    ruleset = load_ruleset(Path("rules"))
    # Sign nothing: the placeholder still has to win over silence for this date.
    result = evaluate_line(
        ruleset,
        EngineLineRequest(
            jurisdiction="CA",
            category="REMOTE_ACCESS_SAAS",
            invoice_date=date(2026, 12, 15),
            service_period_start=date(2027, 1, 1),
            delivery_date=None,
            line_amount="962.32",
            tax_charged="83.00",
            shipping=None,
            location_name="San Francisco, CA",
            state_count=1,
        ),
    )
    assert result.nhr_reason == "RULE_TRANSITION_UNRESOLVED"
    assert result.adjustment_amount is None
    assert result.expected_tax is None
