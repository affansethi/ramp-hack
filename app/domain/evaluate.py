"""Deterministic evaluator. Inactive rules never produce an amount."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.domain.moneyutil import emit_cents, parse_decimal
from app.domain.ruleset import (
    RateEntry,
    RuleEntry,
    Ruleset,
    is_placeholder,
    rate_is_active,
    rule_is_active,
)

Outcome = Literal["supported", "unsupported", "error"]


@dataclass(frozen=True)
class EngineLineRequest:
    jurisdiction: str
    category: str
    invoice_date: date
    service_period_start: date | None
    delivery_date: date | None
    line_amount: str
    tax_charged: str
    shipping: str | None
    location_name: str
    state_count: int


@dataclass(frozen=True)
class EngineLineResult:
    outcome: Outcome
    nhr_reason: str | None
    error_code: str | None
    treatment: str | None
    expected_tax: str | None
    adjustment_amount: str | None
    direction: str | None
    derived_direction: bool
    rule_reference: str | None
    rate: str | None
    location_code: str | None


def quarter_key(day: date) -> str:
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def evaluate_line(ruleset: Ruleset, request: EngineLineRequest) -> EngineLineResult:
    matching = [rule for rule in ruleset.rules if _applies(rule, request)]
    active = [rule for rule in matching if rule_is_active(rule)]
    if len(active) > 1:
        return _error("AMBIGUOUS_RULE")
    if not active:
        if any(is_placeholder(rule.id) for rule in matching):
            return _unsupported("RULE_TRANSITION_UNRESOLVED")
        return _unsupported("ENGINE_UNSUPPORTED")
    rule = active[0]
    if rule.calculation == "not_taxable":
        expected = "0.00"
        rate_row = _find_rate(ruleset, request)
    else:
        rate_row = _find_rate(ruleset, request)
        if rate_row is None:
            return _error("MISSING_RATE_REFERENCE")
        base = parse_decimal(request.line_amount)
        if rule.calculation == "net_plus_shipping":
            if request.shipping is None:
                return _error("MISSING_SHIPPING_FACT")
            base += parse_decimal(request.shipping)
        expected = emit_cents(base * parse_decimal(rate_row.rate))
    if rate_row is None and rule.calculation == "not_taxable":
        return _error("MISSING_RATE_REFERENCE")
    assert rate_row is not None
    direction, adjustment = _direction(expected, request.tax_charged)
    return EngineLineResult(
        outcome="supported",
        nhr_reason=None,
        error_code=None,
        treatment=rule.treatment,
        expected_tax=expected,
        adjustment_amount=adjustment,
        direction=direction,
        derived_direction=True,
        rule_reference=rule.id,
        rate=rate_row.rate,
        location_code=rate_row.location_code,
    )


def _applies(rule: RuleEntry, request: EngineLineRequest) -> bool:
    if rule.jurisdiction != request.jurisdiction or rule.category != request.category:
        return False
    if rule.applies_when == "single_state" and request.state_count != 1:
        return False
    if rule.applies_when == "multi_state" and request.state_count < 2:
        return False
    when = _timing_date(rule, request)
    if when is None:
        return False
    if when < rule.effective_from:
        return False
    return rule.effective_to is None or when <= rule.effective_to


def _timing_date(rule: RuleEntry, request: EngineLineRequest) -> date | None:
    if rule.timing_basis == "service_period_start":
        return request.service_period_start
    if rule.timing_basis == "delivery_date":
        return request.delivery_date
    return request.invoice_date


def _find_rate(ruleset: Ruleset, request: EngineLineRequest) -> RateEntry | None:
    quarter = quarter_key(request.invoice_date)
    for rate in ruleset.rates:
        if (
            rate_is_active(rate)
            and rate.jurisdiction == request.jurisdiction
            and rate.location == request.location_name
            and rate.quarter == quarter
        ):
            return rate
    return None


def _direction(expected: str, tax_charged: str) -> tuple[str, str | None]:
    expected_amount = parse_decimal(expected)
    charged = parse_decimal(tax_charged)
    if expected_amount == charged:
        return "CHARGED_CORRECTLY", None
    if expected_amount > charged:
        return "LIABILITY", emit_cents(expected_amount - charged)
    return "REFUND_CANDIDATE", emit_cents(charged - expected_amount)


def _unsupported(reason: str) -> EngineLineResult:
    return EngineLineResult(
        outcome="unsupported",
        nhr_reason=reason,
        error_code=None,
        treatment=None,
        expected_tax=None,
        adjustment_amount=None,
        direction=None,
        derived_direction=False,
        rule_reference=None,
        rate=None,
        location_code=None,
    )


def _error(code: str) -> EngineLineResult:
    return EngineLineResult(
        outcome="error",
        nhr_reason=None,
        error_code=code,
        treatment=None,
        expected_tax=None,
        adjustment_amount=None,
        direction=None,
        derived_direction=False,
        rule_reference=None,
        rate=None,
        location_code=None,
    )
