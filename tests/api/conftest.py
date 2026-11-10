"""Scenario-specific copies of the owner ruleset, and totals field checks.

A caller names the catalog entries, rules, and rates a scenario uses.
Those entries alone change review or signature metadata. Every other
copied line stays as it is in rules/, including PLACEHOLDER rules.
"""

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml

from app.domain.ruleset import (
    catalog_is_signed,
    load_ruleset,
    rate_is_active,
    rule_is_active,
)

_ROOT = Path(__file__).resolve().parents[2]
_REVIEWER = "AF"
_FORBIDDEN_TOTAL_FIELDS = frozenset({"net", "savings", "recovered"})
_METADATA_KEYS = frozenset({"review_status", "reviewed_by", "status", "signed_off_by"})

__all__ = [
    "assert_refund_candidates_stay_separate",
    "copy_scenario_rules",
    "forbidden_total_field_names",
]


@pytest.fixture(autouse=True)
def _reset_csrf_token() -> Iterator[None]:
    from tests.api.test_flow import reset_csrf

    reset_csrf()
    yield
    reset_csrf()


def forbidden_total_field_names(payload: object) -> list[str]:
    """Field names net, savings, and recovered anywhere in the tree.

    The label value "not recovered cash" is not a field name.
    """
    found: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if _is_forbidden_field(str(key)):
                    found.append(str(key))
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return found


def assert_refund_candidates_stay_separate(totals: dict[str, object]) -> None:
    candidates = totals["refund_candidates"]
    assert isinstance(candidates, dict)
    assert candidates.get("label") == "not recovered cash"
    forbidden = forbidden_total_field_names(totals)
    assert not forbidden, forbidden


def copy_scenario_rules(
    dest: Path,
    *,
    signed_categories: frozenset[str],
    reviewed_rule_ids: frozenset[str],
    reviewed_rates: frozenset[tuple[str, str]],
) -> None:
    if any(rule_id.startswith("PLACEHOLDER:") for rule_id in reviewed_rule_ids):
        raise AssertionError("PLACEHOLDER rules stay inactive")
    dest.mkdir()
    _patch_rules(dest, reviewed_rule_ids)
    _patch_rates(dest, reviewed_rates)
    _patch_catalog(dest, signed_categories)
    _assert_line_edits(
        _ROOT / "rules" / "entries.yaml",
        dest / "entries.yaml",
        2 * len(reviewed_rule_ids),
    )
    _assert_line_edits(
        _ROOT / "rules" / "rates.yaml",
        dest / "rates.yaml",
        2 * len(reviewed_rates),
    )
    _assert_line_edits(
        _ROOT / "rules" / "catalog.yaml",
        dest / "catalog.yaml",
        2 * len(signed_categories),
    )
    _assert_metadata_only(
        _ROOT / "rules" / "entries.yaml",
        dest / "entries.yaml",
        list_key="rules",
        identity=lambda item: item["id"],
        meta=frozenset({"review_status", "reviewed_by"}),
        selected=frozenset(reviewed_rule_ids),
        status_key="review_status",
        status_value="reviewed",
        actor_key="reviewed_by",
        actor_value=_REVIEWER,
    )
    _assert_metadata_only(
        _ROOT / "rules" / "rates.yaml",
        dest / "rates.yaml",
        list_key="rates",
        identity=lambda item: (item["jurisdiction"], item["location"]),
        meta=frozenset({"review_status", "reviewed_by"}),
        selected=frozenset(reviewed_rates),
        status_key="review_status",
        status_value="reviewed",
        actor_key="reviewed_by",
        actor_value=_REVIEWER,
    )
    _assert_metadata_only(
        _ROOT / "rules" / "catalog.yaml",
        dest / "catalog.yaml",
        list_key="entries",
        identity=lambda item: item["category"],
        meta=frozenset({"status", "signed_off_by"}),
        selected=frozenset(signed_categories),
        status_key="status",
        status_value="signed",
        actor_key="signed_off_by",
        actor_value=_REVIEWER,
    )
    _assert_loader(dest, signed_categories, reviewed_rule_ids, reviewed_rates)


def _is_forbidden_field(key: str) -> bool:
    normalized = _normalize_field_name(key)
    return any(
        part in _FORBIDDEN_TOTAL_FIELDS for part in normalized.split("_") if part
    )


def _normalize_field_name(key: str) -> str:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip())
    return spaced.lower().replace("-", "_").replace(" ", "_")


def _patch_rules(dest: Path, rule_ids: frozenset[str]) -> None:
    source = _ROOT / "rules" / "entries.yaml"
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    current_id: str | None = None
    touched: dict[str, set[str]] = {}
    written: list[str] = []
    for line in lines:
        stripped = line.strip()
        field = _field(stripped)
        if field == "id" and stripped.startswith("-"):
            current_id = _scalar(stripped)
        selected = current_id in rule_ids if current_id is not None else False
        if selected and field in {"review_status", "reviewed_by"} and current_id:
            value = "reviewed" if field == "review_status" else _REVIEWER
            line = _set_scalar(line, value)
            touched.setdefault(current_id, set()).add(field)
        written.append(line)
    missing = {
        rule_id
        for rule_id in rule_ids
        if touched.get(rule_id) != {"review_status", "reviewed_by"}
    }
    if missing:
        raise AssertionError(f"rules not updated: {sorted(missing)}")
    (dest / "entries.yaml").write_text("".join(written), encoding="utf-8")


def _patch_rates(dest: Path, rates: frozenset[tuple[str, str]]) -> None:
    source = _ROOT / "rules" / "rates.yaml"
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    jurisdiction: str | None = None
    location: str | None = None
    touched: dict[tuple[str, str], set[str]] = {}
    written: list[str] = []
    for line in lines:
        stripped = line.strip()
        field = _field(stripped)
        if field == "jurisdiction" and stripped.startswith("-"):
            jurisdiction = _scalar(stripped)
            location = None
        elif field == "location":
            location = _scalar(stripped)
        key = (
            (jurisdiction, location)
            if jurisdiction is not None and location is not None
            else None
        )
        selected = key in rates if key is not None else False
        if selected and field in {"review_status", "reviewed_by"} and key is not None:
            value = "reviewed" if field == "review_status" else _REVIEWER
            line = _set_scalar(line, value)
            touched.setdefault(key, set()).add(field)
        written.append(line)
    missing = {
        rate for rate in rates if touched.get(rate) != {"review_status", "reviewed_by"}
    }
    if missing:
        raise AssertionError(f"rates not updated: {sorted(missing)}")
    (dest / "rates.yaml").write_text("".join(written), encoding="utf-8")


def _patch_catalog(dest: Path, categories: frozenset[str]) -> None:
    source = _ROOT / "rules" / "catalog.yaml"
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    category: str | None = None
    touched: dict[str, set[str]] = {}
    written: list[str] = []
    for line in lines:
        stripped = line.strip()
        field = _field(stripped)
        if field == "category" and stripped.startswith("-"):
            category = _scalar(stripped)
        selected = category in categories if category is not None else False
        if selected and field in {"status", "signed_off_by"} and category is not None:
            value = "signed" if field == "status" else _REVIEWER
            line = _set_scalar(line, value)
            touched.setdefault(category, set()).add(field)
        written.append(line)
    missing = {
        name for name in categories if touched.get(name) != {"status", "signed_off_by"}
    }
    if missing:
        raise AssertionError(f"catalog entries not updated: {sorted(missing)}")
    (dest / "catalog.yaml").write_text("".join(written), encoding="utf-8")


def _assert_line_edits(source: Path, dest: Path, expected: int) -> None:
    before = source.read_text(encoding="utf-8").splitlines()
    after = dest.read_text(encoding="utf-8").splitlines()
    assert len(before) == len(after)
    changed = 0
    for left, right in zip(before, after, strict=True):
        if left == right:
            continue
        changed += 1
        key = left.split(":", 1)[0].strip()
        if key.startswith("-"):
            key = key[1:].strip()
        assert key in _METADATA_KEYS, key
        assert left.split(":", 1)[0] == right.split(":", 1)[0]
    # Owner-reviewed rows already match, so a scenario may rewrite fewer lines.
    assert changed <= expected


def _assert_metadata_only(
    source: Path,
    dest: Path,
    *,
    list_key: str,
    identity: Callable[[dict[str, object]], object],
    meta: frozenset[str],
    selected: frozenset[object],
    status_key: str,
    status_value: str,
    actor_key: str,
    actor_value: str,
) -> None:
    before = _rows(source, list_key)
    after = _rows(dest, list_key)
    assert len(before) == len(after)
    seen: set[object] = set()
    for original, updated in zip(before, after, strict=True):
        ident = identity(original)
        assert identity(updated) == ident
        seen.add(ident)
        assert _without(original, meta) == _without(updated, meta)
        if ident in selected:
            assert updated.get(status_key) == status_value
            assert updated.get(actor_key) == actor_value
        else:
            for key in meta:
                assert updated.get(key) == original.get(key)
    missing = set(selected) - seen
    assert not missing, missing


def _assert_loader(
    dest: Path,
    signed_categories: frozenset[str],
    reviewed_rule_ids: frozenset[str],
    reviewed_rates: frozenset[tuple[str, str]],
) -> None:
    ruleset = load_ruleset(dest)
    source = load_ruleset(_ROOT / "rules")
    source_rules = {rule.id: rule for rule in source.rules}
    source_rates = {(rate.jurisdiction, rate.location): rate for rate in source.rates}
    source_catalog = {entry.category: entry for entry in source.catalog}
    for rule in ruleset.rules:
        original = source_rules[rule.id]
        if rule.id in reviewed_rule_ids:
            assert rule.review_status == "reviewed"
            assert rule.reviewed_by == _REVIEWER
            assert rule_is_active(rule)
        else:
            assert rule.review_status == original.review_status
            assert rule.reviewed_by == original.reviewed_by
            assert rule_is_active(rule) == rule_is_active(original)
    for rate in ruleset.rates:
        key = (rate.jurisdiction, rate.location)
        original_rate = source_rates[key]
        if key in reviewed_rates:
            assert rate.review_status == "reviewed"
            assert rate.reviewed_by == _REVIEWER
            assert rate_is_active(rate)
        else:
            assert rate.review_status == original_rate.review_status
            assert rate.reviewed_by == original_rate.reviewed_by
            assert rate_is_active(rate) == rate_is_active(original_rate)
    for entry in ruleset.catalog:
        original_entry = source_catalog[entry.category]
        if entry.category in signed_categories:
            assert entry.status == "signed"
            assert entry.signed_off_by == _REVIEWER
            assert catalog_is_signed(entry)
        else:
            assert entry.status == original_entry.status
            assert entry.signed_off_by == original_entry.signed_off_by
            assert catalog_is_signed(entry) == catalog_is_signed(original_entry)
    placeholder = next(
        rule for rule in ruleset.rules if rule.id == "PLACEHOLDER:CA-DP-001"
    )
    original_placeholder = source_rules["PLACEHOLDER:CA-DP-001"]
    assert placeholder.review_status == original_placeholder.review_status
    assert placeholder.reviewed_by == original_placeholder.reviewed_by
    assert not rule_is_active(placeholder)


def _rows(path: Path, key: str) -> list[dict[str, object]]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"{path.name} must be a mapping")
    rows = loaded.get(key)
    if not isinstance(rows, list):
        raise AssertionError(f"{path.name} missing {key}")
    parsed: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AssertionError(f"{path.name} entry must be a mapping")
        parsed.append(row)
    return parsed


def _without(item: dict[str, object], keys: frozenset[str]) -> dict[str, object]:
    return {key: value for key, value in item.items() if key not in keys}


def _field(stripped: str) -> str:
    head, _separator, _value = stripped.partition(":")
    head = head.strip()
    if head.startswith("-"):
        head = head[1:].strip()
    return head


def _scalar(stripped: str) -> str:
    value = stripped.partition(":")[2].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _set_scalar(line: str, value: str) -> str:
    ending = "\n" if line.endswith("\n") else ""
    body = line[:-1] if ending else line
    key, separator, _old = body.partition(":")
    if separator == "":
        raise AssertionError(f"expected a YAML field: {line!r}")
    return f"{key}: {value}{ending}"
