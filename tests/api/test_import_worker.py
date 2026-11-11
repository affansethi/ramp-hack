"""Claimed imports validate before admission and never determine tax."""

# ruff: noqa: F811 -- pytest resolves the imported fixtures by name.

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.adapters.extractor import cache_key
from app.domain import pipeline
from app.models import (
    AuditEvent,
    Determination,
    Document,
    Evidence,
    Fact,
    Job,
    Purchase,
)
from app.worker import poller
from tests.api.test_flow import signed_client  # noqa: F401
from tests.api.test_imports import PNG, FakeStore, _get, _post, iclient  # noqa: F401


@pytest.fixture
def recordings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "imports.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(poller, "_IMPORT_RECORDINGS", path)
    monkeypatch.setenv("MODEL_PROVIDER", "test-provider")
    monkeypatch.setenv("MODEL_NAME", "test-model")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail("import ran the advance/engine path")

    monkeypatch.setattr(poller, "advance_purchase", unexpected)
    monkeypatch.setattr(pipeline, "advance_purchase", unexpected)
    monkeypatch.setattr(pipeline, "evaluate_line", unexpected)
    return path


def _payload() -> dict[str, Any]:
    # Printed source facts for a unit test, not a recording or tax golden.
    return {
        "invoice_identity": {"invoice_number": "SYN-IMPORT-49", "amount": "10.00"},
        "call_a": {
            "vendor_name": "Synthetic Import Vendor",
            "invoice_date": "2026-11-10",
            "currency": "USD",
            "bill_to": "Synthetic HQ, CA",
            "lines": [
                {
                    "description": "Synthetic subscription",
                    "quantity": "1",
                    "unit_price": "10.00",
                    "line_amount": "10.00",
                    "tax_charged": "0.00",
                }
            ],
            "evidence": [
                {
                    "page": 1,
                    "passage": (
                        "Acme Startup LLC; Synthetic Import Vendor; SYN-IMPORT-49; "
                        "2026-11-10; Synthetic subscription; 10.00; tax 0.00; "
                        "Synthetic HQ, CA"
                    ),
                }
            ],
        },
        "call_b": {"category": "REMOTE_ACCESS_SAAS"},
    }


def _save(path: Path, payload: object, content: bytes = PNG) -> None:
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved[cache_key(content, provider="test-provider", model="test-model")] = payload
    path.write_text(json.dumps(saved), encoding="utf-8")


def _upload(client: TestClient, key: str, content: bytes = PNG) -> dict[str, Any]:
    response = _post(client, key, content=content)
    assert response.status_code == 202, response.text
    return response.json()


def _detail(client: TestClient, created: dict[str, Any]) -> dict[str, Any]:
    response = _get(client, f"/api/v1/jobs/{created['job_id']}")
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["status"] == "SUCCEEDED"
    response = _get(client, f"/api/v1/purchases/{job['purchase_id']}")
    assert response.status_code == 200, response.text
    return response.json()


def _count(client: TestClient, model: type[object]) -> int:
    with client.app.state.ctx.session_factory() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_replay_miss_holds_without_invented_tax(
    iclient: tuple[TestClient, FakeStore], recordings: Path
) -> None:
    client, _ = iclient
    created = _upload(client, "worker-miss")
    assert poller.poll_once(client.app.state.ctx) == 1
    detail = _detail(client, created)
    assert detail["queue"] == "held"
    assert detail["extraction_mode"] == "replay_miss"
    assert detail["facts"] == []
    assert detail["determinations"] == []
    assert detail["holds"][0]["reason"] == "MISSING_REQUIRED_FACT"
    assert [d["id"] for d in detail["documents"]] == [created["import_id"]]
    assert _count(client, Fact) == _count(client, Determination) == 0
    assert _count(client, Job) == 1
    assert poller.poll_once(client.app.state.ctx) == 0


@pytest.mark.parametrize("placement", ["call_a", "call_b", "invoice_identity"])
def test_forbidden_field_aborts_before_admission(
    iclient: tuple[TestClient, FakeStore],
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
    placement: str,
) -> None:
    client, _ = iclient
    payload = _payload()
    payload[placement]["rate"] = "forbidden"
    _save(recordings, payload)

    def writer_ran(*_args: object, **_kwargs: object) -> None:
        pytest.fail("forbidden field reached the fact writer")

    monkeypatch.setattr(pipeline, "_write_facts", writer_ran)
    created = _upload(client, "worker-forbidden")
    assert poller.poll_once(client.app.state.ctx) == 1
    job = _get(client, f"/api/v1/jobs/{created['job_id']}").json()
    assert job["status"] == "FAILED"
    assert job["error_code"] == "FORBIDDEN_FIELD"
    assert job["purchase_id"] is None
    for model in (Purchase, Fact, Evidence, Determination):
        assert _count(client, model) == 0
    with client.app.state.ctx.session_factory() as session:
        assert session.get(Document, created["import_id"]).purchase_id is None
    assert _count(client, Job) == 1
    assert poller.poll_once(client.app.state.ctx) == 0


@pytest.mark.parametrize("tax", ["0.00", None, "absent"])
def test_validated_replay_uses_writer_and_preserves_missing_vs_zero(
    iclient: tuple[TestClient, FakeStore],
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
    tax: str | None,
) -> None:
    client, _ = iclient
    payload = _payload()
    if tax == "absent":
        del payload["call_a"]["lines"][0]["tax_charged"]
    else:
        payload["call_a"]["lines"][0]["tax_charged"] = tax
    _save(recordings, payload)
    written: list[str] = []
    writer = pipeline._write_facts

    def tracked(*args: Any, **kwargs: Any) -> None:
        written.append(args[2].id)
        writer(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_write_facts", tracked)
    created = _upload(client, "worker-valid")
    assert poller.poll_once(client.app.state.ctx) == 1
    detail = _detail(client, created)
    assert written == [created["import_id"]]
    assert detail["queue"] == "held"
    assert detail["extraction_mode"] == "replay"
    assert detail["determinations"] == []
    taxes = [f for f in detail["facts"] if f["field"] == "tax_charged"]
    assert [f["value"] for f in taxes] == (["0.00"] if tax == "0.00" else [])
    assert not any(
        f["field"] in ("delivery_method", "access_model", "user_location")
        for f in detail["facts"]
    )
    for fact in detail["facts"]:
        assert fact["model_output"] == "replay"
        assert fact["evidence"][0]["document_id"] == created["import_id"]
        assert (
            fact["evidence"][0]["passage"]
            == payload["call_a"]["evidence"][0]["passage"]
        )
    assert _count(client, Job) == 1
    assert poller.poll_once(client.app.state.ctx) == 0


def test_tax_proposal_without_its_printed_span_is_unconfirmed(
    iclient: tuple[TestClient, FakeStore], recordings: Path
) -> None:
    client, _ = iclient
    payload = _payload()
    payload["call_a"]["evidence"][0]["passage"] = "Synthetic subscription 10.00"
    _save(recordings, payload)
    created = _upload(client, "worker-ungrounded-tax")
    assert poller.poll_once(client.app.state.ctx) == 1
    detail = _detail(client, created)
    tax = next(f for f in detail["facts"] if f["field"] == "tax_charged")
    assert tax["confirmation"] == "UNCONFIRMED"
    assert detail["queue"] == "held"
    assert detail["determinations"] == []


@pytest.mark.parametrize("change", ["normalized", "amount", "date", "org"])
def test_invoice_identity_matches_only_with_corroboration(
    iclient: tuple[TestClient, FakeStore],
    recordings: Path,
    change: str,
) -> None:
    client, _ = iclient
    _save(recordings, _payload())
    first = _upload(client, "worker-first")
    assert poller.poll_once(client.app.state.ctx) == 1
    original = _detail(client, first)
    facts_before = _count(client, Fact)
    payload = _payload()
    payload["invoice_identity"]["invoice_number"] = " syn-import-49 "
    payload["call_a"]["vendor_name"] = " synthetic import vendor "
    if change == "amount":
        payload["invoice_identity"]["amount"] = "11.00"
    elif change == "date":
        payload["call_a"]["invoice_date"] = "2026-11-11"
    elif change == "org":
        with client.app.state.ctx.session_factory() as session:
            session.get(Purchase, original["id"]).org_id = "another-org"
            session.commit()
    content = PNG + b"second copy"
    _save(recordings, payload, content)
    second = _upload(client, "worker-second", content)
    assert poller.poll_once(client.app.state.ctx) == 1
    linked = _detail(client, second)
    if change == "normalized":
        assert linked["id"] == original["id"]
        assert {d["id"] for d in linked["documents"]} == {
            first["import_id"],
            second["import_id"],
        }
        assert _count(client, Purchase) == 1
        assert _count(client, Fact) == facts_before
    else:
        assert linked["id"] != original["id"]
        assert _count(client, Purchase) == 2
        if change != "org":
            with client.app.state.ctx.session_factory() as session:
                assert (
                    session.scalar(
                        select(AuditEvent).where(
                            AuditEvent.command.like("ambiguous_invoice_match:%")
                        )
                    )
                    is not None
                )
    assert _count(client, Job) == 2
    assert _count(client, Determination) == 0


def test_admission_runs_after_claim_and_recovery_has_no_duplicate_effects(
    iclient: tuple[TestClient, FakeStore],
    recordings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = iclient
    created = _upload(client, "worker-recovery")
    loader = poller._load_import_replay

    def after_claim(document: Document) -> object | None:
        with client.app.state.ctx.session_factory() as session:
            job = session.get(Job, created["job_id"])
            assert job.status == "RUNNING" and job.attempts == 1
            assert job.lease_until is not None
        return loader(document)

    monkeypatch.setattr(poller, "_load_import_replay", after_claim)
    assert poller.poll_once(client.app.state.ctx) == 1
    first = _detail(client, created)
    with client.app.state.ctx.session_factory() as session:
        job = session.get(Job, created["job_id"])
        job.status = "RUNNING"
        job.lease_until = "2000-01-01T00:00:00Z"
        session.commit()
    assert poller.poll_once(client.app.state.ctx) == 1
    assert _detail(client, created) == first
    assert (
        _count(client, Purchase) == _count(client, Document) == _count(client, Job) == 1
    )
