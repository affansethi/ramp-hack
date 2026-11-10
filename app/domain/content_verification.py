"""Agent verification checks for tax content.

An item marked verified is active only when this module's checks pass.
`make verify-content` fails the tree when any such item is short of the
protocol in docs/content-verification.md. Candidate items are ignored.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

import yaml

from app.domain.ruleset import catalog_item_id, is_placeholder, rate_item_id

_METADATA = frozenset(
    {
        "review_status",
        "reviewed_by",
        "reviewed_at",
        "signed_off_by",
        "status",
    }
)
_VERIFIER_META = frozenset(
    {
        "item",
        "lane",
        "model",
        "role",
        "notes",
        "issue",
        "date",
        "blind",
        "conclusion",
        "attestations",
        "values",
    }
)
_AUTHORITIES = frozenset({"statute", "regulation", "agency_guidance", "rate_table"})
_ROLES = frozenset({"research", "verify", "tiebreak"})
_CONCLUSIONS = frozenset({"agree", "disagree"})
_FAMILIES = ("gemini", "claude", "grok", "muse", "gpt")


@dataclass(frozen=True)
class _Item:
    item_id: str
    kind: str
    values: dict[str, object]


def model_family(model: str) -> str:
    """Family is the model id prefix. Codex `default` counts as gpt."""
    token = model.strip().lower()
    if "/" in token:
        token = token.rsplit("/", 1)[-1]
    if token == "default":
        return "gpt"
    for family in _FAMILIES:
        if token == family:
            return family
        if not token.startswith(family):
            continue
        rest = token[len(family) :]
        if rest[:1] in {"-", ".", "_"} or rest[:1].isdigit():
            return family
    prefix = token.split("-", 1)[0].split(".", 1)[0]
    if prefix == "default":
        return "gpt"
    return prefix


def evidence_stem(item_id: str) -> str:
    """File stem for an item id. Slashes in location codes stay one file."""
    return item_id.replace("/", "_").replace("\\", "_")


def substantive_values(values: Mapping[str, object]) -> dict[str, object]:
    """Fields a verifier must match. Activation metadata is not a value."""
    return {key: _canon(item) for key, item in values.items() if key not in _METADATA}


def verify_content(root: Path) -> list[str]:
    """Return one error string per problem. Empty means the tree passes."""
    rules_dir = root / "rules"
    items, errors = _scan(root)
    allow_path = rules_dir / "evidence" / "allowlist.yaml"
    domains: set[str] | None
    if allow_path.exists():
        domains, allow_errors = _load_allowlist(allow_path)
        errors.extend(allow_errors)
    else:
        domains = None
    for item in items:
        if is_placeholder(item.item_id):
            errors.append(
                _err(
                    "PLACEHOLDER_ACTIVE",
                    item.item_id,
                    "PLACEHOLDER ids never activate",
                )
            )
            continue
        errors.extend(
            _check_item(rules_dir, root / "fixtures" / "goldens", item, domains)
        )
    return errors


def evidence_passes(
    rules_dir: Path,
    item_id: str,
    values: Mapping[str, object],
    *,
    kind: str,
) -> bool:
    """True when one verified rule or rate has passing evidence."""
    if is_placeholder(item_id):
        return False
    allow_path = rules_dir / "evidence" / "allowlist.yaml"
    if not allow_path.exists():
        return False
    domains, allow_errors = _load_allowlist(allow_path)
    if allow_errors:
        return False
    item = _Item(item_id=item_id, kind=kind, values=dict(values))
    return not _check_item(
        rules_dir, rules_dir.parent / "fixtures" / "goldens", item, domains
    )


def _check_item(
    rules_dir: Path,
    goldens_dir: Path,
    item: _Item,
    domains: set[str] | None,
) -> list[str]:
    errors, verify_lanes = _check_evidence(rules_dir / "evidence", item, domains)
    errors.extend(_check_verifier(rules_dir / "evidence", item, verify_lanes))
    if item.kind == "golden":
        errors.extend(_check_derivations(goldens_dir, item))
    return errors


def _check_evidence(
    evidence_dir: Path,
    item: _Item,
    domains: set[str] | None,
) -> tuple[list[str], list[str]]:
    path = _evidence_file(evidence_dir, item.item_id)
    if path is None:
        return (
            [_err("EVIDENCE_SCHEMA", item.item_id, "item id is not a safe file name")],
            [],
        )
    if not path.is_file():
        return (
            [_err("MISSING_EVIDENCE", item.item_id, f"missing {path.name}")],
            [],
        )
    loaded, parse_error = _load_yaml(path)
    if parse_error is not None:
        return ([_err("EVIDENCE_PARSE", item.item_id, parse_error)], [])
    doc = _as_dict(loaded)
    if doc is None:
        return (
            [_err("EVIDENCE_PARSE", item.item_id, "evidence must be a mapping")],
            [],
        )
    errors: list[str] = []
    if doc.get("item") != item.item_id:
        errors.append(
            _err("EVIDENCE_SCHEMA", item.item_id, "evidence item id does not match")
        )
    errors.extend(_check_claims(evidence_dir, item, doc.get("claims"), domains))
    attestation_errors, verify_lanes = _check_attestations(
        item.item_id, doc.get("attestations")
    )
    errors.extend(attestation_errors)
    return errors, verify_lanes


def _check_claims(
    evidence_dir: Path,
    item: _Item,
    raw: object,
    domains: set[str] | None,
) -> list[str]:
    claims = _as_list(raw)
    if not claims:
        return [
            _err("EVIDENCE_SCHEMA", item.item_id, "evidence needs at least one claim")
        ]
    errors: list[str] = []
    saw_rate_table = False
    for claim in claims:
        claim_map = _as_dict(claim)
        if claim_map is None:
            errors.append(
                _err("EVIDENCE_SCHEMA", item.item_id, "claim must be a mapping")
            )
            continue
        if not _nonempty(claim_map.get("field")):
            errors.append(
                _err("EVIDENCE_SCHEMA", item.item_id, "claim field is missing")
            )
        if "value" not in claim_map:
            errors.append(
                _err("EVIDENCE_SCHEMA", item.item_id, "claim value is missing")
            )
        sources = _as_list(claim_map.get("sources"))
        if not sources:
            errors.append(
                _err(
                    "URL_NOT_ALLOWLISTED",
                    item.item_id,
                    "claim has no allowlisted source",
                )
            )
            continue
        allowlisted = False
        for source in sources:
            source_errors, host_ok, rate_table = _check_source(
                evidence_dir, item.item_id, source, domains
            )
            errors.extend(source_errors)
            allowlisted = allowlisted or host_ok
            saw_rate_table = saw_rate_table or (host_ok and rate_table)
        if not allowlisted:
            errors.append(
                _err(
                    "URL_NOT_ALLOWLISTED",
                    item.item_id,
                    "claim has no source on an allowlisted domain",
                )
            )
    if item.kind == "rate" and not saw_rate_table:
        quarter = item.values.get("quarter")
        errors.append(
            _err(
                "RATE_TABLE",
                item.item_id,
                f"rate needs an allowlisted rate_table source for {quarter}",
            )
        )
    return errors


def _check_source(
    evidence_dir: Path,
    item_id: str,
    raw: object,
    domains: set[str] | None,
) -> tuple[list[str], bool, bool]:
    source = _as_dict(raw)
    if source is None:
        return (
            [_err("EVIDENCE_SCHEMA", item_id, "source must be a mapping")],
            False,
            False,
        )
    errors: list[str] = []
    for label in ("pinpoint", "retrieved_at"):
        if not _nonempty(source.get(label)):
            errors.append(
                _err("EVIDENCE_SCHEMA", item_id, f"source {label} is missing")
            )
    authority = source.get("authority")
    rate_table = authority == "rate_table"
    if authority not in _AUTHORITIES:
        errors.append(
            _err("EVIDENCE_SCHEMA", item_id, "source authority is not a known kind")
        )
    url = source.get("url")
    host_ok = False
    if not isinstance(url, str):
        errors.append(_err("EVIDENCE_SCHEMA", item_id, "source url must be a string"))
    else:
        host = _host(url)
        if domains is None:
            errors.append(
                _err(
                    "ALLOWLIST",
                    item_id,
                    "rules/evidence/allowlist.yaml is missing",
                )
            )
        elif host is not None and _host_allowed(host, domains):
            host_ok = True
    quote = source.get("quote")
    snapshot = _snapshot_file(evidence_dir, source.get("snapshot"))
    digest = source.get("snapshot_sha256")
    if not isinstance(quote, str) or not quote.strip():
        errors.append(_err("EVIDENCE_SCHEMA", item_id, "source quote is missing"))
    elif snapshot is None:
        errors.append(
            _err(
                "EVIDENCE_SCHEMA",
                item_id,
                "snapshot path is missing or escapes evidence",
            )
        )
    elif not snapshot.is_file():
        errors.append(
            _err("QUOTE_NOT_IN_SNAPSHOT", item_id, "snapshot file is missing")
        )
    else:
        try:
            text = snapshot.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(
                _err("QUOTE_NOT_IN_SNAPSHOT", item_id, "snapshot is not utf-8 text")
            )
        else:
            if _normalize_ws(quote) not in _normalize_ws(text):
                errors.append(
                    _err(
                        "QUOTE_NOT_IN_SNAPSHOT",
                        item_id,
                        "quote does not appear in the snapshot",
                    )
                )
    if not isinstance(digest, str) or not digest.strip():
        errors.append(_err("EVIDENCE_SCHEMA", item_id, "snapshot_sha256 is missing"))
    elif snapshot is None or not snapshot.is_file():
        errors.append(
            _err("SNAPSHOT_HASH_MISMATCH", item_id, "snapshot file is missing")
        )
    else:
        actual = sha256(snapshot.read_bytes()).hexdigest()
        if actual != digest.strip().lower():
            errors.append(
                _err(
                    "SNAPSHOT_HASH_MISMATCH",
                    item_id,
                    "snapshot bytes do not match snapshot_sha256",
                )
            )
    return errors, host_ok, rate_table


def _check_attestations(item_id: str, raw: object) -> tuple[list[str], list[str]]:
    rows = _as_list(raw)
    if not rows:
        return (
            [
                _err(
                    "ATTESTATIONS",
                    item_id,
                    "need a blind research attestation and a blind verify attestation",
                )
            ],
            [],
        )
    errors: list[str] = []
    research: list[tuple[str, str]] = []
    verify: list[tuple[str, str]] = []
    for row in rows:
        attestation = _as_dict(row)
        if attestation is None:
            errors.append(
                _err("EVIDENCE_SCHEMA", item_id, "attestation must be a mapping")
            )
            continue
        parsed = _parse_attestation(item_id, attestation)
        if isinstance(parsed, str):
            errors.append(parsed)
            continue
        role, lane, family, blind, conclusion = parsed
        if conclusion != "agree":
            errors.append(_err("ATTESTATIONS", item_id, "every attestation must agree"))
        if not blind:
            continue
        if role == "research":
            research.append((lane, family))
        elif role == "verify":
            verify.append((lane, family))
    if not research or not verify:
        errors.append(
            _err(
                "ATTESTATIONS",
                item_id,
                "need a blind research attestation and a blind verify attestation",
            )
        )
        return errors, [lane for lane, _family in verify]
    if not any(
        left_lane != right_lane and left_family != right_family
        for left_lane, left_family in research
        for right_lane, right_family in verify
    ):
        lanes_differ = any(
            left_lane != right_lane
            for left_lane, _left in research
            for right_lane, _right in verify
        )
        families_differ = any(
            left_family != right_family
            for _left, left_family in research
            for _right, right_family in verify
        )
        if not lanes_differ:
            errors.append(
                _err(
                    "SAME_LANE",
                    item_id,
                    "research and verify attestations share a lane",
                )
            )
        if not families_differ:
            errors.append(
                _err(
                    "SAME_FAMILY",
                    item_id,
                    "research and verify attestations share a model family",
                )
            )
        if lanes_differ and families_differ:
            errors.append(
                _err(
                    "ATTESTATIONS",
                    item_id,
                    "no research and verify pair from different lanes and families",
                )
            )
    return errors, [lane for lane, _family in verify]


def _parse_attestation(
    item_id: str, attestation: Mapping[str, object]
) -> tuple[str, str, str, bool, str] | str:
    role = attestation.get("role")
    lane = attestation.get("lane")
    model = attestation.get("model")
    conclusion = attestation.get("conclusion")
    blind = attestation.get("blind")
    if role not in _ROLES:
        return _err("EVIDENCE_SCHEMA", item_id, "attestation role is not known")
    if not isinstance(lane, str) or not _safe_token(lane):
        return _err("EVIDENCE_SCHEMA", item_id, "attestation lane is missing")
    if not isinstance(model, str) or not model.strip():
        return _err("EVIDENCE_SCHEMA", item_id, "attestation model is missing")
    if conclusion not in _CONCLUSIONS:
        return _err("EVIDENCE_SCHEMA", item_id, "attestation conclusion is not known")
    if not isinstance(blind, bool):
        return _err("EVIDENCE_SCHEMA", item_id, "attestation blind must be a boolean")
    if not _nonempty(attestation.get("issue")) or not _nonempty(
        attestation.get("date")
    ):
        return _err(
            "EVIDENCE_SCHEMA", item_id, "attestation issue and date are required"
        )
    if "notes" not in attestation or not isinstance(attestation.get("notes"), str):
        return _err("EVIDENCE_SCHEMA", item_id, "attestation notes must be a string")
    assert isinstance(role, str)
    assert isinstance(conclusion, str)
    return role, lane.strip(), model_family(model), blind, conclusion


def _check_verifier(evidence_dir: Path, item: _Item, lanes: list[str]) -> list[str]:
    if not lanes:
        return []
    expected = substantive_values(item.values)
    errors: list[str] = []
    seen: set[str] = set()
    for lane in lanes:
        if lane in seen:
            continue
        seen.add(lane)
        path = _verifier_file(evidence_dir, item.item_id, lane)
        if path is None:
            errors.append(
                _err(
                    "EVIDENCE_SCHEMA",
                    item.item_id,
                    "verifier lane is not a safe file name",
                )
            )
            continue
        if not path.is_file():
            errors.append(
                _err(
                    "MISSING_VERIFIER",
                    item.item_id,
                    f"missing verification file for lane {lane}",
                )
            )
            continue
        loaded, parse_error = _load_yaml(path)
        if parse_error is not None:
            errors.append(_err("VERIFIER_MISMATCH", item.item_id, parse_error))
            continue
        doc = _as_dict(loaded)
        if doc is None:
            errors.append(
                _err(
                    "VERIFIER_MISMATCH", item.item_id, "verifier file must be a mapping"
                )
            )
            continue
        actual = _verifier_values(doc)
        if actual is None:
            errors.append(
                _err(
                    "VERIFIER_MISMATCH",
                    item.item_id,
                    "verifier values must be a mapping",
                )
            )
            continue
        if actual != expected:
            differing = _diff_keys(expected, actual)
            errors.append(
                _err(
                    "VERIFIER_MISMATCH",
                    item.item_id,
                    "verifier values differ: " + ", ".join(differing),
                )
            )
    return errors


def _check_derivations(goldens_dir: Path, item: _Item) -> list[str]:
    fixture_id = item.item_id.removeprefix("golden-")
    derivations = goldens_dir / "derivations"
    if not derivations.is_dir():
        return [
            _err(
                "GOLDEN_DERIVATIONS",
                item.item_id,
                "verified golden needs two derivation files",
            )
        ]
    prefix = fixture_id + "."
    files = sorted(
        path
        for path in derivations.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.suffix in {".yaml", ".yml"}
    )
    if len(files) < 2:
        return [
            _err(
                "GOLDEN_DERIVATIONS",
                item.item_id,
                "verified golden needs two derivation files",
            )
        ]
    expected = _expected_payload(item.values)
    families: list[str] = []
    errors: list[str] = []
    for path in files:
        token = path.name[len(prefix) : -len(path.suffix)]
        families.append(model_family(token))
        loaded, parse_error = _load_yaml(path)
        if parse_error is not None:
            errors.append(_err("GOLDEN_DERIVATIONS", item.item_id, parse_error))
            continue
        doc = _as_dict(loaded)
        if doc is None:
            errors.append(
                _err(
                    "GOLDEN_DERIVATIONS",
                    item.item_id,
                    f"{path.name} must be a mapping",
                )
            )
            continue
        if _expected_payload(doc) != expected:
            errors.append(
                _err(
                    "GOLDEN_DERIVATIONS",
                    item.item_id,
                    f"{path.name} expected values differ",
                )
            )
    if len(set(families)) < 2:
        errors.append(
            _err(
                "GOLDEN_DERIVATIONS",
                item.item_id,
                "derivation files must come from different model families",
            )
        )
    return errors


def _expected_payload(values: Mapping[str, object]) -> object:
    if "expected" in values:
        return _canon(values["expected"])
    return substantive_values(values)


def _verifier_values(doc: Mapping[str, object]) -> dict[str, object] | None:
    if "values" in doc:
        mapped = _as_dict(doc["values"])
        if mapped is None:
            return None
        return substantive_values(mapped)
    return {
        key: _canon(value) for key, value in doc.items() if key not in _VERIFIER_META
    }


def _diff_keys(
    expected: Mapping[str, object], actual: Mapping[str, object]
) -> list[str]:
    keys = set(expected) | set(actual)
    differing: list[str] = []
    for key in sorted(keys):
        if key not in expected or key not in actual or expected[key] != actual[key]:
            differing.append(key)
    return differing


def _scan(root: Path) -> tuple[list[_Item], list[str]]:
    items: list[_Item] = []
    errors: list[str] = []
    rules_dir = root / "rules"
    found, scan_errors = _scan_rules(rules_dir / "entries.yaml")
    items.extend(found)
    errors.extend(scan_errors)
    found, scan_errors = _scan_rates(rules_dir / "rates.yaml")
    items.extend(found)
    errors.extend(scan_errors)
    found, scan_errors = _scan_catalog(rules_dir / "catalog.yaml")
    items.extend(found)
    errors.extend(scan_errors)
    found, scan_errors = _scan_basis(rules_dir / "filing_basis.yaml")
    items.extend(found)
    errors.extend(scan_errors)
    found, scan_errors = _scan_goldens(root / "fixtures" / "goldens")
    items.extend(found)
    errors.extend(scan_errors)
    return items, errors


def _scan_rules(path: Path) -> tuple[list[_Item], list[str]]:
    rows, errors = _rows(path, "rules")
    items: list[_Item] = []
    for row in rows:
        if row.get("review_status") != "verified":
            continue
        rule_id = row.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            errors.append(
                _err("CONTENT_PARSE", path.name, "verified rule is missing an id")
            )
            continue
        items.append(_Item(rule_id.strip(), "rule", row))
    return items, errors


def _scan_rates(path: Path) -> tuple[list[_Item], list[str]]:
    rows, errors = _rows(path, "rates")
    items: list[_Item] = []
    for row in rows:
        if row.get("review_status") != "verified":
            continue
        jurisdiction = row.get("jurisdiction")
        location_code = row.get("location_code")
        quarter = row.get("quarter")
        if (
            not isinstance(jurisdiction, str)
            or not isinstance(location_code, str)
            or not isinstance(quarter, str)
        ):
            errors.append(
                _err(
                    "CONTENT_PARSE",
                    path.name,
                    "verified rate is missing identity fields",
                )
            )
            continue
        item_id = rate_item_id(jurisdiction, location_code, quarter)
        items.append(_Item(item_id, "rate", row))
    return items, errors


def _scan_catalog(path: Path) -> tuple[list[_Item], list[str]]:
    rows, errors = _rows(path, "entries")
    items: list[_Item] = []
    for row in rows:
        if row.get("status") != "verified":
            continue
        category = row.get("category")
        if not isinstance(category, str) or not category.strip():
            errors.append(
                _err(
                    "CONTENT_PARSE",
                    path.name,
                    "verified catalog entry is missing a category",
                )
            )
            continue
        items.append(_Item(catalog_item_id(category.strip()), "catalog", row))
    return items, errors


def _scan_basis(path: Path) -> tuple[list[_Item], list[str]]:
    if not path.exists():
        return [], []
    rows, errors = _rows(path, "entries")
    items: list[_Item] = []
    for row in rows:
        if row.get("status") != "verified":
            continue
        basis_id = row.get("id")
        if not isinstance(basis_id, str) or not basis_id.strip():
            errors.append(
                _err(
                    "CONTENT_PARSE", path.name, "verified filing basis is missing an id"
                )
            )
            continue
        items.append(_Item(f"basis-{basis_id.strip()}", "basis", row))
    return items, errors


def _scan_goldens(path: Path) -> tuple[list[_Item], list[str]]:
    if not path.is_dir():
        return [], []
    items: list[_Item] = []
    errors: list[str] = []
    files = sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file()
        and candidate.suffix in {".yaml", ".yml"}
        and "derivations" not in candidate.relative_to(path).parts
    )
    for golden in files:
        loaded, parse_error = _load_yaml(golden)
        if parse_error is not None:
            errors.append(_err("CONTENT_PARSE", golden.name, parse_error))
            continue
        doc = _as_dict(loaded)
        if doc is None or doc.get("status") != "verified":
            continue
        fixture_id = doc.get("id")
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            fixture_id = golden.stem
        items.append(_Item(f"golden-{fixture_id.strip()}", "golden", doc))
    return items, errors


def _rows(path: Path, key: str) -> tuple[list[dict[str, object]], list[str]]:
    if not path.exists():
        return [], []
    loaded, parse_error = _load_yaml(path)
    if parse_error is not None:
        return [], [_err("CONTENT_PARSE", path.name, parse_error)]
    if loaded is None:
        return [], []
    doc = _as_dict(loaded)
    if doc is None:
        return [], [_err("CONTENT_PARSE", path.name, "must be a mapping")]
    raw = doc.get(key, [])
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], [_err("CONTENT_PARSE", path.name, f"{key} must be a list")]
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    for item in raw:
        mapped = _as_dict(item)
        if mapped is None:
            errors.append(
                _err("CONTENT_PARSE", path.name, f"{key} entry must be a mapping")
            )
            continue
        rows.append(mapped)
    return rows, errors


def _load_allowlist(path: Path) -> tuple[set[str], list[str]]:
    loaded, parse_error = _load_yaml(path)
    if parse_error is not None:
        return set(), [_err("ALLOWLIST", "allowlist", parse_error)]
    doc = _as_dict(loaded)
    if doc is None:
        return set(), [_err("ALLOWLIST", "allowlist", "must be a mapping")]
    raw = doc.get("domains")
    if not isinstance(raw, list):
        return set(), [_err("ALLOWLIST", "allowlist", "domains must be a list")]
    domains: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return set(), [_err("ALLOWLIST", "allowlist", "domains must be hostnames")]
        domains.add(item.strip().lower().rstrip("."))
    return domains, []


def _host(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return None
    return parsed.hostname.lower().rstrip(".")


def _host_allowed(host: str, domains: set[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def _snapshot_file(evidence_dir: Path, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    root = evidence_dir.resolve()
    candidate = (evidence_dir / relative).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


def _evidence_file(evidence_dir: Path, item_id: str) -> Path | None:
    stem = evidence_stem(item_id)
    if not _safe_token(stem):
        return None
    return evidence_dir / f"{stem}.yaml"


def _verifier_file(evidence_dir: Path, item_id: str, lane: str) -> Path | None:
    stem = evidence_stem(item_id)
    if not _safe_token(stem) or not _safe_token(lane):
        return None
    return evidence_dir / "verification" / f"{stem}.{lane}.yaml"


def _safe_token(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
    )


def _canon(value: object) -> object:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _canon(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canon(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _nonempty(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, date):
        return True
    return isinstance(value, int) and not isinstance(value, bool)


def _as_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        mapped[key] = item
    return mapped


def _as_list(value: object) -> list[object] | None:
    if not isinstance(value, list) or not value:
        return None
    return list(value)


def _load_yaml(path: Path) -> tuple[object, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, str(exc)
    try:
        loaded: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"YAML parse error: {exc}"
    return loaded, None


def _err(code: str, item: str, detail: str) -> str:
    return f"{code}: {item}: {' '.join(detail.split())}"
