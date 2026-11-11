"""Task 13: end-to-end Case 2 (CA vs WA) and Case 5 (NY hardware).

Temp-signed copies of rules/ only; repo YAML is unchanged. Expected figures
are illustrative until the owners sign the goldens.
"""

import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from tests.api.test_flow import GOOD_ENV, ROOT, _activate, _headers, _run, login
from tests.api.test_flow import signed_client as signed_client  # noqa: F401


@pytest.fixture
def rounding_client(tmp_path: Path, _reset_csrf_token: None) -> Iterator[TestClient]:
    """signed_client plus AF's reviewed rounding basis, copied unchanged."""
    rules = tmp_path / "rules"
    _activate(rules)
    shutil.copy(ROOT / "rules" / "rounding.yaml", rules / "rounding.yaml")
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + (tmp_path / "app.db").resolve().as_posix(),
        rules_dir=str(rules),
        ramp_webhook_secret="test-secret",
    )
    with TestClient(create_app(settings, start_worker=False)) as client:
        login(client)
        yield client


def _result(client: TestClient, scenario: str) -> tuple[str, dict[str, object]]:
    created = _run(client, scenario, f"run-{scenario}")
    purchase_id = str(created["purchase_id"])
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    [determination] = detail["determinations"]
    return purchase_id, determination


def _approve(client: TestClient, det: dict[str, object], path: str, key: str) -> None:
    response = client.post(
        f"/api/v1/determinations/{det['id']}/review",
        json={
            "action": "approve",
            "approval_path": path,
            "reason": "Reviewed the engine result",
            "expected_version": det["version"],
        },
        headers=_headers(key),
    )
    assert response.status_code == 200, response.text


def test_case2_ca_charged_correctly_beside_case1_wa_liability(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    _wa_id, wa = _result(client, "case1")
    _ca_id, ca = _result(client, "case2")
    assert wa["direction"] == "LIABILITY"
    assert wa["rule_reference"] == "WA-RAS-001"
    assert ca["label"] == "ENGINE_SUPPORTED"
    assert ca["direction"] == "CHARGED_CORRECTLY"
    assert ca["rule_reference"] == "CA-RAS-001"
    assert ca["engine_amount"] is None
    # Spec §13 fail condition: the requests differ only by line, jurisdiction
    # and location.
    same = ("line_amount", "tax_charged", "category", "invoice_date", "shipping")
    for key in same:
        assert wa["engine_request"][key] == ca["engine_request"][key], key
    assert wa["engine_request"]["jurisdiction"] == "WA"
    assert ca["engine_request"]["jurisdiction"] == "CA"
    assert (
        wa["engine_request"]["location_name"] != ca["engine_request"]["location_name"]
    )
    # Approved separately; charged-correctly never enters a total.
    _approve(client, wa, "LIABILITY", "a-wa")
    _approve(client, ca, "CHARGED_CORRECTLY", "a-ca")
    totals = client.get("/api/v1/totals").json()
    rows = totals["approved_liabilities"]["by_jurisdiction"]
    assert [(row["jurisdiction"], row["total"]["value"]) for row in rows] == [
        ("WA", wa["engine_amount"]["value"])
    ]
    assert totals["refund_candidates"]["count"] == 0


def test_case5_ny_hardware_with_confirmed_delivery(
    rounding_client: TestClient,
) -> None:
    client = rounding_client
    purchase_id, det = _result(client, "case5")
    assert det["label"] == "ENGINE_SUPPORTED"
    assert det["direction"] == "LIABILITY"
    assert det["rule_reference"] == "NY-TPP-001"
    assert det["rate"] == "0.08875"
    assert det["location_code"] == "8081"
    assert det["engine_request"]["category"] == "COMPUTER_HARDWARE"
    assert det["engine_request"]["shipping"] == "0.00"
    # Illustrative until signed: 2100.00 x 0.08875 under AF's NY rounding basis.
    assert det["engine_amount"] == {"value": "186.38", "currency": "USD"}
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    delivery = [f for f in detail["facts"] if f["field"] == "delivery_evidence"]
    assert delivery and delivery[0]["confirmation"] == "CONFIRMED"
    _approve(client, det, "LIABILITY", "a-ny")
    rows = client.get("/api/v1/totals").json()["approved_liabilities"][
        "by_jurisdiction"
    ]
    assert [(row["jurisdiction"], row["total"]["value"]) for row in rows] == [
        ("NY", "186.38")
    ]


def test_case5_without_the_rounding_basis_is_an_error_with_no_amount(
    signed_client: TestClient,  # noqa: F811
) -> None:
    _purchase_id, det = _result(signed_client, "case5")
    assert det["unit_status"] == "DETERMINATION_ERROR"
    assert det["error_code"] == "INEXACT_CENTS"
    assert det["engine_amount"] is None
