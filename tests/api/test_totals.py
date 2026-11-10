"""GET /totals keeps liabilities and refund candidates apart.

Every request sends X-Requested-With: taxleak. Each test copies rules/ and
changes only the catalog, rule, and rate metadata that scenario needs.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.domain.ruleset import Ruleset, load_ruleset, rate_is_active, rule_is_active
from app.main import create_app
from tests.api.conftest import (
    assert_refund_candidates_stay_separate,
    copy_scenario_rules,
    forbidden_total_field_names,
)
from tests.api.test_flow import GOOD_ENV, ROOT, _headers, _run


@dataclass(frozen=True)
class _Rules:
    signed_categories: frozenset[str]
    reviewed_rule_ids: frozenset[str]
    reviewed_rates: frozenset[tuple[str, str]]


_SAAS = frozenset({"REMOTE_ACCESS_SAAS"})
_WA_RULE = frozenset({"WA-RAS-001"})
_CA_RULE = frozenset({"CA-RAS-001"})
_WA_RATE = frozenset({("WA", "Seattle, WA")})
_CA_RATE = frozenset({("CA", "San Francisco, CA")})
_CASE1 = _Rules(_SAAS, _WA_RULE, _WA_RATE)
_CASE3 = _Rules(_SAAS, _CA_RULE, _CA_RATE)
_BOTH = _Rules(_SAAS, _WA_RULE | _CA_RULE, _WA_RATE | _CA_RATE)
_NONE = _Rules(frozenset(), frozenset(), frozenset())


def _get(client: TestClient, path: str) -> dict[str, object]:
    response = client.get(path, headers={"X-Requested-With": "taxleak"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


@contextmanager
def _client(tmp_path: Path, rules: _Rules) -> Iterator[TestClient]:
    copy_scenario_rules(
        tmp_path / "rules",
        signed_categories=rules.signed_categories,
        reviewed_rule_ids=rules.reviewed_rule_ids,
        reviewed_rates=rules.reviewed_rates,
    )
    database = tmp_path / "app.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(tmp_path / "rules"),
        ramp_webhook_secret="test-secret",
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        client.headers["X-Requested-With"] = "taxleak"
        login = client.post(
            "/api/v1/demo/login",
            json={},
            headers=_headers("login"),
        )
        assert login.status_code == 200, login.text
        token = login.json().get("csrf_token")
        if isinstance(token, str) and token:
            client.headers["X-CSRF-Token"] = token
        yield client


def test_case_1_liability_total_copies_the_engine_string(tmp_path: Path) -> None:
    with _client(tmp_path, _CASE1) as client:
        created = _run(client, "case1", "case1")
        detail = _get(client, f"/api/v1/purchases/{created['purchase_id']}")
        determination = detail["determinations"][0]
        assert isinstance(determination, dict)
        assert determination["direction"] == "LIABILITY"
        assert determination["rule_reference"] == "WA-RAS-001"
        engine_amount = determination["engine_amount"]
        assert isinstance(engine_amount, dict)
        engine = engine_amount["value"]
        approved = client.post(
            f"/api/v1/determinations/{determination['id']}/review",
            json={
                "action": "approve",
                "approval_path": "LIABILITY",
                "reason": "Reviewed",
                "expected_version": determination["version"],
            },
            headers=_headers("approve-case1"),
        )
        assert approved.status_code == 200, approved.text
        totals = _get(client, "/api/v1/totals")
        rows = totals["approved_liabilities"]["by_jurisdiction"]
        assert rows == [
            {
                "jurisdiction": "WA",
                "period": "2026Q4",
                "total": {"value": engine, "currency": "USD"},
                "line_count": 1,
            }
        ]
        assert rows[0]["total"]["value"] != "0.00" or engine == "0.00"
        candidates = totals["refund_candidates"]
        assert isinstance(candidates, dict)
        assert candidates["count"] == 0
        assert candidates["total"] is None
        assert_refund_candidates_stay_separate(totals)


def test_unreviewed_refund_amount_stays_off_totals(tmp_path: Path) -> None:
    with _client(tmp_path, _CASE3) as client:
        created = _run(client, "case3", "case3")
        detail = _get(client, f"/api/v1/purchases/{created['purchase_id']}")
        determination = detail["determinations"][0]
        assert isinstance(determination, dict)
        assert determination["rule_reference"] == "CA-RAS-001"
        assert determination["direction"] == "REFUND_CANDIDATE"
        assert determination["engine_amount"] is not None
        totals = _get(client, "/api/v1/totals")
        candidates = totals["refund_candidates"]
        assert isinstance(candidates, dict)
        assert candidates["count"] == 0
        assert candidates["total"] is None
        liabilities = totals["approved_liabilities"]
        assert isinstance(liabilities, dict)
        assert liabilities["message"] == "No approved liabilities"
        assert "0.00" not in json.dumps(liabilities)
        unreviewed = totals["unreviewed"]
        assert isinstance(unreviewed, dict)
        by_direction = unreviewed["by_direction"]
        assert isinstance(by_direction, list)
        directions = {row["direction"]: row["count"] for row in by_direction}
        assert directions.get("REFUND_CANDIDATE") == 1
        assert "amount" not in by_direction[0]
        assert_refund_candidates_stay_separate(totals)


def test_approved_refund_does_not_reduce_liabilities(tmp_path: Path) -> None:
    with _client(tmp_path, _BOTH) as client:
        case1 = _run(client, "case1", "liability")
        liability = _get(client, f"/api/v1/purchases/{case1['purchase_id']}")[
            "determinations"
        ][0]
        assert isinstance(liability, dict)
        assert liability["direction"] == "LIABILITY"
        approved_liability = client.post(
            f"/api/v1/determinations/{liability['id']}/review",
            json={
                "action": "approve",
                "approval_path": "LIABILITY",
                "reason": "Reviewed",
                "expected_version": liability["version"],
            },
            headers=_headers("approve-liability"),
        )
        assert approved_liability.status_code == 200, approved_liability.text
        before = _get(client, "/api/v1/totals")
        before_rows = before["approved_liabilities"]["by_jurisdiction"]
        assert isinstance(before_rows, list)
        liability_total = before_rows[0]["total"]["value"]
        case3 = _run(client, "case3", "refund")
        refund = _get(client, f"/api/v1/purchases/{case3['purchase_id']}")[
            "determinations"
        ][0]
        assert isinstance(refund, dict)
        assert refund["direction"] == "REFUND_CANDIDATE"
        assert refund["rule_reference"] == "CA-RAS-001"
        reviewed = client.post(
            f"/api/v1/determinations/{refund['id']}/review",
            json={
                "action": "approve",
                "approval_path": "REFUND_PURSUIT",
                "reason": "Pursue",
                "expected_version": refund["version"],
            },
            headers=_headers("approve-refund"),
        )
        assert reviewed.status_code == 200, reviewed.text
        detail = reviewed.json()
        letter_id = detail["determinations"][0]["refund_letter_id"]
        assert letter_id
        letter = _get(client, f"/api/v1/drafts/refund-letters/{letter_id}")
        assert letter["basis_text"] == ""
        assert letter["status"] == "DRAFT"
        totals = _get(client, "/api/v1/totals")
        rows = totals["approved_liabilities"]["by_jurisdiction"]
        assert isinstance(rows, list)
        assert rows[0]["total"]["value"] == liability_total
        candidates = totals["refund_candidates"]
        assert isinstance(candidates, dict)
        assert candidates["count"] == 1
        refund_total = candidates["total"]
        assert isinstance(refund_total, dict)
        engine_amount = refund["engine_amount"]
        assert isinstance(engine_amount, dict)
        assert refund_total["value"] == engine_amount["value"]
        refund_rows = candidates["rows"]
        assert isinstance(refund_rows, list)
        assert refund_rows[0]["refund_letter_id"] == letter_id
        journals = _get(client, "/api/v1/drafts/journal-entries")["drafts"]
        assert isinstance(journals, list)
        assert all(row["purchase_id"] != case3["purchase_id"] for row in journals)
        assert_refund_candidates_stay_separate(totals)


def test_held_omits_purchase_amount_when_empty(tmp_path: Path) -> None:
    with _client(tmp_path, _NONE) as client:
        totals = _get(client, "/api/v1/totals")
        held = totals["held"]
        assert isinstance(held, dict)
        assert held["count"] == 0
        assert "purchase_amount" not in held
        assert held["label"] == "purchase amount, not tax"
        assert totals["open_needs_human_review"] == {"count": 0}
        assert totals["determination_errors"] == {"count": 0}
        assert_refund_candidates_stay_separate(totals)


def test_scenario_copy_leaves_unrelated_rules_unsigned(tmp_path: Path) -> None:
    repo = {
        name: (ROOT / "rules" / name).read_bytes()
        for name in ("entries.yaml", "rates.yaml", "catalog.yaml")
    }
    case1 = tmp_path / "case1"
    copy_scenario_rules(
        case1,
        signed_categories=_CASE1.signed_categories,
        reviewed_rule_ids=_CASE1.reviewed_rule_ids,
        reviewed_rates=_CASE1.reviewed_rates,
    )
    wa_only = load_ruleset(case1)
    assert _active_rule_ids(wa_only) == {"WA-RAS-001"}
    assert _active_rate_keys(wa_only) == {("WA", "Seattle, WA")}
    assert _signed_categories(wa_only) == {"REMOTE_ACCESS_SAAS"}

    both = tmp_path / "both"
    copy_scenario_rules(
        both,
        signed_categories=_BOTH.signed_categories,
        reviewed_rule_ids=_BOTH.reviewed_rule_ids,
        reviewed_rates=_BOTH.reviewed_rates,
    )
    paired = load_ruleset(both)
    assert _active_rule_ids(paired) == {"CA-RAS-001", "WA-RAS-001"}
    assert _active_rate_keys(paired) == {
        ("CA", "San Francisco, CA"),
        ("WA", "Seattle, WA"),
    }
    assert _signed_categories(paired) == {"REMOTE_ACCESS_SAAS"}
    for rule in paired.rules:
        if rule.id in {"WA-RAS-001", "CA-RAS-001"}:
            continue
        assert rule.review_status == "candidate"
        assert rule.reviewed_by is None
        assert not rule_is_active(rule)
    for rate in paired.rates:
        if (rate.jurisdiction, rate.location) in _BOTH.reviewed_rates:
            continue
        assert rate.review_status == "candidate"
        assert rate.reviewed_by is None
        assert not rate_is_active(rate)
    hardware = next(
        entry for entry in paired.catalog if entry.category == "COMPUTER_HARDWARE"
    )
    assert hardware.status == "candidate"
    assert hardware.signed_off_by is None
    for name, raw in repo.items():
        assert (ROOT / "rules" / name).read_bytes() == raw


def test_forbidden_total_names_ignore_the_disclaimer() -> None:
    clean = {
        "refund_candidates": {
            "label": "not recovered cash",
            "total": None,
            "count": 0,
        }
    }
    assert forbidden_total_field_names(clean) == []
    assert_refund_candidates_stay_separate(clean)
    injected = {
        "refund_candidates": {
            "label": "not recovered cash",
            "recovered": {"value": "1.00", "currency": "USD"},
        },
        "approved_liabilities": {
            "net": {"value": "1.00"},
            "rows": [{"savings": "1.00"}],
        },
    }
    assert set(forbidden_total_field_names(injected)) == {"net", "recovered", "savings"}
    with pytest.raises(AssertionError):
        assert_refund_candidates_stay_separate(injected)


def _active_rule_ids(ruleset: Ruleset) -> set[str]:
    return {rule.id for rule in ruleset.rules if rule_is_active(rule)}


def _active_rate_keys(ruleset: Ruleset) -> set[tuple[str, str]]:
    return {
        (rate.jurisdiction, rate.location)
        for rate in ruleset.rates
        if rate_is_active(rate)
    }


def _signed_categories(ruleset: Ruleset) -> set[str]:
    return {
        entry.category
        for entry in ruleset.catalog
        if entry.status == "signed" and entry.signed_off_by
    }
