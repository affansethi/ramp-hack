"""Replay extraction. A miss or a forbidden field writes nothing."""

import json
import re
from decimal import InvalidOperation
from hashlib import sha256
from typing import Protocol

from pydantic import ValidationError

from app.contracts.extraction import CallA, CallB, CheckedExtraction, LineExtraction
from app.domain.moneyutil import parse_decimal

SCHEMA_VERSION = "v2"
PROMPT_VERSION = "v2"
TIMEOUT_SECONDS = 45
FORBIDDEN_KEYS = frozenset(
    {
        "rate",
        "expected_tax",
        "amount_owed",
        "label",
        "direction",
        "hold",
        "approval",
        "tax_jurisdiction_assignment",
        "location_code_assignment",
        "legal_conclusion",
        "treatment",
        "verdict",
    }
)


class ForbiddenField(ValueError):
    code = "FORBIDDEN_FIELD"

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class SchemaError(ValueError):
    """The payload is not the extraction schema. Not a tax decision."""


class ExtractionMiss(LookupError):
    """Replay has no saved payload, or a bounded call gave up."""


class ProviderTimeout(TimeoutError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


class Clock(Protocol):
    def sleep(self, seconds: float) -> None:
        """Advance or wait. Tests use a fake clock."""


class ModelProvider(Protocol):
    def complete(self, document: bytes, *, timeout: float) -> str:
        """Return one model payload. Tests use a fake. No network here."""


def cache_key(
    document: bytes,
    *,
    provider: str,
    model: str,
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    digest = sha256(document).hexdigest()
    return f"{digest}:{provider}:{model}:{prompt_version}:{schema_version}"


def parse_extraction(payload: object) -> CheckedExtraction:
    if not isinstance(payload, dict):
        raise SchemaError("payload")
    _reject_forbidden(payload)
    call_a_raw = payload.get("call_a", {})
    call_b_raw = payload.get("call_b", {})
    if not isinstance(call_a_raw, dict) or not isinstance(call_b_raw, dict):
        raise SchemaError("payload")
    lines = _lines(call_a_raw.get("lines", []))
    call_a = CallA(
        vendor_name=_optional_text(call_a_raw.get("vendor_name")),
        invoice_date=_optional_text(call_a_raw.get("invoice_date")),
        currency=_optional_text(call_a_raw.get("currency")),
        bill_to=_optional_text(call_a_raw.get("bill_to")),
        ship_to=_optional_text(call_a_raw.get("ship_to")),
        lines=lines,
        evidence=call_a_raw.get("evidence", []),
    )
    call_b = CallB.model_validate(call_b_raw)
    return CheckedExtraction(call_a=call_a, call_b=call_b)


class ReplayExtractor:
    """Serves saved payloads. A miss raises and does not invent a tax line."""

    def __init__(self, saved: dict[str, object] | None = None) -> None:
        self.saved = {} if saved is None else saved

    def extract(
        self,
        document: bytes,
        *,
        provider: str,
        model: str,
        prompt_version: str = PROMPT_VERSION,
        schema_version: str = SCHEMA_VERSION,
    ) -> CheckedExtraction:
        key = cache_key(
            document,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )
        if key not in self.saved:
            raise ExtractionMiss(key)
        return parse_extraction(self.saved[key])


def extract_bounded(
    provider: ModelProvider,
    document: bytes,
    clock: Clock,
) -> CheckedExtraction:
    """One schema repair, then two transport retries. Then a miss. No facts written."""
    repair_used = False
    transport_retries = 0
    while True:
        try:
            text = provider.complete(document, timeout=TIMEOUT_SECONDS)
        except ProviderTimeout as exc:
            if transport_retries >= 2:
                raise ExtractionMiss("timeout") from exc
            clock.sleep(exc.retry_after)
            transport_retries += 1
            continue
        try:
            return parse_extraction(json.loads(text))
        except ForbiddenField:
            raise
        except (
            json.JSONDecodeError,
            SchemaError,
            ValidationError,
            InvalidOperation,
        ) as exc:
            if repair_used:
                raise ExtractionMiss("schema") from exc
            repair_used = True


def _lines(raw: object) -> list[LineExtraction]:
    if not isinstance(raw, list):
        raise SchemaError("lines")
    lines: list[LineExtraction] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SchemaError("lines")
        present = "tax_charged" in item
        value = item.get("tax_charged") if present else None
        if present and value is not None and not isinstance(value, str):
            raise SchemaError("tax_charged")
        if isinstance(value, str):
            try:
                parse_decimal(value)
            except (ValueError, InvalidOperation) as exc:
                raise SchemaError("tax_charged") from exc
        payload = {key: item[key] for key in item if key != "tax_charged"}
        line = LineExtraction.model_validate(payload)
        lines.append(
            line.model_copy(
                update={"tax_charged": value, "tax_charged_present": present}
            )
        )
    return lines


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError("text")
    return value


def _reject_forbidden(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _normalize_key(str(key)) in FORBIDDEN_KEYS:
                raise ForbiddenField(str(key))
            _reject_forbidden(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden(item)


def _normalize_key(key: str) -> str:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip())
    return spaced.lower().replace("-", "_").replace(" ", "_")
