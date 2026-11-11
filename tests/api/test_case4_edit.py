"""Case 4 edit: moving case 3's service period into January 2027 supersedes approval.

Every request sends X-Requested-With: taxleak. The temp rules copy signs
only the SaaS catalog entry, CA-RAS-001, and the San Francisco rate; the
word signed activates nothing else and PLACEHOLDER:CA-DP-001 stays a
candidate. Repo rules/ bytes are snapshotted and compared at the end.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import load_settings
from app.main import create_app
from app.models import Fact
from app.worker.poller import poll_once
from tests.api.conftest import (
    assert_refund_candidates_stay_separate,
    copy_scenario_rules,
    forbidden_total_field_names,
)
from tests.api.test_flow import GOOD_ENV, ROOT, _headers

_RULES = {
    "signed_categories": frozenset({"REMOTE_ACCESS_SAAS"}),
    "reviewed_rule_ids": frozenset({"CA-RAS-001"}),
    "reviewed_rates": frozenset({("CA", "San Francisco, CA")}),
}


def _get(client: TestClient, path: str) -> dict[str, object]:
    response = client.get(path, headers={"X-Requested-With": "taxleak"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


@contextmanager
def _client(tmp_path: Path) -> Iterator[TestClient]:
    repo = {
        name: (ROOT / "rules" / name).read_bytes()
        for name in ("entries.yaml", "rates.yaml", "catalog.yaml")
    }
    copy_scenario_rules(
        tmp_path / "rules",
        signed_categories=_RULES["signed_categories"],
        reviewed_rule_ids=_RULES["reviewed_rule_ids"],
        reviewed_rates=_RULES["reviewed_rates"],
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
            headers=_headers("login-case4-edit"),
        )
        assert login.status_code == 200, login.text
        token = login.json().get("csrf_token")
        if isinstance(token, str) and token:
            client.headers["X-CSRF-Token"] = token
        yield client
    for name, raw in repo.items():
        assert (ROOT / "rules" / name).read_bytes() == raw


def _run(client: TestClient, scenario: str, key: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/demo/events",
        json={"scenario": scenario},
        headers=_headers(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert poll_once(client.app.state.ctx) == 1 or body["duplicate"] is True
    return body


def _poll_for_facts_version(
    client: TestClient, purchase_id: str, facts_version: int
) -> dict[str, object]:
    detail: dict[str, object] = {}
    for _ in range(5):
        poll_once(client.app.state.ctx)
        detail = _get(client, f"/api/v1/purchases/{purchase_id}")
        determinations = detail["determinations"]
        assert isinstance(determinations, list)
        if any(
            isinstance(item, dict) and item.get("facts_version") == facts_version
            for item in determinations
        ):
            break
    return detail


def test_case4_edit_supersedes_december_approval(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = _run(client, "case3", "case3-edit")
        purchase_id = str(created["purchase_id"])
        detail = _get(client, f"/api/v1/purchases/{purchase_id}")
        assert detail["invoice_date"] == "2026-12-15"
        determinations = detail["determinations"]
        assert isinstance(determinations, list) and len(determinations) == 1
        december = determinations[0]
        assert isinstance(december, dict)
        assert december["label"] == "ENGINE_SUPPORTED"
        assert december["direction"] == "REFUND_CANDIDATE"
        assert december["rule_reference"] == "CA-RAS-001"
        engine_amount = december["engine_amount"]
        assert isinstance(engine_amount, dict)
        approved_value = str(engine_amount["value"])

        approved = client.post(
            f"/api/v1/determinations/{december['id']}/review",
            json={
                "action": "approve",
                "approval_path": "REFUND_PURSUIT",
                "reason": "Pursue the December overcharge",
                "expected_version": december["version"],
            },
            headers=_headers("approve-december"),
        )
        assert approved.status_code == 200, approved.text
        letter_id = approved.json()["determinations"][0]["refund_letter_id"]
        assert letter_id

        edited = client.post(
            f"/api/v1/purchases/{purchase_id}/facts",
            json={
                "field": "service_period",
                "value": "2027-01-01/2027-01-31",
                "source": "Corrected service dates from the order form",
                "reason": "Service moved to January",
                "expected_version": detail["facts_version"],
                "line_id": detail["lines"][0]["id"],
            },
            headers=_headers("edit-service-period"),
        )
        assert edited.status_code == 200, edited.text
        facts_version = int(edited.json()["facts_version"])

        detail = _poll_for_facts_version(client, purchase_id, facts_version)
        lines = detail["lines"]
        assert isinstance(lines, list) and len(lines) == 1
        assert lines[0]["service_period_start"] == "2027-01-01"
        assert lines[0]["service_period_end"] == "2027-01-31"
        assert detail["invoice_date"] == "2026-12-15"

        session = client.app.state.ctx.session_factory()
        try:
            periods = list(
                session.scalars(
                    select(Fact)
                    .where(
                        Fact.purchase_id == purchase_id,
                        Fact.field_key == "service_period",
                    )
                    .order_by(Fact.version)
                ).all()
            )
            assert len(periods) == 2
            assert periods[0].is_current is False
            assert periods[1].is_current is True
            assert periods[1].value == "2027-01-01/2027-01-31"
            assert periods[1].line_id == lines[0]["id"]
        finally:
            session.close()

        determinations = detail["determinations"]
        assert isinstance(determinations, list) and len(determinations) == 1
        current = determinations[0]
        assert isinstance(current, dict)
        assert current["facts_version"] == facts_version
        assert current["label"] == "NEEDS_HUMAN_REVIEW"
        assert current["nhr_reason"] == "RULE_TRANSITION_UNRESOLVED"
        assert current["engine_amount"] is None
        assert current["expected_tax"] is None
        assert current["rule_reference"] is None
        assert current["rule_reference"] != "PLACEHOLDER:CA-DP-001"
        assert current["refund_letter_id"] is None

        refused = client.post(
            f"/api/v1/determinations/{current['id']}/review",
            json={
                "action": "approve",
                "approval_path": "REFUND_PURSUIT",
                "reason": "Must stay unapprovable",
                "expected_version": current["version"],
            },
            headers=_headers("approve-nhr"),
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "NEEDS_HUMAN_REVIEW_NOT_APPROVABLE"

        totals = _get(client, "/api/v1/totals")
        liabilities = totals["approved_liabilities"]
        assert isinstance(liabilities, dict)
        assert liabilities["message"] == "No approved liabilities"
        assert approved_value not in json.dumps(liabilities)
        candidates = totals["refund_candidates"]
        assert isinstance(candidates, dict)
        assert candidates["count"] == 0
        assert candidates["total"] is None
        assert candidates["rows"] == []
        assert approved_value not in json.dumps(candidates)
        assert forbidden_total_field_names(totals) == []
        assert_refund_candidates_stay_separate(totals)
