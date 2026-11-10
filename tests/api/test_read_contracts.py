"""Determination, error, and job read contracts.

The spec case-1 object is illustrative. These tests do not compare it to live rows.
"""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.serialize import _determination_json, meta
from app.clock import utcnow
from app.config import load_settings
from app.contracts import DeterminationRead, ErrorBody, ErrorResponse, JobRead, Money
from app.contracts.determination import ILLUSTRATIVE_SPEC_CASE_1
from app.contracts.jobs import read_job
from app.main import create_app
from app.models import Determination, Job
from app.seed import ORG_ID

_MODES = {
    "DATA_MODE": "synthetic",
    "ENGINE_MODE": "mock_reviewed_ruleset",
    "DEMO_MODE": "true",
    "LLM_MODE": "replay",
}

_DETERMINATION_KEYS = {
    "id",
    "line_id",
    "jurisdiction",
    "facts_version",
    "ruleset_version",
    "label",
    "nhr_reason",
    "direction",
    "derived_direction",
    "rate",
    "location_code",
    "expected_tax",
    "engine_amount",
    "rule_reference",
    "engine_treatment",
    "unit_status",
    "version",
    "error_code",
    "engine_request",
    "engine_response",
    "refund_letter_id",
}

_BANNED = {"net", "savings", "recovered"}


def _client(tmp_path: Path) -> TestClient:
    database = tmp_path / "reads.db"
    settings = replace(
        load_settings(_MODES),
        database_url="sqlite:///" + database.resolve().as_posix(),
    )
    return TestClient(create_app(settings, start_worker=False))


def _row(
    *,
    label: str | None = "ENGINE_SUPPORTED",
    nhr_reason: str | None = None,
    direction: str | None = "LIABILITY",
    derived_direction: bool = True,
    rate: str | None = "0.1055",
    location_code: str | None = "1726",
    expected_tax: str | None = "10.00",
    engine_amount: str | None = "10.00",
    rule_reference: str | None = "WA-RAS-001",
    engine_treatment: str | None = "Retail sale",
    unit_status: str = "READY_FOR_REVIEW",
    error_code: str | None = None,
) -> Determination:
    return Determination(
        id="det-1",
        org_id=ORG_ID,
        purchase_id="purchase-1",
        line_id="line-1",
        jurisdiction="WA",
        facts_version=1,
        ruleset_version="rules-1",
        label=label,
        nhr_reason=nhr_reason,
        direction=direction,
        derived_direction=derived_direction,
        rate=rate,
        location_code=location_code,
        expected_tax=expected_tax,
        engine_amount=engine_amount,
        rule_reference=rule_reference,
        engine_treatment=engine_treatment,
        unit_status=unit_status,
        version=1,
        is_current=True,
        error_code=error_code,
    )


def test_determination_json_keeps_its_keys_and_copies_decimal_strings() -> None:
    payload = _determination_json(_row())
    assert set(payload) == _DETERMINATION_KEYS
    assert payload["engine_amount"] == {"value": "10.00", "currency": "USD"}
    assert payload["expected_tax"] == {"value": "10.00", "currency": "USD"}
    assert isinstance(payload["engine_amount"], dict)
    assert isinstance(payload["engine_amount"]["value"], str)
    assert payload["rate"] == "0.1055"
    assert payload["rule_reference"] == "WA-RAS-001"
    assert payload["engine_request"] is None
    assert payload["engine_response"] is None
    assert "data_mode" not in payload
    assert "engine_mode" not in payload
    assert "service_period_start" not in payload
    assert "evidence_ids" not in payload


def test_nhr_engine_amount_stays_null() -> None:
    payload = _determination_json(
        _row(
            label="NEEDS_HUMAN_REVIEW",
            nhr_reason="ENGINE_UNSUPPORTED",
            direction="LIABILITY",
            derived_direction=True,
            rate="0.1055",
            location_code="1726",
            expected_tax="10.00",
            engine_amount="10.00",
            rule_reference="WA-RAS-001",
            engine_treatment="Retail sale",
            unit_status="NHR_ENGINE",
        )
    )
    assert payload["engine_amount"] is None
    assert payload["expected_tax"] is None
    assert payload["direction"] is None
    assert payload["derived_direction"] is False
    assert payload["rate"] is None
    assert payload["rule_reference"] is None
    assert payload["nhr_reason"] == "ENGINE_UNSUPPORTED"


def test_error_determination_has_no_amount() -> None:
    payload = _determination_json(
        _row(
            label=None,
            direction=None,
            derived_direction=False,
            rate=None,
            expected_tax=None,
            engine_amount=None,
            rule_reference=None,
            engine_treatment=None,
            unit_status="DETERMINATION_ERROR",
            error_code="MALFORMED",
        )
    )
    assert payload["label"] is None
    assert payload["engine_amount"] is None
    assert payload["error_code"] == "MALFORMED"


def test_modes_stay_on_meta() -> None:
    parent = meta(load_settings(_MODES))
    assert parent["data_mode"] == "synthetic"
    assert parent["engine_mode"] == "mock_reviewed_ruleset"
    assert "data_mode" not in DeterminationRead.model_fields
    assert "engine_mode" not in DeterminationRead.model_fields
    assert "service_period_start" not in DeterminationRead.model_fields
    assert "evidence_ids" not in DeterminationRead.model_fields


def test_illustrative_spec_case_is_not_the_live_schema() -> None:
    extra = set(ILLUSTRATIVE_SPEC_CASE_1) - set(DeterminationRead.model_fields)
    assert extra == {
        "data_mode",
        "engine_mode",
        "evidence_ids",
        "service_period_start",
    }
    with pytest.raises(ValidationError):
        DeterminationRead.model_validate(ILLUSTRATIVE_SPEC_CASE_1)


def test_read_models_have_no_net_savings_or_recovered_field() -> None:
    for model in (DeterminationRead, ErrorBody, ErrorResponse, JobRead, Money):
        assert _BANNED.isdisjoint(model.model_fields)


def test_determination_money_rejects_a_binary_float() -> None:
    body = _determination_json(_row())
    body["engine_amount"] = {"value": 0.1, "currency": "USD"}
    with pytest.raises(ValidationError):
        DeterminationRead.model_validate(body)


@pytest.mark.parametrize("banned", sorted(_BANNED))
def test_determination_rejects_banned_fields(banned: str) -> None:
    body = _determination_json(_row())
    body[banned] = {"value": "1.00", "currency": "USD"}
    with pytest.raises(ValidationError):
        DeterminationRead.model_validate(body)


def test_error_envelope_keeps_error_code(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/api/v1/purchases/missing")
        assert response.status_code == 401
        body = response.json()
        assert set(body) == {"error"}
        error = body["error"]
        assert set(error) == {
            "code",
            "message",
            "field_errors",
            "retryable",
            "correlation_id",
        }
        assert error["code"] == "UNAUTHORIZED"
        assert error["retryable"] is False
        assert error["field_errors"] == []
        assert isinstance(error["correlation_id"], str)
        assert error["correlation_id"]


def test_job_read_reports_kind_as_stage_and_retryable(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        login = client.post(
            "/api/v1/demo/login",
            json={},
            headers={"Idempotency-Key": "login-jobs", "X-Requested-With": "taxleak"},
        )
        assert login.status_code == 200
        queued = _insert_job(client, kind="fetch", status="QUEUED", purchase_id=None)
        running = _insert_job(
            client, kind="advance", status="RUNNING", purchase_id=str(uuid4())
        )
        succeeded = _insert_job(
            client,
            kind="advance",
            status="SUCCEEDED",
            purchase_id=str(uuid4()),
            error_code=None,
            attempts=1,
        )
        failed = _insert_job(
            client,
            kind="advance",
            status="FAILED",
            purchase_id=str(uuid4()),
            error_code="ADVANCE_FAILED",
            attempts=3,
        )

        queued_body = client.get(f"/api/v1/jobs/{queued}").json()
        assert queued_body["id"] == queued
        assert queued_body["status"] == "QUEUED"
        assert queued_body["purchase_id"] is None
        assert queued_body["attempts"] == 0
        assert queued_body["error_code"] is None
        assert queued_body["stage"] == "fetch"
        assert queued_body["retryable"] is True
        assert queued_body["data_mode"] == "synthetic"
        assert queued_body["engine_mode"] == "mock_reviewed_ruleset"

        assert client.get(f"/api/v1/jobs/{running}").json()["retryable"] is True
        succeeded_body = client.get(f"/api/v1/jobs/{succeeded}").json()
        assert succeeded_body["retryable"] is False
        assert succeeded_body["stage"] == "advance"
        assert succeeded_body["attempts"] == 1
        failed_body = client.get(f"/api/v1/jobs/{failed}").json()
        assert failed_body["retryable"] is False
        assert failed_body["error_code"] == "ADVANCE_FAILED"

        missing = client.get(f"/api/v1/jobs/{uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"


def test_read_job_retryable_matches_status() -> None:
    assert read_job(
        id="job",
        status="QUEUED",
        purchase_id=None,
        attempts=0,
        error_code=None,
        stage="fetch",
    ).retryable
    assert read_job(
        id="job",
        status="RUNNING",
        purchase_id=None,
        attempts=1,
        error_code=None,
        stage="advance",
    ).retryable
    assert (
        read_job(
            id="job",
            status="SUCCEEDED",
            purchase_id="purchase",
            attempts=1,
            error_code=None,
            stage="advance",
        ).retryable
        is False
    )
    assert (
        read_job(
            id="job",
            status="FAILED",
            purchase_id="purchase",
            attempts=3,
            error_code="ADVANCE_FAILED",
            stage="advance",
        ).retryable
        is False
    )


def _insert_job(
    client: TestClient,
    *,
    kind: str,
    status: str,
    purchase_id: str | None,
    error_code: str | None = None,
    attempts: int = 0,
) -> str:
    job_id = str(uuid4())
    session = client.app.state.ctx.session_factory()
    try:
        session.add(
            Job(
                id=job_id,
                org_id=ORG_ID,
                kind=kind,
                purchase_id=purchase_id,
                status=status,
                lease_until=None,
                attempts=attempts,
                error_code=error_code,
                created_at=utcnow(),
            )
        )
        session.commit()
    finally:
        session.close()
    return job_id
