"""Write synthetic verification evidence for tests. Not tax content."""

from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

import yaml

from app.domain.content_verification import evidence_stem, substantive_values
from app.domain.ruleset import is_placeholder, rate_item_id

SNAPSHOT_REL = "snapshots/activation.txt"
SNAPSHOT_TEXT = "Header\nSynthetic quoted\nsentence for verification.\nFooter\n"
QUOTE = "Synthetic quoted sentence for verification."


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


_Dumper.add_representer(str, _represent_str)


def dump_yaml(data: object) -> str:
    dumped = yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True)
    if not isinstance(dumped, str):
        raise TypeError("yaml dump must be text")
    return dumped


def write_item_evidence(
    rules_dir: Path,
    item_id: str,
    values: Mapping[str, object],
    *,
    kind: str,
    url: str,
    quote: str = QUOTE,
    snapshot_rel: str = SNAPSHOT_REL,
    snapshot_text: str = SNAPSHOT_TEXT,
    lanes: tuple[str, str] = ("alpha", "beta"),
    models: tuple[str, str] = ("grok-4", "default"),
    include_verify: bool = True,
    digest: str | None = None,
    authority: str | None = None,
    verifier: Mapping[str, object] | None = None,
) -> None:
    evidence = rules_dir / "evidence"
    snapshot = evidence / snapshot_rel
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(snapshot_text, encoding="utf-8")
    (evidence / "verification").mkdir(parents=True, exist_ok=True)
    file_digest = (
        digest if digest is not None else sha256(snapshot.read_bytes()).hexdigest()
    )
    source_authority = authority or ("rate_table" if kind == "rate" else "statute")
    attestations: list[dict[str, object]] = [
        _attestation("research", lanes[0], models[0]),
    ]
    if include_verify:
        attestations.append(_attestation("verify", lanes[1], models[1]))
    document: dict[str, object] = {
        "item": item_id,
        "claims": [
            {
                "field": "rate" if kind == "rate" else "treatment",
                "value": "synthetic",
                "sources": [
                    {
                        "url": url,
                        "authority": source_authority,
                        "pinpoint": "synthetic",
                        "quote": quote,
                        "retrieved_at": "2026-01-01",
                        "snapshot": snapshot_rel,
                        "snapshot_sha256": file_digest,
                    }
                ],
            }
        ],
        "attestations": attestations,
    }
    stem = evidence_stem(item_id)
    (evidence / f"{stem}.yaml").write_text(dump_yaml(document), encoding="utf-8")
    if include_verify:
        payload = dict(verifier) if verifier is not None else substantive_values(values)
        (evidence / "verification" / f"{stem}.{lanes[1]}.yaml").write_text(
            dump_yaml(payload), encoding="utf-8"
        )


def stamp_verified_ruleset(rules_dir: Path, allowlist_source: Path) -> None:
    """Attach passing synthetic evidence to verified rules and rates."""
    evidence = rules_dir / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    target = evidence / "allowlist.yaml"
    if not target.exists():
        target.write_text(
            allowlist_source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    _stamp_file(
        rules_dir,
        "entries.yaml",
        "rules",
        "rule",
        "https://app.leg.wa.gov/synthetic-activation",
    )
    _stamp_file(
        rules_dir,
        "rates.yaml",
        "rates",
        "rate",
        "https://dor.wa.gov/synthetic-activation",
    )


def _stamp_file(rules_dir: Path, filename: str, key: str, kind: str, url: str) -> None:
    path = rules_dir / filename
    if not path.is_file():
        return
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return
    rows = loaded.get(key, [])
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict) or row.get("review_status") != "verified":
            continue
        values = {str(field): value for field, value in row.items()}
        item_id = _item_id(kind, values)
        if item_id is None or is_placeholder(item_id):
            continue
        write_item_evidence(rules_dir, item_id, values, kind=kind, url=url)


def _item_id(kind: str, values: Mapping[str, object]) -> str | None:
    if kind == "rule":
        rule_id = values.get("id")
        return rule_id if isinstance(rule_id, str) else None
    jurisdiction = values.get("jurisdiction")
    location_code = values.get("location_code")
    quarter = values.get("quarter")
    if (
        not isinstance(jurisdiction, str)
        or not isinstance(location_code, str)
        or not isinstance(quarter, str)
    ):
        return None
    return rate_item_id(jurisdiction, location_code, quarter)


def _attestation(role: str, lane: str, model: str) -> dict[str, object]:
    return {
        "role": role,
        "lane": lane,
        "model": model,
        "issue": "test",
        "date": "2026-01-01",
        "blind": True,
        "conclusion": "agree",
        "notes": "synthetic",
    }
