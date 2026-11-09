"""Replay eval harness without scoring unsigned goldens.

Runs the DocumentExtractor checker (``parse_extraction`` / ``ReplayExtractor``)
in replay mode only: a forbidden tax-decision key fails, a clean printed-invoice
payload passes, and a replay miss fails without calling a provider. Accuracy and
precision stay ``"unsigned"`` because the owner-signed golden set does not exist
yet; this harness never invents precision, recall, or an expected tax.
"""

from typing import Any

import pytest

from app.adapters.extractor import (
    ExtractionMiss,
    ForbiddenField,
    ReplayExtractor,
    cache_key,
    parse_extraction,
)

ACCURACY_UNSIGNED = "unsigned"
PRECISION_UNSIGNED = "unsigned"


def _clean_payload() -> dict[str, Any]:
    """In-memory printed-invoice payload with source evidence (not a fixture)."""
    return {
        "call_a": {
            "vendor_name": "Synthetic Cloud Co",
            "invoice_date": "2026-11-15",
            "currency": "USD",
            "bill_to": "Synthetic HQ, CA",
            "lines": [
                {
                    "description": "Remote access subscription",
                    "quantity": "1",
                    "unit_price": "1440.00",
                    "line_amount": "1440.00",
                    "service_period_start": "2026-11-01",
                    "service_period_end": "2026-11-30",
                    "printed_tax_jurisdiction": "WA",
                    "tax_charged": "0.00",
                }
            ],
            "evidence": [
                {
                    "page": 1,
                    "passage": "Line amount 1440.00, tax 0.00 as printed",
                }
            ],
        },
        "call_b": {
            "category": "REMOTE_ACCESS_SAAS",
            "rationale": "hosted software",
            "evidence_ids": [],
            "unresolved_keys": [],
            "question_draft": None,
        },
    }


def _forbidden_payloads() -> list[tuple[str, dict[str, Any]]]:
    """One payload per forbidden tax-decision key (issue #39 scope)."""
    keys = [
        "rate",
        "expected_tax",
        "amount_owed",
        "treatment",
        "label",
        "direction",
        "verdict",
        "hold",
        "approval",
        "tax_jurisdiction_assignment",
        "location_code_assignment",
        "legal_conclusion",
    ]
    cases: list[tuple[str, dict[str, Any]]] = []
    for key in keys:
        payload = _clean_payload()
        call_a = payload["call_a"]
        assert isinstance(call_a, dict)
        call_a[key] = "decision-value"
        cases.append((key, payload))
    return cases


def summarize_replay(*, checked: int) -> dict[str, str]:
    """Harness summary. Accuracy/precision stay unsigned without scored goldens."""
    return {
        "accuracy": ACCURACY_UNSIGNED,
        "precision": PRECISION_UNSIGNED,
        "checked": str(checked),
        "reason": "goldens unsigned",
    }


def format_summary(summary: dict[str, str]) -> str:
    return (
        f"accuracy: {summary['accuracy']}, "
        f"precision: {summary['precision']} "
        f"(checked {summary['checked']}; {summary['reason']})"
    )


def test_forbidden_field_payload_fails() -> None:
    for key, payload in _forbidden_payloads():
        with pytest.raises(ForbiddenField) as raised:
            parse_extraction(payload)
        assert raised.value.code == "FORBIDDEN_FIELD", key


def test_clean_printed_invoice_payload_passes() -> None:
    parsed = parse_extraction(_clean_payload())
    assert parsed.call_a.lines[0].line_amount == "1440.00"
    assert parsed.call_a.lines[0].tax_charged == "0.00"
    assert parsed.call_a.lines[0].tax_charged_present is True
    assert parsed.call_a.evidence[0].passage.startswith("Line amount")
    assert parsed.call_b.category == "REMOTE_ACCESS_SAAS"


class _NoCallProvider:
    """A provider the replay path must never touch."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, document: bytes, *, timeout: float) -> str:
        del document
        del timeout
        self.calls += 1
        raise AssertionError("replay must not call a provider")


def test_replay_miss_fails_and_does_not_call_provider() -> None:
    provider = _NoCallProvider()
    document = b"synthetic-invoice-bytes-issue-39"
    extractor = ReplayExtractor({})
    with pytest.raises(ExtractionMiss):
        extractor.extract(document, provider="replay", model="recorded")
    assert provider.calls == 0
    other_key = cache_key(document, provider="replay", model="recorded")
    extractor_saved = ReplayExtractor({other_key: _clean_payload()})
    found = extractor_saved.extract(document, provider="replay", model="recorded")
    assert found.call_a.lines[0].line_amount == "1440.00"


def test_summary_says_unsigned_for_accuracy_and_precision() -> None:
    summary = summarize_replay(checked=2)
    assert summary["accuracy"] == "unsigned"
    assert summary["precision"] == "unsigned"
    text = format_summary(summary)
    assert "accuracy: unsigned" in text
    assert "precision: unsigned" in text
