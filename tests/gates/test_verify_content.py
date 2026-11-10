"""verify-content fails closed on synthetic evidence. No real tax data."""

from pathlib import Path

import yaml

from app.domain.content_verification import (
    model_family,
    substantive_values,
    verify_content,
)
from app.domain.ruleset import (
    catalog_is_verified,
    load_ruleset,
    rate_is_active,
    rule_is_active,
)
from tests.activation_evidence import dump_yaml, write_item_evidence

ROOT = Path(__file__).resolve().parents[2]
RULE_URL = "https://laws.statute.test/section/1"


def _codes(errors: list[str]) -> set[str]:
    return {error.split(":", 1)[0] for error in errors}


def _rule(rule_id: str = "SYN-1", review_status: str = "verified") -> dict[str, object]:
    return {
        "id": rule_id,
        "jurisdiction": "ZZ",
        "category": "SYNTHETIC",
        "treatment": "Synthetic treatment",
        "calculation": "not_taxable",
        "applies_when": "any",
        "effective_from": "2020-01-01",
        "effective_to": None,
        "timing_basis": "invoice_date",
        "primary_source_url": RULE_URL,
        "review_status": review_status,
        "reviewed_by": None,
        "reviewed_at": None,
    }


def _rate() -> dict[str, object]:
    return {
        "jurisdiction": "ZZ",
        "location": "Synthetic City",
        "location_code": "0000",
        "rate": "0.1000",
        "quarter": "2026Q4",
        "source_url": "https://laws.statute.test/rates",
        "review_status": "verified",
        "reviewed_by": None,
    }


def _allow(rules: Path) -> None:
    evidence = rules / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "allowlist.yaml").write_text(
        "domains:\n  - statute.test\n", encoding="utf-8"
    )


def _write_rules(
    root: Path, document: dict[str, object], filename: str
) -> dict[str, object]:
    rules = root / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    _allow(rules)
    (rules / filename).write_text(dump_yaml(document), encoding="utf-8")
    loaded = yaml.safe_load((rules / filename).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    key = next(iter(document))
    rows = loaded[key]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    return {str(field): value for field, value in rows[0].items()}


def test_shipped_content_passes() -> None:
    assert verify_content(ROOT) == []


def test_model_family_prefixes() -> None:
    assert model_family("grok-4") == "grok"
    assert model_family("gpt-5") == "gpt"
    assert model_family("openai/gpt-4.1") == "gpt"
    assert model_family("default") == "gpt"
    assert model_family("claude-opus") == "claude"
    assert model_family("muse-wonder") == "muse"
    assert model_family("gemini-2.5") == "gemini"


def test_passing_rule_activates(tmp_path: Path) -> None:
    root, values = _passing_rule(tmp_path)
    assert verify_content(root) == []
    loaded = load_ruleset(root / "rules")
    assert rule_is_active(loaded.rules[0])
    assert loaded.rules[0].review_status == "verified"
    assert values["id"] == "SYN-1"


def test_candidate_needs_no_evidence(tmp_path: Path) -> None:
    root = tmp_path
    _write_rules(root, {"rules": [_rule(review_status="candidate")]}, "entries.yaml")
    assert verify_content(root) == []
    assert not rule_is_active(load_ruleset(root / "rules").rules[0])


def test_missing_evidence(tmp_path: Path) -> None:
    root = tmp_path
    _write_rules(root, {"rules": [_rule()]}, "entries.yaml")
    assert _codes(verify_content(root)) == {"MISSING_EVIDENCE"}
    assert not rule_is_active(load_ruleset(root / "rules").rules[0])


def test_non_allowlisted_url(tmp_path: Path) -> None:
    root, values = _passing_rule(tmp_path, url="https://blog.example/post")
    assert _codes(verify_content(root)) == {"URL_NOT_ALLOWLISTED"}
    assert not rule_is_active(load_ruleset(root / "rules").rules[0])
    assert values["primary_source_url"] == RULE_URL


def test_quote_not_in_snapshot(tmp_path: Path) -> None:
    root, _values = _passing_rule(
        tmp_path, quote="This sentence was never snapshotted."
    )
    assert _codes(verify_content(root)) == {"QUOTE_NOT_IN_SNAPSHOT"}


def test_snapshot_hash_mismatch(tmp_path: Path) -> None:
    root, _values = _passing_rule(tmp_path, digest="0" * 64)
    assert _codes(verify_content(root)) == {"SNAPSHOT_HASH_MISMATCH"}


def test_one_attestation_only(tmp_path: Path) -> None:
    root, _values = _passing_rule(tmp_path, include_verify=False)
    assert _codes(verify_content(root)) == {"ATTESTATIONS"}


def test_same_lane(tmp_path: Path) -> None:
    root, _values = _passing_rule(tmp_path, lanes=("alpha", "alpha"))
    assert _codes(verify_content(root)) == {"SAME_LANE"}


def test_same_model_family(tmp_path: Path) -> None:
    root, _values = _passing_rule(tmp_path, models=("gpt-5", "default"))
    assert _codes(verify_content(root)) == {"SAME_FAMILY"}


def test_verifier_values_differ(tmp_path: Path) -> None:
    root = tmp_path
    values = _write_rules(root, {"rules": [_rule()]}, "entries.yaml")
    changed = dict(substantive_values(values))
    changed["treatment"] = "Different synthetic treatment"
    write_item_evidence(
        root / "rules",
        "SYN-1",
        values,
        kind="rule",
        url=RULE_URL,
        verifier=changed,
    )
    assert _codes(verify_content(root)) == {"VERIFIER_MISMATCH"}
    assert not rule_is_active(load_ruleset(root / "rules").rules[0])


def test_placeholder_marked_verified_never_activates(tmp_path: Path) -> None:
    root = tmp_path
    _write_rules(root, {"rules": [_rule("PLACEHOLDER:SYN")]}, "entries.yaml")
    assert _codes(verify_content(root)) == {"PLACEHOLDER_ACTIVE"}
    assert not rule_is_active(load_ruleset(root / "rules").rules[0])


def test_rate_requires_allowlisted_rate_table(tmp_path: Path) -> None:
    failing = tmp_path / "failing"
    _rate_tree(failing, authority="statute")
    assert _codes(verify_content(failing)) == {"RATE_TABLE"}
    assert not rate_is_active(load_ruleset(failing / "rules").rates[0])
    passing = tmp_path / "passing"
    _rate_tree(passing, authority="rate_table")
    assert verify_content(passing) == []
    assert rate_is_active(load_ruleset(passing / "rules").rates[0])


def test_verified_golden_needs_two_derivations(tmp_path: Path) -> None:
    root = _golden(tmp_path)
    assert verify_content(root) == []
    derivation = root / "fixtures" / "goldens" / "derivations" / "synth-1.default.yaml"
    derivation.write_text(
        derivation.read_text(encoding="utf-8").replace(
            "NEEDS_HUMAN_REVIEW", "ENGINE_SUPPORTED"
        ),
        encoding="utf-8",
    )
    assert _codes(verify_content(root)) == {"GOLDEN_DERIVATIONS"}


def test_verified_catalog_without_evidence_fails_the_gate(tmp_path: Path) -> None:
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "catalog.yaml").write_text(
        "\n".join(
            [
                "entries:",
                "  - category: SYNTHETIC",
                "    status: verified",
                "    required_keys:",
                "      - vendor_name",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    errors = verify_content(tmp_path)
    assert _codes(errors) == {"MISSING_EVIDENCE"}
    assert any("catalog-SYNTHETIC" in error for error in errors)
    assert catalog_is_verified(load_ruleset(rules).catalog[0])


def test_verified_basis_without_evidence_fails(tmp_path: Path) -> None:
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "filing_basis.yaml").write_text(
        "\n".join(
            [
                "entries:",
                '  - id: "synth"',
                "    status: verified",
                '    text: "Synthetic basis"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    errors = verify_content(tmp_path)
    assert _codes(errors) == {"MISSING_EVIDENCE"}
    assert any("basis-synth" in error for error in errors)


def _passing_rule(
    tmp_path: Path,
    *,
    url: str = RULE_URL,
    quote: str | None = None,
    digest: str | None = None,
    lanes: tuple[str, str] | None = None,
    models: tuple[str, str] | None = None,
    include_verify: bool = True,
) -> tuple[Path, dict[str, object]]:
    root = tmp_path
    values = _write_rules(root, {"rules": [_rule()]}, "entries.yaml")
    arguments: dict[str, object] = {
        "kind": "rule",
        "url": url,
        "include_verify": include_verify,
    }
    if quote is not None:
        arguments["quote"] = quote
    if digest is not None:
        arguments["digest"] = digest
    if lanes is not None:
        arguments["lanes"] = lanes
    if models is not None:
        arguments["models"] = models
    write_item_evidence(
        root / "rules",
        "SYN-1",
        values,
        **arguments,  # type: ignore[arg-type]
    )
    return root, values


def _rate_tree(root: Path, *, authority: str) -> None:
    values = _write_rules(root, {"rates": [_rate()]}, "rates.yaml")
    write_item_evidence(
        root / "rules",
        "rate-ZZ-0000-2026Q4",
        values,
        kind="rate",
        url="https://laws.statute.test/rates",
        authority=authority,
    )


def _golden(root: Path) -> Path:
    expected = {"label": "NEEDS_HUMAN_REVIEW", "amount": None}
    golden = {
        "id": "synth-1",
        "status": "verified",
        "expected": expected,
    }
    rules = root / "rules"
    _allow(rules)
    goldens = root / "fixtures" / "goldens"
    goldens.mkdir(parents=True)
    (goldens / "synth-1.yaml").write_text(dump_yaml(golden), encoding="utf-8")
    loaded = yaml.safe_load((goldens / "synth-1.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    values = {str(field): value for field, value in loaded.items()}
    write_item_evidence(
        rules,
        "golden-synth-1",
        values,
        kind="golden",
        url=RULE_URL,
    )
    derivations = goldens / "derivations"
    derivations.mkdir()
    payload = dump_yaml({"expected": expected})
    (derivations / "synth-1.grok.yaml").write_text(payload, encoding="utf-8")
    (derivations / "synth-1.default.yaml").write_text(payload, encoding="utf-8")
    return root
