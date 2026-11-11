"""RG-4, the negative half of RG-9, H1, and H5.

Repo YAML is only read. A temp copy may mark WA-RAS-001 and the Seattle
rate reviewed, and the SaaS catalog signed. PLACEHOLDER stays inactive.
The word signed does not activate a rule or a rate.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import load_settings
from app.contracts.enums import Label
from app.demo.scenarios import get_scenario
from app.domain.evaluate import (
    EngineLineRequest,
    EngineLineResult,
    evaluate_line,
    quarter_key,
)
from app.domain.moneyutil import emit_cents, parse_decimal
from app.domain.ruleset import (
    Ruleset,
    catalog_is_signed,
    is_placeholder,
    load_ruleset,
    rate_is_active,
    rule_is_active,
)
from app.main import create_app
from app.models import Determination, Purchase
from app.worker.poller import poll_once
from tests.api.test_flow import GOOD_ENV, ROOT, _headers, _open, _run

_PLACEHOLDER = "PLACEHOLDER:CA-DP-001"
_WA_RULE = "WA-RAS-001"
_SEATTLE = "Seattle, WA"
_SAAS = "REMOTE_ACCESS_SAAS"
_RULE_FILES = ("entries.yaml", "rates.yaml", "catalog.yaml")
_FORBIDDEN = ("treatment", "label", "direction", "tax_decision_amount")


def _repo_bytes() -> dict[str, bytes]:
    return {name: (ROOT / "rules" / name).read_bytes() for name in _RULE_FILES}


@pytest.fixture(autouse=True)
def _repo_rules_are_not_edited() -> Iterator[None]:
    before = _repo_bytes()
    yield
    assert _repo_bytes() == before


@dataclass(frozen=True)
class _Stored:
    id: str
    is_current: bool
    unit_status: str
    engine_amount: str | None
    expected_tax: str | None
    label: str | None
    facts_version: int
    rule_reference: str | None
    rate: str | None
    engine_request: str | None
    engine_response: str | None


@dataclass(frozen=True)
class _RulesCopy:
    rule_status: str | None = None
    rate_status: str | None = None
    catalog_signed: bool = False
    placeholder_status: str | None = None


def _load_yaml(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"{path.name} must be a mapping")
    return loaded


def _records(document: dict[str, object], key: str) -> list[dict[str, object]]:
    raw = document.get(key)
    if not isinstance(raw, list):
        raise AssertionError(f"{key} must be a list")
    records: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AssertionError(f"{key} entry must be a mapping")
        records.append(item)
    return records


def _one(records: list[dict[str, object]], field: str, value: str) -> dict[str, object]:
    matches = [record for record in records if record.get(field) == value]
    if len(matches) != 1:
        raise AssertionError(f"expected one {field}={value}")
    return matches[0]


def _write_rules(dest: Path, copy: _RulesCopy) -> None:
    """Copy repo YAML and touch only the named records. No blanket replace."""
    dest.mkdir()
    source = ROOT / "rules"
    entries = _load_yaml(source / "entries.yaml")
    rates = _load_yaml(source / "rates.yaml")
    catalog = _load_yaml(source / "catalog.yaml")
    if copy.rule_status is not None:
        rule = _one(_records(entries, "rules"), "id", _WA_RULE)
        rule["review_status"] = copy.rule_status
        rule["reviewed_by"] = "AF"
    if copy.placeholder_status is not None:
        placeholder = _one(_records(entries, "rules"), "id", _PLACEHOLDER)
        placeholder["review_status"] = copy.placeholder_status
        placeholder["reviewed_by"] = "AF"
    if copy.rate_status is not None:
        rate = _one(_records(rates, "rates"), "location", _SEATTLE)
        rate["review_status"] = copy.rate_status
        rate["reviewed_by"] = "AF"
    if copy.catalog_signed:
        entry = _one(_records(catalog, "entries"), "category", _SAAS)
        entry["status"] = "signed"
        entry["signed_off_by"] = "AF"
    for name, document in (
        ("entries.yaml", entries),
        ("rates.yaml", rates),
        ("catalog.yaml", catalog),
    ):
        (dest / name).write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )


@contextmanager
def _client(tmp_path: Path, copy: _RulesCopy) -> Iterator[tuple[TestClient, Path]]:
    rules = tmp_path / "rules"
    _write_rules(rules, copy)
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + (tmp_path / "gates.db").resolve().as_posix(),
        rules_dir=str(rules),
        ramp_webhook_secret="test-secret",
    )
    with TestClient(create_app(settings, start_worker=False)) as client:
        client.headers["X-Requested-With"] = "taxleak"
        yield client, rules


def _login(client: TestClient, key: str) -> None:
    response = client.post("/api/v1/demo/login", json={}, headers=_headers(key))
    assert response.status_code == 200, response.text
    token = response.json().get("csrf_token")
    if token:
        client.headers["X-CSRF-Token"] = token


def _scenario(client: TestClient, scenario: str, key: str) -> dict[str, object]:
    # IDs-only webhooks (#25): the purchase exists after the worker's fetch, so
    # use the shared helper that resolves it from the job and runs the pipeline.
    return _run(client, scenario, key)


def _detail(client: TestClient, purchase_id: object, key: str) -> dict[str, object]:
    response = client.get(f"/api/v1/purchases/{purchase_id}", headers=_headers(key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _totals(client: TestClient, key: str) -> dict[str, object]:
    response = client.get("/api/v1/totals", headers=_headers(key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _stored(client: TestClient) -> tuple[_Stored, ...]:
    session = _open(client)
    try:
        rows = session.scalars(select(Determination).order_by(Determination.id)).all()
        return tuple(
            _Stored(
                id=row.id,
                is_current=row.is_current,
                unit_status=row.unit_status,
                engine_amount=row.engine_amount,
                expected_tax=row.expected_tax,
                label=row.label,
                facts_version=row.facts_version,
                rule_reference=row.rule_reference,
                rate=row.rate,
                engine_request=row.engine_request,
                engine_response=row.engine_response,
            )
            for row in rows
        )
    finally:
        session.close()


def _first(detail: dict[str, object]) -> dict[str, object]:
    rows = detail["determinations"]
    assert isinstance(rows, list) and len(rows) == 1
    row = rows[0]
    assert isinstance(row, dict)
    return row


def _money(body: dict[str, object], key: str) -> str:
    amount = body[key]
    assert isinstance(amount, dict)
    value = amount["value"]
    assert isinstance(value, str)
    return value


def _case_request(scenario_id: str) -> EngineLineRequest:
    scenario = get_scenario(scenario_id)
    assert scenario is not None
    line = scenario.lines[0]
    assert line.service_start is not None
    assert line.location_state is not None
    assert line.location_name is not None
    assert line.tax_charged is not None
    return EngineLineRequest(
        jurisdiction=line.location_state,
        category=scenario.category,
        invoice_date=date.fromisoformat(scenario.invoice_date),
        service_period_start=date.fromisoformat(line.service_start),
        delivery_date=None,
        line_amount=line.line_amount,
        tax_charged=line.tax_charged,
        shipping=line.shipping,
        location_name=line.location_name,
        state_count=1,
    )


def _assert_no_engine_amount(result: EngineLineResult) -> None:
    assert result.outcome != "supported"
    assert result.expected_tax is None
    assert result.adjustment_amount is None
    assert result.rule_reference is None
    assert result.rate is None


def _assert_api_has_no_amount(determination: dict[str, object]) -> None:
    assert determination["label"] != "ENGINE_SUPPORTED"
    assert determination["engine_amount"] is None
    assert determination["expected_tax"] is None
    assert determination["rule_reference"] is None


def _assert_db_has_no_amount(client: TestClient) -> None:
    rows = _stored(client)
    assert rows
    assert all(row.engine_amount is None and row.expected_tax is None for row in rows)


def _approved_values(totals: dict[str, object]) -> list[str]:
    liabilities = totals["approved_liabilities"]
    assert isinstance(liabilities, dict)
    rows = liabilities["by_jurisdiction"]
    assert isinstance(rows, list)
    values: list[str] = []
    for row in rows:
        assert isinstance(row, dict)
        amount = row["total"]
        assert isinstance(amount, dict)
        value = amount["value"]
        assert isinstance(value, str)
        values.append(value)
    return values


def _unreviewed(totals: dict[str, object]) -> int:
    block = totals["unreviewed"]
    assert isinstance(block, dict)
    return sum(int(row["count"]) for row in block["by_direction"])


def _assert_excluded(totals: dict[str, object], *amounts: str) -> None:
    values = _approved_values(totals)
    assert values == []
    assert totals["approved_liabilities"]["message"] == "No approved liabilities"
    for amount in amounts:
        assert amount not in values
    refunds = totals["refund_candidates"]
    assert isinstance(refunds, dict)
    assert refunds["count"] == 0
    assert refunds["total"] is None
    assert refunds["label"] == "not recovered cash"


def _assert_only_named_reviews_changed(ruleset: Ruleset) -> None:
    repo = load_ruleset(ROOT / "rules")
    for rule in ruleset.rules:
        original = next(item for item in repo.rules if item.id == rule.id)
        if rule.id == _WA_RULE:
            assert rule.review_status == "reviewed"
            assert rule.reviewed_by == "AF"
            assert rule_is_active(rule)
            continue
        assert rule.review_status == original.review_status
        assert rule.reviewed_by == original.reviewed_by
    for rate in ruleset.rates:
        original = next(
            item
            for item in repo.rates
            if item.jurisdiction == rate.jurisdiction
            and item.location == rate.location
            and item.quarter == rate.quarter
        )
        if rate.location == _SEATTLE and rate.jurisdiction == "WA":
            assert rate.review_status == "reviewed"
            assert rate.reviewed_by == "AF"
            assert rate_is_active(rate)
            continue
        assert rate.review_status == original.review_status
        assert rate.reviewed_by == original.reviewed_by
    for entry in ruleset.catalog:
        original = next(
            item for item in repo.catalog if item.category == entry.category
        )
        if entry.category == _SAAS:
            assert catalog_is_signed(entry)
            continue
        assert entry.status == original.status
        assert entry.signed_off_by == original.signed_off_by
    placeholder = next(rule for rule in ruleset.rules if rule.id == _PLACEHOLDER)
    assert placeholder.review_status == "candidate"
    assert not rule_is_active(placeholder)


def _assert_cited_rule_and_rate(ruleset: Ruleset, detail: dict[str, object]) -> None:
    determination = _first(detail)
    assert determination["label"] == "ENGINE_SUPPORTED"
    reference = determination["rule_reference"]
    assert isinstance(reference, str)
    assert not is_placeholder(reference)
    rule = next(item for item in ruleset.rules if item.id == reference)
    assert rule.review_status == "reviewed"
    assert rule.reviewed_by
    assert rule_is_active(rule)
    assert rule.jurisdiction == determination["jurisdiction"]
    assert rule.category == detail["category"]
    assert rule.applies_when != "multi_state"
    lines = detail["lines"]
    assert isinstance(lines, list) and lines
    line = lines[0]
    assert isinstance(line, dict)
    service_start = line["service_period_start"]
    location = line["location_name"]
    assert isinstance(service_start, str)
    assert isinstance(location, str)
    when = date.fromisoformat(service_start)
    assert rule.timing_basis == "service_period_start"
    assert rule.effective_from <= when
    if rule.effective_to is not None:
        assert when <= rule.effective_to
    invoice = detail["invoice_date"]
    cited_rate = determination["rate"]
    cited_code = determination["location_code"]
    assert isinstance(invoice, str)
    assert isinstance(cited_rate, str)
    assert isinstance(cited_code, str)
    matches = [
        rate
        for rate in ruleset.rates
        if rate.rate == cited_rate
        and rate.jurisdiction == determination["jurisdiction"]
        and rate.location == location
        and rate.quarter == quarter_key(date.fromisoformat(invoice))
        and rate.location_code == cited_code
    ]
    assert len(matches) == 1
    rate = matches[0]
    assert rate.review_status == "reviewed"
    assert rate.reviewed_by
    assert rate_is_active(rate)
    assert rate.jurisdiction == rule.jurisdiction
    assert rate.location == _SEATTLE
    response = determination["engine_response"]
    assert isinstance(response, dict)
    engine = _money(determination, "engine_amount")
    assert engine == _money(determination, "expected_tax")
    assert engine == response["adjustment_amount"]
    assert _money(determination, "expected_tax") == response["expected_tax"]


def _exact_base(rate: str, original: str) -> str:
    """A different sourced amount the rate can compute in exact cents."""
    rate_amount = parse_decimal(rate)
    for whole in range(500, 4000):
        candidate = f"{whole}.00"
        if candidate == original:
            continue
        product = emit_cents(parse_decimal(candidate) * rate_amount)
        if product is not None:
            return candidate
    raise AssertionError("no sourced amount is an exact multiple of the rate")


def test_repo_placeholder_stays_inactive() -> None:
    repo = load_ruleset(ROOT / "rules")
    placeholder = next(rule for rule in repo.rules if rule.id == _PLACEHOLDER)
    assert placeholder.review_status == "candidate"
    assert placeholder.reviewed_by is None
    assert is_placeholder(placeholder.id)
    assert not rule_is_active(placeholder)
    text = (ROOT / "rules" / "entries.yaml").read_text(encoding="utf-8")
    assert f'id: "{_PLACEHOLDER}"' in text or f"id: {_PLACEHOLDER}" in text


def test_h1_reads_only_two_labels() -> None:
    source = (ROOT / "app" / "contracts" / "enums.py").read_text(encoding="utf-8")
    start = source.index("class Label")
    body = source[start : source.index("\nclass ", start)]
    assignments: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("class ", '"""', "#")):
            continue
        if "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name.isidentifier():
            assignments.append(name)
    assert assignments == ["ENGINE_SUPPORTED", "NEEDS_HUMAN_REVIEW"]
    assert [item.value for item in Label] == [
        "ENGINE_SUPPORTED",
        "NEEDS_HUMAN_REVIEW",
    ]


def test_january_2027_on_the_shipped_evaluator_is_unresolved() -> None:
    result = evaluate_line(load_ruleset(ROOT / "rules"), _case_request("case4"))
    assert result.nhr_reason == "RULE_TRANSITION_UNRESOLVED"
    assert result.outcome != "supported"
    _assert_no_engine_amount(result)


def test_supported_result_cites_reviewed_applicable_rule_and_rate(
    tmp_path: Path,
) -> None:
    copy = _RulesCopy(
        rule_status="reviewed", rate_status="reviewed", catalog_signed=True
    )
    with _client(tmp_path, copy) as (client, rules):
        _login(client, "supported-login")
        ruleset = load_ruleset(rules)
        _assert_only_named_reviews_changed(ruleset)
        created = _scenario(client, "case1", "supported-case")
        detail = _detail(client, created["purchase_id"], "supported-get")
        _assert_cited_rule_and_rate(ruleset, detail)


def test_case_4_with_signed_catalog_is_unresolved(tmp_path: Path) -> None:
    with _client(tmp_path, _RulesCopy(catalog_signed=True)) as (client, rules):
        _login(client, "case4-login")
        ruleset = load_ruleset(rules)
        repo = load_ruleset(ROOT / "rules")
        assert [(rule.id, rule.review_status) for rule in ruleset.rules] == [
            (rule.id, rule.review_status) for rule in repo.rules
        ]
        assert [(rate.location, rate.review_status) for rate in ruleset.rates] == [
            (rate.location, rate.review_status) for rate in repo.rates
        ]
        placeholder = next(rule for rule in ruleset.rules if rule.id == _PLACEHOLDER)
        assert placeholder.review_status == "candidate"
        assert not rule_is_active(placeholder)
        saas = next(entry for entry in ruleset.catalog if entry.category == _SAAS)
        assert catalog_is_signed(saas)
        created = _scenario(client, "case4", "case4")
        detail = _detail(client, created["purchase_id"], "case4-get")
        determination = _first(detail)
        assert determination["label"] == "NEEDS_HUMAN_REVIEW"
        assert determination["nhr_reason"] == "RULE_TRANSITION_UNRESOLVED"
        _assert_api_has_no_amount(determination)
        _assert_db_has_no_amount(client)


@pytest.mark.parametrize(
    ("rule_status", "rate_status"),
    [("signed", "reviewed"), ("reviewed", "signed")],
)
def test_signed_does_not_activate_a_rule_or_a_rate(
    tmp_path: Path, rule_status: str, rate_status: str
) -> None:
    copy = _RulesCopy(
        rule_status=rule_status,
        rate_status=rate_status,
        catalog_signed=True,
    )
    with _client(tmp_path, copy) as (client, rules):
        _login(client, f"signed-login-{rule_status}-{rate_status}")
        ruleset = load_ruleset(rules)
        wa_rule = next(rule for rule in ruleset.rules if rule.id == _WA_RULE)
        seattle = next(
            rate
            for rate in ruleset.rates
            if rate.location == _SEATTLE and rate.jurisdiction == "WA"
        )
        saas = next(entry for entry in ruleset.catalog if entry.category == _SAAS)
        assert catalog_is_signed(saas)
        assert wa_rule.review_status == rule_status
        assert seattle.review_status == rate_status
        assert wa_rule.reviewed_by == "AF"
        assert seattle.reviewed_by == "AF"
        if rule_status == "signed":
            assert not rule_is_active(wa_rule)
        if rate_status == "signed":
            assert not rate_is_active(seattle)
        placeholder = next(rule for rule in ruleset.rules if rule.id == _PLACEHOLDER)
        assert placeholder.review_status == "candidate"
        assert not rule_is_active(placeholder)
        result = evaluate_line(ruleset, _case_request("case1"))
        _assert_no_engine_amount(result)
        if rule_status == "signed":
            assert result.nhr_reason == "ENGINE_UNSUPPORTED"
        else:
            assert result.error_code == "MISSING_RATE_REFERENCE"
        created = _scenario(client, "case1", f"signed-{rule_status}-{rate_status}")
        detail = _detail(
            client, created["purchase_id"], f"signed-get-{rule_status}-{rate_status}"
        )
        determination = _first(detail)
        _assert_api_has_no_amount(determination)
        _assert_db_has_no_amount(client)
        if rule_status == "signed":
            assert determination["label"] == "NEEDS_HUMAN_REVIEW"
            assert determination["nhr_reason"] == "ENGINE_UNSUPPORTED"
        else:
            assert determination["label"] is None
            assert determination["error_code"] == "MISSING_RATE_REFERENCE"


@pytest.mark.parametrize("status", ["signed", "reviewed"])
def test_placeholder_status_never_supports(tmp_path: Path, status: str) -> None:
    rules = tmp_path / "placeholder-rules"
    _write_rules(rules, _RulesCopy(placeholder_status=status, catalog_signed=True))
    ruleset = load_ruleset(rules)
    placeholder = next(rule for rule in ruleset.rules if rule.id == _PLACEHOLDER)
    assert placeholder.review_status == status
    assert placeholder.reviewed_by == "AF"
    assert is_placeholder(placeholder.id)
    assert not rule_is_active(placeholder)
    result = evaluate_line(ruleset, _case_request("case4"))
    assert result.nhr_reason == "RULE_TRANSITION_UNRESOLVED"
    _assert_no_engine_amount(result)


def test_h5_forbidden_fields_leave_amounts_and_correction_awaits_review(
    tmp_path: Path,
) -> None:
    copy = _RulesCopy(
        rule_status="reviewed", rate_status="reviewed", catalog_signed=True
    )
    with _client(tmp_path, copy) as (client, rules):
        _login(client, "h5-login")
        ruleset = load_ruleset(rules)
        created = _scenario(client, "case1", "h5-case")
        purchase_id = created["purchase_id"]
        detail = _detail(client, purchase_id, "h5-get")
        determination = _first(detail)
        assert determination["direction"] == "LIABILITY"
        assert determination["unit_status"] == "READY_FOR_REVIEW"
        prior_amount = _money(determination, "engine_amount")
        prior_id = determination["id"]
        assert isinstance(prior_id, str)
        request = determination["engine_request"]
        assert isinstance(request, dict)
        original_base = request["line_amount"]
        assert isinstance(original_base, str)
        seattle = next(
            rate.rate
            for rate in ruleset.rates
            if rate.location == _SEATTLE and rate_is_active(rate)
        )
        corrected = _exact_base(seattle, original_base)
        assert corrected != original_base
        approved = client.post(
            f"/api/v1/determinations/{prior_id}/review",
            json={
                "action": "approve",
                "approval_path": "LIABILITY",
                "reason": "Reviewed the engine result",
                "expected_version": determination["version"],
            },
            headers=_headers("h5-approve"),
        )
        assert approved.status_code == 200, approved.text
        approved_detail = _detail(client, purchase_id, "h5-approved")
        assert approved_detail["facts_version"] == detail["facts_version"]
        assert _first(approved_detail)["unit_status"] == "APPROVED"
        baseline_rows = _stored(client)
        baseline_totals = _totals(client, "h5-baseline-totals")
        assert prior_amount in _approved_values(baseline_totals)
        facts_version = approved_detail["facts_version"]
        assert isinstance(facts_version, int)
        for field in _FORBIDDEN:
            before_rows = _stored(client)
            before_totals = _totals(client, f"h5-before-{field}")
            assert before_rows == baseline_rows
            assert before_totals == baseline_totals
            refused = client.post(
                f"/api/v1/purchases/{purchase_id}/facts",
                json={
                    "field": field,
                    "value": "no",
                    "source": "test",
                    "reason": "no",
                    "expected_version": facts_version,
                },
                headers=_headers(f"h5-bad-{field}"),
            )
            assert refused.status_code == 422
            assert refused.json()["error"]["code"] == "FORBIDDEN_FIELD"
            assert "0.00" not in refused.text
            assert _stored(client) == baseline_rows
            assert _totals(client, f"h5-after-{field}") == baseline_totals
        still = _detail(client, purchase_id, "h5-still")
        assert still["facts_version"] == facts_version
        assert _first(still)["id"] == prior_id
        assert _first(still)["unit_status"] == "APPROVED"
        posted = client.post(
            f"/api/v1/purchases/{purchase_id}/facts",
            json={
                "field": "line_amount",
                "value": corrected,
                "source": "invoice",
                "reason": "confirm printed amount",
                "expected_version": facts_version,
            },
            headers=_headers("h5-correct"),
        )
        assert posted.status_code == 200, posted.text
        # Ramp's charge total agrees with the corrected invoice (as a later
        # transactions.cleared fetch would record), so the one-cent cross-check
        # admits the corrected amount instead of holding it.
        with client.app.state.ctx.session_factory() as session:
            purchase = session.get(Purchase, purchase_id)
            assert purchase is not None
            purchase.ramp_entity_amount = corrected
            session.commit()
        pending = posted.json()
        assert isinstance(pending, dict)
        assert pending["facts_version"] == facts_version + 1
        assert pending["determinations"] == []
        before_worker = _stored(client)
        old = next(row for row in before_worker if row.id == prior_id)
        assert old.is_current is False
        assert old.unit_status == "SUPERSEDED"
        assert old.engine_amount == prior_amount
        assert [row for row in before_worker if row.is_current] == []
        before_totals = _totals(client, "h5-before-worker")
        _assert_excluded(before_totals, prior_amount)
        assert _unreviewed(before_totals) == 0
        assert before_totals["open_needs_human_review"]["count"] == 0
        assert poll_once(client.app.state.ctx) == 1
        later = _detail(client, purchase_id, "h5-later")
        assert later["facts_version"] == facts_version + 1
        current = _first(later)
        assert current["id"] != prior_id
        assert current["unit_status"] == "READY_FOR_REVIEW"
        assert current["label"] == "ENGINE_SUPPORTED"
        _assert_cited_rule_and_rate(ruleset, later)
        # H5: the sourced correction feeds the engine request (not the old line).
        assert current["engine_request"]["line_amount"] == corrected
        assert corrected != original_base
        new_amount = _money(current, "engine_amount")
        assert new_amount != prior_amount
        amounts = [
            fact["value"]
            for fact in later["facts"]
            if isinstance(fact, dict) and fact["field"] == "line_amount"
        ]
        assert amounts == [corrected]
        after_worker = _stored(client)
        stored_old = next(row for row in after_worker if row.id == prior_id)
        stored_new = next(row for row in after_worker if row.is_current)
        assert stored_old.is_current is False
        assert stored_old.unit_status == "SUPERSEDED"
        assert stored_old.engine_amount == prior_amount
        assert stored_new.id == current["id"]
        assert stored_new.unit_status == "READY_FOR_REVIEW"
        assert stored_new.engine_amount == new_amount
        assert stored_new.engine_request is not None
        assert json.loads(stored_new.engine_request)["line_amount"] == corrected
        after_totals = _totals(client, "h5-after-worker")
        _assert_excluded(after_totals, prior_amount, new_amount)
        assert _unreviewed(after_totals) == 1
        assert after_totals["open_needs_human_review"]["count"] == 0
