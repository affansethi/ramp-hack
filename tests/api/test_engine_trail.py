"""Engine request and response are stored per determination and served read-only."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Determination
from app.worker.poller import poll_once
from tests.api.test_flow import _headers, _open, _run, signed_client  # noqa: F401


def _detail(client: TestClient, purchase_id: object) -> dict[str, object]:
    return client.get(f"/api/v1/purchases/{purchase_id}").json()


def test_supported_case_returns_request_and_response(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "trail1")
    row = _detail(signed_client, created["purchase_id"])["determinations"][0]
    request, response = row["engine_request"], row["engine_response"]
    assert request["jurisdiction"] == row["jurisdiction"]
    assert isinstance(request["line_amount"], str)
    assert isinstance(request["tax_charged"], str)
    assert response["adjustment_amount"] == row["engine_amount"]["value"]
    assert response["expected_tax"] == row["expected_tax"]["value"]


def test_nhr_engine_row_has_null_response_amounts(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case4", "trail4")
    row = _detail(signed_client, created["purchase_id"])["determinations"][0]
    assert row["engine_request"] is not None
    response = row["engine_response"]
    for key in (
        "expected_tax",
        "adjustment_amount",
        "rate",
        "direction",
        "rule_reference",
    ):
        assert response[key] is None


def test_held_purchase_has_no_determination_or_trail(
    signed_client: TestClient,  # noqa: F811
) -> None:
    _run(signed_client, "case1", "seed")
    created = _run(signed_client, "case6", "trail6")
    detail = _detail(signed_client, created["purchase_id"])
    assert detail["determinations"] == []
    session = _open(signed_client)
    try:
        rows = session.scalars(
            select(Determination).where(
                Determination.purchase_id == created["purchase_id"]
            )
        ).all()
        assert all(r.engine_request is None and r.engine_response is None for r in rows)
    finally:
        session.close()


def test_stored_json_is_sorted_and_trail_fields_are_refused(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "trail-sorted")
    session = _open(signed_client)
    try:
        row = session.scalars(select(Determination)).first()
        assert row is not None and row.engine_request is not None
        assert row.engine_request == json.dumps(
            json.loads(row.engine_request), sort_keys=True
        )
    finally:
        session.close()
    detail = _detail(signed_client, created["purchase_id"])
    for field in ("engine_request", "engine_response"):
        refused = signed_client.post(
            f"/api/v1/purchases/{created['purchase_id']}/facts",
            json={
                "field": field,
                "value": "{}",
                "source": "no",
                "reason": "no",
                "expected_version": detail["facts_version"],
            },
            headers=_headers(f"forbid-{field}"),
        )
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "FORBIDDEN_FIELD"


def test_facts_edit_keeps_the_superseded_trail(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "trail-v")
    purchase_id = created["purchase_id"]
    detail = _detail(signed_client, purchase_id)
    edited = signed_client.post(
        f"/api/v1/purchases/{purchase_id}/facts",
        json={
            "field": "user_location",
            "value": "WA",
            "source": "invoice",
            "reason": "Confirmed the seat location",
            "expected_version": detail["facts_version"],
        },
        headers=_headers("edit-v"),
    )
    assert edited.status_code == 200, edited.text
    assert poll_once(signed_client.app.state.ctx) == 1
    session = _open(signed_client)
    try:
        rows = session.scalars(
            select(Determination)
            .where(Determination.purchase_id == purchase_id)
            .order_by(Determination.facts_version)
        ).all()
        assert [r.facts_version for r in rows] == [1, 2]
        first, second = (json.loads(r.engine_request or "") for r in rows)
        assert rows[0].engine_request is not None
        assert rows[1].engine_request is not None
        assert first["tax_charged"] == second["tax_charged"]
        assert first["line_amount"] == second["line_amount"]
        assert rows[0].is_current is False and rows[1].is_current is True
    finally:
        session.close()
