"""RG-4, the negative half of RG-9, H1, and H5. Repo YAML is not edited."""

from dataclasses import replace
from datetime import date
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.config import load_settings
from app.contracts.enums import Label
from app.domain.evaluate import EngineLineRequest, evaluate_line
from app.domain.ruleset import is_placeholder, load_ruleset, rule_is_active
from app.main import create_app
from app.worker.poller import poll_once
from tests.api.test_flow import GOOD_ENV, ROOT, _headers

_PLACEHOLDER = "PLACEHOLDER:CA-DP-001"


def _copy_signed_saas(dest: Path) -> None:
    dest.mkdir()
    entries = yaml.safe_load((ROOT / "rules" / "entries.yaml").read_text())
    rates = yaml.safe_load((ROOT / "rules" / "rates.yaml").read_text())
    catalog = yaml.safe_load((ROOT / "rules" / "catalog.yaml").read_text())
    for rule in entries["rules"]:
        if rule["id"] == "WA-RAS-001":
            rule["review_status"] = "reviewed"
            rule["reviewed_by"] = "AF"
    for rate in rates["rates"]:
        if rate["location"] == "Seattle, WA":
            rate["review_status"] = "reviewed"
            rate["reviewed_by"] = "AF"
    for entry in catalog["entries"]:
        if entry["category"] == "REMOTE_ACCESS_SAAS":
            entry["status"] = "signed"
            entry["signed_off_by"] = "AF"
    (dest / "entries.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
    (dest / "rates.yaml").write_text(yaml.safe_dump(rates), encoding="utf-8")
    (dest / "catalog.yaml").write_text(yaml.safe_dump(catalog), encoding="utf-8")


def _client(tmp_path: Path) -> TestClient:
    rules = tmp_path / "rules"
    _copy_signed_saas(rules)
    database = tmp_path / "gates.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(rules),
        ramp_webhook_secret="test-secret",
    )
    return TestClient(create_app(settings, start_worker=False))


def test_repo_rules_stay_candidate_after_the_temp_copy() -> None:
    repo = load_ruleset(ROOT / "rules")
    placeholder = next(rule for rule in repo.rules if rule.id == _PLACEHOLDER)
    assert placeholder.review_status == "candidate"
    assert placeholder.reviewed_by is None
    assert not rule_is_active(placeholder)


def test_h1_allows_only_two_labels() -> None:
    source = (ROOT / "app" / "contracts" / "enums.py").read_text(encoding="utf-8")
    assert list(Label) == [Label.ENGINE_SUPPORTED, Label.NEEDS_HUMAN_REVIEW]
    assert "ENGINE_SUPPORTED" in source
    assert "NEEDS_HUMAN_REVIEW" in source
    assert source.count("ENGINE_SUPPORTED") >= 1


def test_january_2027_is_unresolved_transition() -> None:
    result = evaluate_line(
        load_ruleset(ROOT / "rules"),
        EngineLineRequest(
            jurisdiction="CA",
            category="REMOTE_ACCESS_SAAS",
            invoice_date=date(2026, 12, 15),
            service_period_start=date(2027, 1, 1),
            delivery_date=None,
            line_amount="962.32",
            tax_charged="83.00",
            shipping=None,
            location_name="San Francisco, CA",
            state_count=1,
        ),
    )
    assert result.nhr_reason == "RULE_TRANSITION_UNRESOLVED"
    assert result.expected_tax is None
    assert result.adjustment_amount is None
    assert result.outcome != "supported"


def test_supported_result_cites_reviewed_rule_and_rate(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        login = client.post("/api/v1/demo/login", json={}, headers=_headers("login"))
        token = login.json().get("csrf_token")
        if token:
            client.headers["X-CSRF-Token"] = token
        created = client.post(
            "/api/v1/demo/events",
            json={"scenario": "case1"},
            headers=_headers("case1"),
        )
        assert created.status_code == 200, created.text
        poll_once(client.app.state.ctx)
        detail = client.get(
            f"/api/v1/purchases/{created.json()['purchase_id']}",
            headers=_headers("get"),
        ).json()
        determination = detail["determinations"][0]
        assert determination["label"] == "ENGINE_SUPPORTED"
        assert determination["rule_reference"] == "WA-RAS-001"
        assert determination["rate"]
        assert not is_placeholder(determination["rule_reference"])
        engine = determination["engine_amount"]["value"]
        assert engine == determination["expected_tax"]["value"]


def test_case_4_with_signed_catalog_is_nhr_not_placeholder(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        login = client.post("/api/v1/demo/login", json={}, headers=_headers("login"))
        token = login.json().get("csrf_token")
        if token:
            client.headers["X-CSRF-Token"] = token
        created = client.post(
            "/api/v1/demo/events",
            json={"scenario": "case4"},
            headers=_headers("case4"),
        )
        poll_once(client.app.state.ctx)
        detail = client.get(
            f"/api/v1/purchases/{created.json()['purchase_id']}",
            headers=_headers("get4"),
        ).json()
        determination = detail["determinations"][0]
        assert determination["label"] == "NEEDS_HUMAN_REVIEW"
        assert determination["engine_amount"] is None
        assert determination["rule_reference"] is None


def test_h5_forbidden_fields_and_one_amount_correction(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        login = client.post("/api/v1/demo/login", json={}, headers=_headers("login"))
        token = login.json().get("csrf_token")
        if token:
            client.headers["X-CSRF-Token"] = token
        created = client.post(
            "/api/v1/demo/events",
            json={"scenario": "case1"},
            headers=_headers("h5"),
        )
        poll_once(client.app.state.ctx)
        detail = client.get(
            f"/api/v1/purchases/{created.json()['purchase_id']}",
            headers=_headers("get-h5"),
        ).json()
        determination = detail["determinations"][0]
        engine = determination["engine_amount"]["value"]
        approved = client.post(
            f"/api/v1/determinations/{determination['id']}/review",
            json={
                "action": "approve",
                "approval_path": "LIABILITY",
                "reason": "ok",
                "expected_version": determination["version"],
            },
            headers=_headers("approve-h5"),
        )
        assert approved.status_code == 200, approved.text
        purchase_id = created.json()["purchase_id"]
        for field in ("treatment", "label", "direction", "tax_decision_amount"):
            refused = client.post(
                f"/api/v1/purchases/{purchase_id}/facts",
                json={
                    "field": field,
                    "value": "no",
                    "source": "test",
                    "reason": "no",
                    "expected_version": detail["facts_version"],
                },
                headers=_headers(f"bad-{field}"),
            )
            assert refused.status_code == 422
            assert refused.json()["error"]["code"] == "FORBIDDEN_FIELD"
            assert "0.00" not in json_safe(refused.json())
        corrected = client.post(
            f"/api/v1/purchases/{purchase_id}/facts",
            json={
                "field": "line_amount",
                "value": "1440.00",
                "source": "invoice",
                "reason": "confirm printed amount",
                "expected_version": detail["facts_version"],
            },
            headers=_headers("ok-amount"),
        )
        assert corrected.status_code == 200, corrected.text
        poll_once(client.app.state.ctx)
        later = client.get(
            f"/api/v1/purchases/{purchase_id}", headers=_headers("after")
        ).json()
        assert later["facts_version"] > detail["facts_version"]
        current = later["determinations"][0]
        assert (
            current["id"] != determination["id"] or current["unit_status"] != "APPROVED"
        )
        totals = client.get("/api/v1/totals", headers=_headers("tot")).json()
        message = totals.get("approved_liabilities", {})
        if isinstance(message, dict):
            assert message.get("message") == "No approved liabilities" or not any(
                row.get("total", {}).get("value") == engine
                and current["unit_status"] != "APPROVED"
                for row in message.get("by_jurisdiction", [])
            )
        del engine


def json_safe(body: object) -> str:
    import json

    return json.dumps(body)
