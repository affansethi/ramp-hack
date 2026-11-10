"""Trace panel fields: stored rule link, receipt documents, and fact evidence."""

import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.serialize import _determination_json
from app.domain.evaluate import EngineLineRequest, EngineLineResult, quarter_key
from app.domain.ruleset import load_ruleset
from app.models import Document, Evidence
from tests.api.test_flow import _open, _run, signed_client  # noqa: F401
from tests.api.test_read_contracts import _row

_EVIDENCE_KEYS = {"id", "document_id", "page", "passage", "provenance"}
_DOCUMENT_KEYS = {"id", "mime_type", "sha256", "pages"}


def test_rule_source_url_is_copied_from_the_stored_response() -> None:
    stored = "https://stored.example/rule "
    row = _row()
    row.engine_response = json.dumps({"rule_source_url": stored})
    assert _determination_json(row)["rule_source_url"] == stored

    # A row stored before this field existed has no link to fill in.
    previous = _row()
    previous.engine_response = json.dumps(
        {"rule_reference": "WA-RAS-001", "rate": "0.1055", "location_code": "1726"}
    )
    assert _determination_json(previous)["rule_source_url"] is None
    assert _determination_json(_row())["rule_source_url"] is None

    blocked = json.dumps({"rule_source_url": stored})
    nhr = _row(
        label="NEEDS_HUMAN_REVIEW",
        nhr_reason="RULE_TRANSITION_UNRESOLVED",
        unit_status="NHR_ENGINE",
    )
    nhr.engine_response = blocked
    assert _determination_json(nhr)["rule_source_url"] is None
    error = _row(
        label=None,
        direction=None,
        derived_direction=False,
        rate=None,
        location_code=None,
        expected_tax=None,
        engine_amount=None,
        rule_reference=None,
        engine_treatment=None,
        unit_status="DETERMINATION_ERROR",
        error_code="INEXACT_CENTS",
    )
    error.engine_response = blocked
    assert _determination_json(error)["rule_source_url"] is None


def test_case_1_copies_rule_link_rate_and_location(
    signed_client: TestClient,  # noqa: F811
) -> None:
    rules_dir = Path(signed_client.app.state.ctx.rules_dir)
    ruleset = load_ruleset(rules_dir)
    created = _run(signed_client, "case1", "trace-case1")
    detail = _detail(signed_client, created["purchase_id"])
    _assert_sources(signed_client, created["purchase_id"], detail)
    determination = _one(detail)
    response = determination["engine_response"]
    request = determination["engine_request"]
    assert isinstance(response, dict)
    assert isinstance(request, dict)
    rule = next(
        item for item in ruleset.rules if item.id == determination["rule_reference"]
    )
    line = _line(detail, determination["line_id"])
    rate = next(
        item
        for item in ruleset.rates
        if item.jurisdiction == determination["jurisdiction"]
        and item.location == line["location_name"]
        and item.quarter == quarter_key(date.fromisoformat(str(detail["invoice_date"])))
    )
    assert (
        determination["rule_source_url"]
        == response["rule_source_url"]
        == rule.primary_source_url
    )
    assert determination["rate"] == response["rate"] == rate.rate
    assert (
        determination["location_code"]
        == response["location_code"]
        == rate.location_code
    )
    assert determination["rule_reference"] == response["rule_reference"] == rule.id
    assert determination["expected_tax"] == {
        "value": response["expected_tax"],
        "currency": "USD",
    }
    assert determination["engine_amount"] == {
        "value": response["adjustment_amount"],
        "currency": "USD",
    }
    assert isinstance(response["expected_tax"], str)
    assert isinstance(response["adjustment_amount"], str)
    assert isinstance(determination["rate"], str)
    assert isinstance(determination["location_code"], str)
    assert set(request) == set(EngineLineRequest.__dataclass_fields__)
    assert set(response) == set(EngineLineResult.__dataclass_fields__)
    assert "rule_source_url" not in request

    path = rules_dir / "entries.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            rule.primary_source_url,
            "https://replaced.example/not-stored",
            1,
        ),
        encoding="utf-8",
    )
    reread = _one(_detail(signed_client, created["purchase_id"]))
    assert reread["rule_source_url"] == rule.primary_source_url
    assert reread["rate"] == rate.rate
    assert reread["location_code"] == rate.location_code
    reread_response = reread["engine_response"]
    assert isinstance(reread_response, dict)
    assert reread_response["rule_source_url"] == rule.primary_source_url


def test_case_4_january_nhr_has_null_rule_link(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case4", "trace-case4")
    detail = _detail(signed_client, created["purchase_id"])
    _assert_sources(signed_client, created["purchase_id"], detail)
    assert detail["queue"] == "needs_human_review"
    determination = _one(detail)
    assert determination["nhr_reason"] == "RULE_TRANSITION_UNRESOLVED"
    _no_tax_link(determination)
    response = determination["engine_response"]
    assert isinstance(response, dict)
    assert response["rule_source_url"] is None


def test_case_5_error_has_null_rule_link(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case5", "trace-case5")
    detail = _detail(signed_client, created["purchase_id"])
    _assert_sources(signed_client, created["purchase_id"], detail)
    determination = _one(detail)
    assert detail["queue"] == "error"
    assert determination["unit_status"] == "DETERMINATION_ERROR"
    assert determination["error_code"] == "INEXACT_CENTS"
    _no_tax_link(determination)
    response = determination["engine_response"]
    assert isinstance(response, dict)
    assert response["rule_source_url"] is None
    assert response["rate"] is None
    assert response["location_code"] is None


def test_case_6_held_shows_sources_and_no_tax_figure(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case6", "trace-case6")
    detail = _detail(signed_client, created["purchase_id"])
    _assert_sources(signed_client, created["purchase_id"], detail)
    assert detail["queue"] == "held"
    assert detail["determinations"] == []
    assert detail["purchase_amount_note"] == "purchase amount, not tax"
    assert "engine_amount" not in detail
    assert "expected_tax" not in detail
    assert "rate" not in detail
    assert "location_code" not in detail
    assert "rule_source_url" not in detail


def _detail(client: TestClient, purchase_id: object) -> dict[str, object]:
    response = client.get(f"/api/v1/purchases/{purchase_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _one(detail: dict[str, object]) -> dict[str, object]:
    rows = detail["determinations"]
    assert isinstance(rows, list) and len(rows) == 1
    row = rows[0]
    assert isinstance(row, dict)
    return row


def _line(detail: dict[str, object], line_id: object) -> dict[str, object]:
    rows = detail["lines"]
    assert isinstance(rows, list)
    found = [row for row in rows if isinstance(row, dict) and row["id"] == line_id]
    assert len(found) == 1
    return found[0]


def _no_tax_link(determination: dict[str, object]) -> None:
    assert determination["rule_source_url"] is None
    assert determination["rule_reference"] is None
    assert determination["engine_amount"] is None
    assert determination["expected_tax"] is None
    assert determination["rate"] is None
    assert determination["location_code"] is None


def _assert_sources(
    client: TestClient, purchase_id: object, detail: dict[str, object]
) -> None:
    facts = detail["facts"]
    assert isinstance(facts, list) and facts
    session = _open(client)
    try:
        documents = [
            {
                "id": document.id,
                "mime_type": document.mime_type,
                "sha256": document.sha256,
                "pages": document.pages,
            }
            for document in session.scalars(
                select(Document)
                .where(Document.purchase_id == purchase_id)
                .order_by(Document.id)
            ).all()
        ]
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in session.scalars(
            select(Evidence)
            .where(Evidence.fact_id.in_([fact["id"] for fact in facts]))
            .order_by(Evidence.id)
        ).all():
            grouped.setdefault(row.fact_id, []).append(
                {
                    "id": row.id,
                    "document_id": row.document_id,
                    "page": row.page,
                    "passage": row.passage,
                    "provenance": row.provenance,
                }
            )
    finally:
        session.close()
    assert documents
    assert detail["documents"] == documents
    assert all(set(document) == _DOCUMENT_KEYS for document in documents)
    document_ids = {document["id"] for document in documents}
    for fact in facts:
        assert isinstance(fact, dict)
        assert fact["model_output"] == "replay"
        assert fact["origin"] == "AI_EXTRACTED"
        copied = fact["evidence"]
        assert isinstance(copied, list) and copied == grouped[str(fact["id"])]
        assert all(set(item) == _EVIDENCE_KEYS for item in copied)
        assert all(item["document_id"] in document_ids for item in copied)
