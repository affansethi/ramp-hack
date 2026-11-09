"""Replay eval harness without scoring unsigned goldens.

Runs the DocumentExtractor checker (``parse_extraction`` / ``ReplayExtractor``)
in replay mode only: a forbidden tax-decision key fails, a clean printed-invoice
payload passes, and a replay miss fails without calling a provider. Accuracy and
precision stay ``"unsigned"`` because the owner-signed golden set does not exist
yet; this harness never invents precision, recall, or an expected tax.
"""

from typing import Any

import pytest

import app.adapters.extractor as extractor_module
from app.adapters.extractor import (
    CheckedExtraction,
    ExtractionMiss,
    ForbiddenField,
    ModelProvider,
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


def report_summary(summary: dict[str, str]) -> str:
    """Print the harness summary to the terminal and return the emitted text."""
    text = "replay eval: " + format_summary(summary)
    print(text)
    return text


def eval_terminal_line(*, checked: int) -> str:
    """The unsigned line the eval session summary emits (see conftest.py)."""
    return "replay eval: " + format_summary(summarize_replay(checked=checked))


def evaluate_replay(
    document: bytes,
    saved: dict[str, object],
    *,
    provider: ModelProvider,
    model: str = "recorded",
) -> CheckedExtraction:
    """Replay-only eval entry point.

    Serves the saved payload for ``document`` or raises ``ExtractionMiss``.
    The live ``provider`` is accepted so eval callers can thread one provider
    through both live and replay runs, but replay never touches it: a miss
    fails instead of falling back to ``extract_bounded``.
    """
    del provider  # replay must not call a provider; the miss test proves it.
    extractor = ReplayExtractor(saved)
    return extractor.extract(document, provider="replay", model=model)


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


def test_replay_miss_fails_and_does_not_call_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _NoCallProvider()
    fallback_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _fail_fallback(*args: object, **kwargs: object) -> CheckedExtraction:
        fallback_calls.append((args, kwargs))
        raise AssertionError("replay must not fall back to a live provider call")

    monkeypatch.setattr(extractor_module, "extract_bounded", _fail_fallback)
    document = b"synthetic-invoice-bytes-issue-39"
    with pytest.raises(ExtractionMiss):
        evaluate_replay(document, {}, provider=provider)
    assert provider.calls == 0
    assert fallback_calls == []
    other_key = cache_key(document, provider="replay", model="recorded")
    found = evaluate_replay(document, {other_key: _clean_payload()}, provider=provider)
    assert found.call_a.lines[0].line_amount == "1440.00"
    assert provider.calls == 0
    assert fallback_calls == []


def test_summary_says_unsigned_for_accuracy_and_precision(
    capsys: pytest.CaptureFixture[str],
) -> None:
    summary = summarize_replay(checked=2)
    assert summary["accuracy"] == "unsigned"
    assert summary["precision"] == "unsigned"
    text = report_summary(summary)
    emitted = capsys.readouterr().out
    assert "accuracy: unsigned" in emitted
    assert "precision: unsigned" in emitted
    assert text in emitted


def test_terminal_summary_hook_reports_unsigned() -> None:
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    path = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("issue39_eval_conftest", str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reports = [
        SimpleNamespace(nodeid="tests/eval/test_replay_eval.py::test_first"),
        SimpleNamespace(nodeid="tests/eval/test_replay_eval.py::test_second"),
        SimpleNamespace(nodeid="tests/unit/test_other.py::test_third"),
    ]

    class _Terminal:
        def __init__(self) -> None:
            self.stats = {"passed": reports}
            self.lines: list[str] = []

        def write_line(self, text: str) -> None:
            self.lines.append(text)

    terminal = _Terminal()
    module.pytest_terminal_summary(terminal, 0, None)
    assert len(terminal.lines) == 1
    assert "accuracy: unsigned" in terminal.lines[0]
    assert "precision: unsigned" in terminal.lines[0]
    assert "checked 2" in terminal.lines[0]
