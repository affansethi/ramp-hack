"""Review list shows each unreviewed engine-supported amount, never in a total.

Covers issue #73 (epic #57, Design Spec section 4.2): an unreviewed
refund-direction result shows its amount on the review list, never in any
total. Uses the temp-signed rules pattern from test_totals.py.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.clock import utcnow
from app.config import load_settings
from app.main import create_app
from app.models import Determination, Purchase
from app.seed import ORG_ID
from tests.api.conftest import (
    assert_refund_candidates_stay_separate,
    copy_scenario_rules,
)
from tests.api.test_flow import GOOD_ENV, _headers, _run


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
_CASE3 = _Rules(_SAAS, _CA_RULE, _CA_RATE)
_CASE4 = _Rules(_SAAS, _CA_RULE, _CA_RATE)
_CASE6 = _Rules(_SAAS, _CA_RULE, _CA_RATE)


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


def _get(client: TestClient, path: str) -> dict[str, object]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _row_for(client: TestClient, purchase_id: str) -> dict[str, object]:
    body = _get(client, "/api/v1/purchases")
    rows = body["purchases"]
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        if row["id"] == purchase_id:
            return row
    raise AssertionError(f"purchase {purchase_id} missing from review list")


def _insert_error_purchase(client: TestClient) -> str:
    session = client.app.state.ctx.session_factory()
    try:
        purchase_id = str(uuid4())
        session.add(
            Purchase(
                id=purchase_id,
                org_id=ORG_ID,
                vendor="Synthetic Vendor",
                invoice_number=f"SYN-ERR-{purchase_id[:8]}",
                invoice_date="2026-11-15",
                entity_name="Acme Startup LLC",
                currency="USD",
                item_status="DETERMINED",
                extraction_mode="replay",
                facts_version=1,
                created_at=utcnow(),
            )
        )
        session.add(
            Determination(
                id=str(uuid4()),
                org_id=ORG_ID,
                purchase_id=purchase_id,
                jurisdiction="CA",
                facts_version=1,
                ruleset_version="test",
                label=None,
                direction=None,
                derived_direction=False,
                unit_status="DETERMINATION_ERROR",
                error_code="ENGINE_REQUEST_INVALID",
                version=1,
                is_current=True,
            )
        )
        session.commit()
        return purchase_id
    finally:
        session.close()


def test_case3_unreviewed_row_copies_engine_amount(tmp_path: Path) -> None:
    with _client(tmp_path, _CASE3) as client:
        created = _run(client, "case3", "case3")
        purchase_id = str(created["purchase_id"])
        detail = _get(client, f"/api/v1/purchases/{purchase_id}")
        determinations = detail["determinations"]
        assert isinstance(determinations, list) and len(determinations) == 1
        determination = determinations[0]
        assert isinstance(determination, dict)
        assert determination["direction"] == "REFUND_CANDIDATE"
        assert determination["unit_status"] == "READY_FOR_REVIEW"
        assert determination["engine_amount"] is not None

        row = _row_for(client, purchase_id)
        pending = row["pending_results"]
        assert isinstance(pending, list) and len(pending) == 1
        entry = pending[0]
        assert isinstance(entry, dict)
        assert entry["determination_id"] == determination["id"]
        assert entry["jurisdiction"] == determination["jurisdiction"]
        assert entry["direction"] == "REFUND_CANDIDATE"
        assert json.dumps(entry["engine_amount"], sort_keys=True) == json.dumps(
            determination["engine_amount"], sort_keys=True
        )
        assert entry["note"] == "unreviewed; not in any total"

        totals = _get(client, "/api/v1/totals")
        candidates = totals["refund_candidates"]
        assert isinstance(candidates, dict)
        assert candidates["count"] == 0
        assert candidates["total"] is None
        engine_value = determination["engine_amount"]["value"]
        assert isinstance(engine_value, str)
        assert engine_value not in json.dumps(candidates)
        liabilities = totals["approved_liabilities"]
        assert isinstance(liabilities, dict)
        assert liabilities["message"] == "No approved liabilities"
        assert_refund_candidates_stay_separate(totals)


def test_approved_refund_clears_pending_results(tmp_path: Path) -> None:
    with _client(tmp_path, _CASE3) as client:
        created = _run(client, "case3", "case3")
        purchase_id = str(created["purchase_id"])
        detail = _get(client, f"/api/v1/purchases/{purchase_id}")
        determinations = detail["determinations"]
        assert isinstance(determinations, list) and len(determinations) == 1
        determination = determinations[0]
        assert isinstance(determination, dict)
        approved = client.post(
            f"/api/v1/determinations/{determination['id']}/review",
            json={
                "action": "approve",
                "approval_path": "REFUND_PURSUIT",
                "reason": "Pursue",
                "expected_version": determination["version"],
            },
            headers=_headers("approve-refund"),
        )
        assert approved.status_code == 200, approved.text
        row = _row_for(client, purchase_id)
        assert row["pending_results"] == []


def test_case4_case6_and_error_have_no_pending_results(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, _CASE4) as client:
        case4 = _run(client, "case4", "case4")
        case6 = _run(client, "case6", "case6")
        error_id = _insert_error_purchase(client)
        for purchase_id in (
            str(case4["purchase_id"]),
            str(case6["purchase_id"]),
            error_id,
        ):
            row = _row_for(client, purchase_id)
            assert row["pending_results"] == [], purchase_id
            assert "engine_amount" not in row
            assert "expected_tax" not in row
        detail = _get(client, f"/api/v1/purchases/{error_id}")
        assert detail["queue"] == "error"
