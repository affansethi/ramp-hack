"""Evaluator: candidate rules emit no amount. A verified rule's string is exact."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.domain.evaluate import EngineLineRequest, EngineLineResult, evaluate_line
from app.domain.moneyutil import emit_cents
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


def _verified_ruleset() -> Ruleset:
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
        review_status="verified",
        reviewed_by=None,
        reviewed_at=None,
        evidence_ok=True,
    )
    rate = RateEntry(
        jurisdiction="WA",
        location="Seattle, WA",
        location_code="1726",
        rate="0.1055",
        quarter="2026Q4",
        source_url="https://example.test/rate",
        review_status="verified",
        reviewed_by=None,
        evidence_ok=True,
    )
    return Ruleset(rules=(rule,), rates=(rate,), catalog=(), version="test")


def test_placeholder_stays_inactive_after_owner_signoff() -> None:
    ruleset = load_ruleset(Path("rules"))
    assert ruleset.rules
    placeholders = [
        rule for rule in ruleset.rules if rule.id.startswith("PLACEHOLDER:")
    ]
    assert placeholders
    assert all(not rule_is_active(rule) for rule in placeholders)
    assert all(rule.reviewed_by is None for rule in placeholders)
    signed = [rule for rule in ruleset.rules if not rule.id.startswith("PLACEHOLDER:")]
    assert signed
    assert all(rule_is_active(rule) for rule in signed)
    assert all(rate_is_active(rate) for rate in ruleset.rates)


def test_unsigned_copy_emits_no_amount(tmp_path: Path) -> None:
    dest = tmp_path / "rules"
    dest.mkdir()
    for name in ("entries.yaml", "rates.yaml", "catalog.yaml"):
        text = Path("rules", name).read_text(encoding="utf-8")
        text = text.replace("review_status: reviewed", "review_status: candidate")
        text = text.replace("reviewed_by: AF", "reviewed_by:")
        (dest / name).write_text(text, encoding="utf-8")
    result = evaluate_line(load_ruleset(dest), _wa_request())
    assert result.outcome == "unsupported"
    assert result.rule_source_url is None
    assert result.expected_tax is None
    assert result.adjustment_amount is None
    assert result.direction is None


def test_reviewed_rule_copies_a_decimal_string() -> None:
    result = evaluate_line(_verified_ruleset(), _wa_request())
    assert result.outcome == "supported"
    assert result.expected_tax == "151.92"
    assert result.adjustment_amount == "151.92"
    assert result.direction == "LIABILITY"
    assert result.rule_reference == "WA-RAS-001"
    assert result.rule_source_url == "https://example.test/wa"
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
    assert result.rule_source_url is None
    assert result.adjustment_amount is None
    assert result.expected_tax is None


def _no_amount(result: EngineLineResult) -> None:
    assert result.expected_tax is None
    assert result.adjustment_amount is None
    assert result.direction is None
    assert result.nhr_reason is None
    assert result.rule_source_url is None


def test_emit_cents_keeps_exact_cents_and_refuses_to_round() -> None:
    assert emit_cents(Decimal("151.92")) == "151.92"
    assert emit_cents(Decimal("186.375")) is None


def test_inexact_product_is_an_error_with_no_amount() -> None:
    ruleset = _verified_ruleset()
    ruleset = Ruleset(
        rules=ruleset.rules,
        rates=(replace(ruleset.rates[0], rate="0.08875"),),
        catalog=ruleset.catalog,
        version=ruleset.version,
    )
    result = evaluate_line(ruleset, _wa_request(line_amount="2100.00"))
    assert result.outcome == "error"
    assert result.error_code == "INEXACT_CENTS"
    _no_amount(result)


def test_inexact_difference_is_an_error_with_no_amount() -> None:
    result = evaluate_line(_verified_ruleset(), _wa_request(tax_charged="1.005"))
    assert result.outcome == "error"
    assert result.error_code == "INEXACT_CENTS"
    _no_amount(result)


def test_not_taxable_uses_zero_instead_of_the_rate() -> None:
    ruleset = _verified_ruleset()
    ruleset = Ruleset(
        rules=(replace(ruleset.rules[0], calculation="not_taxable"),),
        rates=ruleset.rates,
        catalog=ruleset.catalog,
        version=ruleset.version,
    )
    charged_zero = evaluate_line(ruleset, _wa_request())
    assert charged_zero.outcome == "supported"
    assert charged_zero.expected_tax == "0.00"
    assert charged_zero.adjustment_amount is None
    assert charged_zero.direction == "CHARGED_CORRECTLY"
    charged = evaluate_line(ruleset, _wa_request(tax_charged="83.00"))
    assert charged.outcome == "supported"
    assert charged.expected_tax == "0.00"
    assert charged.adjustment_amount == "83.00"
    assert charged.direction == "REFUND_CANDIDATE"


def test_location_code_na_is_present_and_blank_is_an_error() -> None:
    ruleset = _verified_ruleset()
    present = Ruleset(
        rules=ruleset.rules,
        rates=(replace(ruleset.rates[0], location_code="n/a"),),
        catalog=ruleset.catalog,
        version=ruleset.version,
    )
    result = evaluate_line(present, _wa_request())
    assert result.outcome == "supported"
    assert result.location_code == "n/a"
    assert result.expected_tax == "151.92"
    blank = Ruleset(
        rules=ruleset.rules,
        rates=(replace(ruleset.rates[0], location_code=""),),
        catalog=ruleset.catalog,
        version=ruleset.version,
    )
    missing = evaluate_line(blank, _wa_request())
    assert missing.outcome == "error"
    assert missing.error_code == "INCOMPLETE_ENGINE_RESULT"
    _no_amount(missing)


def test_blank_rule_id_or_rate_is_an_error_with_no_amount() -> None:
    ruleset = _verified_ruleset()
    no_id = Ruleset(
        rules=(replace(ruleset.rules[0], id=""),),
        rates=ruleset.rates,
        catalog=ruleset.catalog,
        version=ruleset.version,
    )
    missing_id = evaluate_line(no_id, _wa_request())
    assert missing_id.outcome == "error"
    _no_amount(missing_id)
    no_rate = Ruleset(
        rules=ruleset.rules,
        rates=(replace(ruleset.rates[0], rate=" "),),
        catalog=ruleset.catalog,
        version=ruleset.version,
    )
    missing_rate = evaluate_line(no_rate, _wa_request())
    assert missing_rate.outcome == "error"
    _no_amount(missing_rate)


def test_multi_state_net_is_unsupported_with_no_amount() -> None:
    ruleset = _verified_ruleset()
    ruleset = Ruleset(
        rules=(
            replace(
                ruleset.rules[0],
                id="MEM-MPU",
                applies_when="multi_state",
                calculation="net",
            ),
        ),
        rates=ruleset.rates,
        catalog=ruleset.catalog,
        version=ruleset.version,
    )
    result = evaluate_line(ruleset, _wa_request(state_count=2))
    assert result.outcome == "unsupported"
    assert result.nhr_reason == "ENGINE_UNSUPPORTED"
    assert result.rule_source_url is None
    assert result.expected_tax is None
    assert result.adjustment_amount is None
    assert result.direction is None


def test_signoff_fields_do_not_activate() -> None:
    rule = replace(
        _verified_ruleset().rules[0],
        review_status="reviewed",
        reviewed_by="AF",
        evidence_ok=True,
    )
    rate = replace(
        _verified_ruleset().rates[0],
        review_status="reviewed",
        reviewed_by="AF",
        evidence_ok=True,
    )
    assert not rule_is_active(rule)
    assert not rate_is_active(rate)


def test_verified_without_evidence_does_not_activate() -> None:
    rule = replace(_verified_ruleset().rules[0], evidence_ok=False)
    assert rule.review_status == "verified"
    assert not rule_is_active(rule)


def test_verified_placeholder_never_activates() -> None:
    rule = replace(
        _verified_ruleset().rules[0],
        id="PLACEHOLDER:CA-DP-001",
        jurisdiction="CA",
        applies_when="any",
        effective_from=date(2027, 1, 1),
        timing_basis="service_period_start",
    )
    result = evaluate_line(
        Ruleset(rules=(rule,), rates=(), catalog=(), version="test"),
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
    assert result.outcome == "unsupported"
    assert result.nhr_reason == "RULE_TRANSITION_UNRESOLVED"
    assert result.rule_source_url is None
    assert result.expected_tax is None
    assert result.adjustment_amount is None
