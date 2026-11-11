"""H5: a sourced line_amount correction feeds the engine and needs fresh approval."""

from fastapi.testclient import TestClient

from app.models import Purchase
from app.worker.poller import poll_once
from tests.api.test_flow import _headers, _run
from tests.api.test_flow import signed_client as signed_client  # noqa: F401

# Illustrative synthetic amount, not a golden: 1000.00 x the WA test rate is exact.
_CORRECTED = "1000.00"


def _approve(client: TestClient, determination: dict[str, object], key: str) -> None:
    response = client.post(
        f"/api/v1/determinations/{determination['id']}/review",
        json={
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed the engine result",
            "expected_version": determination["version"],
        },
        headers=_headers(key),
    )
    assert response.status_code == 200, response.text


def _correct(client: TestClient, purchase_id: str, line_id: str, version: int):
    return client.post(
        f"/api/v1/purchases/{purchase_id}/facts",
        json={
            "field": "line_amount",
            "line_id": line_id,
            "value": _CORRECTED,
            "source": "Corrected synthetic invoice, page 1",
            "reason": "Printed line amount was misread",
            "expected_version": version,
        },
        headers=_headers("h5-correct"),
    )


def _liability_totals(client: TestClient) -> list[dict[str, object]]:
    totals = client.get("/api/v1/totals").json()
    return totals["approved_liabilities"]["by_jurisdiction"]


def test_line_amount_correction_supersedes_and_feeds_the_engine(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    ctx = client.app.state.ctx
    created = _run(client, "case1", "h5-case1")
    purchase_id = str(created["purchase_id"])
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    first = detail["determinations"][0]
    _approve(client, first, "h5-approve-1")
    old_amount = first["engine_amount"]["value"]
    assert [row["total"]["value"] for row in _liability_totals(client)] == [old_amount]

    corrected = _correct(client, purchase_id, first["line_id"], detail["facts_version"])
    assert corrected.status_code == 200, corrected.text
    with ctx.session_factory() as session:
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        purchase.ramp_entity_amount = _CORRECTED  # Ramp's total agrees
        session.commit()
    # Superseded at once: nothing counts before the worker re-runs.
    assert _liability_totals(client) == []
    assert poll_once(ctx) == 1

    later = client.get(f"/api/v1/purchases/{purchase_id}").json()
    current = later["determinations"][0]
    assert current["id"] != first["id"]
    assert current["unit_status"] == "READY_FOR_REVIEW"
    assert current["engine_request"]["line_amount"] == _CORRECTED
    new_amount = current["engine_amount"]["value"]
    assert new_amount != old_amount
    # Unreviewed: still out of every total until a person approves it.
    assert _liability_totals(client) == []
    _approve(client, current, "h5-approve-2")
    assert [row["total"]["value"] for row in _liability_totals(client)] == [new_amount]


def test_correction_that_disagrees_with_ramp_holds(
    signed_client: TestClient,  # noqa: F811
) -> None:
    client = signed_client
    created = _run(client, "case1", "h5-mismatch")
    purchase_id = str(created["purchase_id"])
    detail = client.get(f"/api/v1/purchases/{purchase_id}").json()
    first = detail["determinations"][0]
    corrected = _correct(client, purchase_id, first["line_id"], detail["facts_version"])
    assert corrected.status_code == 200, corrected.text
    assert poll_once(client.app.state.ctx) == 1
    later = client.get(f"/api/v1/purchases/{purchase_id}").json()
    # Ramp still reports the original charge: the one-cent cross-check holds it.
    assert later["queue"] == "held"
    assert later["determinations"] == []
