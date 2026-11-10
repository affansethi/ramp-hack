"""Load rule YAML. Candidate and PLACEHOLDER entries stay inactive.

A rule or rate is active only when ``review_status`` is verified and its
evidence passes the content-verification checks. A catalog entry is active
only when ``status`` is verified. ``reviewed_by`` and ``signed_off_by`` are
not activation inputs.
"""

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Literal, cast

import yaml

Calculation = Literal["not_taxable", "net", "net_plus_shipping"]
AppliesWhen = Literal["any", "single_state", "multi_state"]
TimingBasis = Literal["invoice_date", "service_period_start", "delivery_date"]


@dataclass(frozen=True)
class RuleEntry:
    id: str
    jurisdiction: str
    category: str
    treatment: str
    calculation: Calculation
    applies_when: AppliesWhen
    effective_from: date
    effective_to: date | None
    timing_basis: TimingBasis
    primary_source_url: str
    review_status: str
    reviewed_by: str | None
    reviewed_at: str | None
    evidence_ok: bool = False


@dataclass(frozen=True)
class RateEntry:
    jurisdiction: str
    location: str
    location_code: str
    rate: str
    quarter: str
    source_url: str
    review_status: str
    reviewed_by: str | None
    evidence_ok: bool = False


@dataclass(frozen=True)
class CatalogEntry:
    category: str
    required_keys: tuple[str, ...]
    status: str
    signed_off_by: str | None


@dataclass(frozen=True)
class Ruleset:
    rules: tuple[RuleEntry, ...]
    rates: tuple[RateEntry, ...]
    catalog: tuple[CatalogEntry, ...]
    version: str


def is_placeholder(rule_id: str) -> bool:
    """True for PLACEHOLDER: ids, including a rate id that embeds the prefix."""
    return "PLACEHOLDER:" in rule_id


def rate_item_id(jurisdiction: str, location_code: str, quarter: str) -> str:
    return f"rate-{jurisdiction}-{location_code}-{quarter}"


def catalog_item_id(category: str) -> str:
    return f"catalog-{category}"


def rule_is_active(rule: RuleEntry) -> bool:
    return (
        rule.review_status == "verified"
        and rule.evidence_ok
        and bool(rule.primary_source_url.strip())
        and not is_placeholder(rule.id)
    )


def rate_is_active(rate: RateEntry) -> bool:
    item_id = rate_item_id(rate.jurisdiction, rate.location_code, rate.quarter)
    return (
        rate.review_status == "verified"
        and rate.evidence_ok
        and bool(rate.rate.strip())
        and not is_placeholder(item_id)
    )


def catalog_is_verified(entry: CatalogEntry) -> bool:
    return entry.status == "verified" and not is_placeholder(
        catalog_item_id(entry.category)
    )


def _evidence_ok(
    directory: Path, item_id: str, raw: dict[str, object], kind: str
) -> bool:
    if is_placeholder(item_id):
        return False
    from app.domain.content_verification import evidence_passes

    return evidence_passes(directory, item_id, raw, kind=kind)


def _raw_mapping(item: dict[str, object]) -> dict[str, object]:
    return {str(key): value for key, value in item.items()}


def load_ruleset(directory: Path) -> Ruleset:
    rules = _load_rules(directory / "entries.yaml")
    rates = _load_rates(directory / "rates.yaml")
    catalog = _load_catalog(directory / "catalog.yaml")
    blob = "\n".join(
        path.read_text(encoding="utf-8") if path.exists() else ""
        for path in (
            directory / "entries.yaml",
            directory / "rates.yaml",
            directory / "catalog.yaml",
        )
    )
    version = sha256(blob.encode("utf-8")).hexdigest()[:16]
    return Ruleset(
        rules=tuple(rules), rates=tuple(rates), catalog=tuple(catalog), version=version
    )


def _read(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} must be a mapping")
    return loaded


def _as_date(value: object, label: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"{label} must be an ISO date")


def _optional_date(value: object, label: str) -> date | None:
    if value is None or value == "":
        return None
    return _as_date(value, label)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a string")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional text field must be a string")
    stripped = value.strip()
    return stripped or None


def _load_rules(path: Path) -> list[RuleEntry]:
    raw_rules = _read(path).get("rules", [])
    if not isinstance(raw_rules, list):
        raise ValueError("rules must be a list")
    entries: list[RuleEntry] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            raise ValueError("rule entry must be a mapping")
        calculation_raw = item.get("calculation")
        if calculation_raw not in ("not_taxable", "net", "net_plus_shipping"):
            raise ValueError(f"rule {item.get('id')} has an unknown calculation")
        applies_raw = item.get("applies_when", "any")
        if applies_raw not in ("any", "single_state", "multi_state"):
            raise ValueError(f"rule {item.get('id')} has an unknown applies_when")
        timing_raw = item.get("timing_basis")
        if timing_raw not in ("invoice_date", "service_period_start", "delivery_date"):
            raise ValueError(f"rule {item.get('id')} has an unknown timing_basis")
        rule_id = _text(item.get("id"), "id")
        review_status = _text(item.get("review_status"), "review_status")
        raw_values = _raw_mapping(item)
        entries.append(
            RuleEntry(
                id=rule_id,
                jurisdiction=_text(item.get("jurisdiction"), "jurisdiction"),
                category=_text(item.get("category"), "category"),
                treatment=_text(item.get("treatment"), "treatment"),
                calculation=cast(Calculation, calculation_raw),
                applies_when=cast(AppliesWhen, applies_raw),
                effective_from=_as_date(item.get("effective_from"), "effective_from"),
                effective_to=_optional_date(item.get("effective_to"), "effective_to"),
                timing_basis=cast(TimingBasis, timing_raw),
                primary_source_url=_text(
                    item.get("primary_source_url"), "primary_source_url"
                ),
                review_status=review_status,
                reviewed_by=_optional_text(item.get("reviewed_by")),
                reviewed_at=_optional_text(item.get("reviewed_at")),
                evidence_ok=(
                    _evidence_ok(path.parent, rule_id, raw_values, "rule")
                    if review_status == "verified"
                    else False
                ),
            )
        )
    return entries


def _load_rates(path: Path) -> list[RateEntry]:
    raw_rates = _read(path).get("rates", [])
    if not isinstance(raw_rates, list):
        raise ValueError("rates must be a list")
    entries: list[RateEntry] = []
    for item in raw_rates:
        if not isinstance(item, dict):
            raise ValueError("rate entry must be a mapping")
        rate = item.get("rate")
        if isinstance(rate, float):
            raise ValueError("rate must be a decimal string, never a binary float")
        jurisdiction = _text(item.get("jurisdiction"), "jurisdiction")
        location_code = _text(item.get("location_code"), "location_code")
        quarter = _text(item.get("quarter"), "quarter")
        review_status = _text(item.get("review_status"), "review_status")
        raw_values = _raw_mapping(item)
        item_id = rate_item_id(jurisdiction, location_code, quarter)
        entries.append(
            RateEntry(
                jurisdiction=jurisdiction,
                location=_text(item.get("location"), "location"),
                location_code=location_code,
                rate=_text(rate, "rate"),
                quarter=quarter,
                source_url=_text(item.get("source_url"), "source_url"),
                review_status=review_status,
                reviewed_by=_optional_text(item.get("reviewed_by")),
                evidence_ok=(
                    _evidence_ok(path.parent, item_id, raw_values, "rate")
                    if review_status == "verified"
                    else False
                ),
            )
        )
    return entries


def _load_catalog(path: Path) -> list[CatalogEntry]:
    raw_entries = _read(path).get("entries", [])
    if not isinstance(raw_entries, list):
        raise ValueError("catalog entries must be a list")
    entries: list[CatalogEntry] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            raise ValueError("catalog entry must be a mapping")
        keys = item.get("required_keys")
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise ValueError("required_keys must be a list of strings")
        entries.append(
            CatalogEntry(
                category=_text(item.get("category"), "category"),
                required_keys=tuple(keys),
                status=_text(item.get("status"), "status"),
                signed_off_by=_optional_text(item.get("signed_off_by")),
            )
        )
    return entries
