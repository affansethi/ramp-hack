"""Filter and paginate the purchase list (issue #21).

Sends X-Requested-With on GET as well as POST so the suite passes before
and after the demo-header fix. Covers Technical Spec v2 section 7
(GET /purchases page of at most 100) and section 5 (unit status strings),
Design Spec section 4.2 (shown vs blocked) and hard rules H1-H5.
"""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.clock import utcnow
from app.config import load_settings
from app.main import create_app
from app.models import Determination, Purchase
from app.seed import ORG_ID
from app.worker.poller import poll_once
from tests.api.test_flow import GOOD_ENV, _activate, _headers, _run

SUMMARY_FIELDS = {
    "id",
    "vendor",
    "invoice_number",
    "invoice_date",
    "item_status",
    "queue",
    "scenario",
    "extraction_mode",
    "facts_version",
    "stop_reason",
}

TAX_KEYS = (
    "engine_amount",
    "expected_tax",
    "tax_amount",
    "net",
    "savings",
    "recovered",
)


def _client(tmp_path: Path) -> TestClient:
    rules = tmp_path / "rules"
    _activate(rules)
    database = tmp_path / "list.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(rules),
        ramp_webhook_secret="test-secret",
    )
    return TestClient(create_app(settings, start_worker=False))


def _login(client: TestClient) -> None:
    response = client.post("/api/v1/demo/login", json={}, headers=_headers("login"))
    assert response.status_code == 200, response.text


def _listing(client: TestClient, query: str = "") -> dict:
    response = client.get(
        f"/api/v1/purchases{query}",
        headers={"X-Requested-With": "taxleak"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _listing_raw(client: TestClient, query: str = ""):
    return client.get(
        f"/api/v1/purchases{query}",
        headers={"X-Requested-With": "taxleak"},
    )


def _seed(client: TestClient) -> dict[str, str]:
    """Ingest one held, one NHR, one ready, one refund-direction purchase."""
    _run(client, "case6", "seed-case6")
    _run(client, "case4", "seed-case4")
    _run(client, "case1", "seed-case1")
    _run(client, "case3", "seed-case3")
    body = _listing(client)
    by_scenario = {row["scenario"]: row["id"] for row in body["purchases"]}
    assert set(by_scenario) == {"case6", "case4", "case1", "case3"}, by_scenario
    return by_scenario


def _insert_purchase(
    client: TestClient,
    *,
    invoice_date: str = "2026-11-15",
    created_at: str | None = None,
    vendor: str = "Synthetic Vendor",
    invoice_number: str | None = None,
) -> str:
    session = client.app.state.ctx.session_factory()
    try:
        purchase_id = str(uuid4())
        session.add(
            Purchase(
                id=purchase_id,
                org_id=ORG_ID,
                vendor=vendor,
                invoice_number=invoice_number or f"SYN-{purchase_id[:8]}",
                invoice_date=invoice_date,
                entity_name="Acme Startup LLC",
                currency="USD",
                item_status="DETERMINED",
                extraction_mode="replay",
                facts_version=1,
                created_at=created_at or utcnow(),
            )
        )
        session.commit()
        return purchase_id
    finally:
        session.close()


def _set_determination(
    client: TestClient,
    purchase_id: str,
    *,
    label: str | None,
    direction: str | None,
    unit_status: str,
) -> str:
    session = client.app.state.ctx.session_factory()
    try:
        purchase = session.get(Purchase, purchase_id)
        assert purchase is not None
        determination_id = str(uuid4())
        session.add(
            Determination(
                id=determination_id,
                org_id=purchase.org_id,
                purchase_id=purchase_id,
                jurisdiction="WA",
                facts_version=1,
                ruleset_version="test",
                label=label,
                direction=direction,
                derived_direction=False,
                unit_status=unit_status,
                version=1,
                is_current=True,
            )
        )
        session.commit()
        return determination_id
    finally:
        session.close()


def test_summary_fields_stay(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _run(client, "case1", "fields-case1")
        body = _listing(client)
        assert len(body["purchases"]) == 1
        row = body["purchases"][0]
        assert set(row) >= SUMMARY_FIELDS
        for key in TAX_KEYS:
            assert key not in row, key


def test_page_size_capped_at_100(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _run(client, "case1", "cap-case1")
        for index in range(120):
            _insert_purchase(
                client,
                created_at=f"2026-11-{(index % 28) + 1:02d}T00:00:{index % 60:02d}Z",
                invoice_number=f"SYN-CAP-{index:03d}",
            )
        body = _listing(client, "?limit=500")
        assert len(body["purchases"]) <= 100
        assert len(body["purchases"]) == 100


def test_cursor_pages_do_not_repeat_ids(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _run(client, "case1", "cursor-case1")
        _run(client, "case2", "cursor-case2")
        _run(client, "case3", "cursor-case3")
        first = _listing(client, "?limit=2")
        assert len(first["purchases"]) == 2
        assert first.get("next_cursor")
        second = _listing(client, f"?limit=2&cursor={first['next_cursor']}")
        first_ids = [row["id"] for row in first["purchases"]]
        second_ids = [row["id"] for row in second["purchases"]]
        assert not set(first_ids) & set(second_ids)
        assert len(set(first_ids + second_ids)) == len(first_ids) + len(second_ids)


def test_order_is_created_at_then_id(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        late = _insert_purchase(client, created_at="2026-11-20T00:00:00Z")
        early = _insert_purchase(client, created_at="2026-11-10T00:00:00Z")
        body = _listing(client)
        ids = [row["id"] for row in body["purchases"]]
        assert ids.index(early) < ids.index(late)


def test_empty_filter_page_is_200(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _run(client, "case6", "empty-case6")
        response = _listing_raw(client, "?period=2025Q1")
        assert response.status_code == 200
        assert response.json()["purchases"] == []


def test_period_uses_invoice_date_not_service_period(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        by_scenario = _seed(client)
        in_q4 = _listing(client, "?period=2026Q4")
        ids = {row["id"] for row in in_q4["purchases"]}
        # case4 has a January 2027 service period but an invoice date in
        # December 2026, so the invoice-date quarter still matches 2026Q4.
        assert by_scenario["case4"] in ids
        assert by_scenario["case1"] in ids
        out = _listing(client, "?period=2027Q1")
        assert {row["id"] for row in out["purchases"]} == set()


def test_label_filter_uses_current_determination(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        by_scenario = _seed(client)
        supported = _listing(client, "?label=ENGINE_SUPPORTED")
        ids = {row["id"] for row in supported["purchases"]}
        assert by_scenario["case1"] in ids
        assert by_scenario["case4"] not in ids
        assert by_scenario["case6"] not in ids
        nhr = _listing(client, "?label=NEEDS_HUMAN_REVIEW")
        assert {row["id"] for row in nhr["purchases"]} == {by_scenario["case4"]}
        bad = _listing_raw(client, "?label=SUPPORTED")
        assert bad.status_code == 422


def test_direction_filter_excludes_hold_nhr_error(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        by_scenario = _seed(client)
        liabilities = _listing(client, "?direction=LIABILITY")
        ids = {row["id"] for row in liabilities["purchases"]}
        assert by_scenario["case1"] in ids
        assert by_scenario["case6"] not in ids
        assert by_scenario["case4"] not in ids
        refunds = _listing(client, "?direction=REFUND_CANDIDATE")
        assert {row["id"] for row in refunds["purchases"]} == {by_scenario["case3"]}
        for row in liabilities["purchases"] + refunds["purchases"]:
            assert "engine_amount" not in row
            assert "expected_tax" not in row
        bad = _listing_raw(client, "?direction=REFUND")
        assert bad.status_code == 422


def test_hold_filter_returns_only_active_holds(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        by_scenario = _seed(client)
        held = _listing(client, "?hold=true")
        assert {row["id"] for row in held["purchases"]} == {by_scenario["case6"]}
        for row in held["purchases"]:
            assert "engine_amount" not in row
            assert "expected_tax" not in row
            assert "net" not in row
        open_ = _listing(client, "?hold=false")
        ids = {row["id"] for row in open_["purchases"]}
        assert by_scenario["case6"] not in ids
        assert by_scenario["case1"] in ids
        bad = _listing_raw(client, "?hold=maybe")
        assert bad.status_code == 422


def test_review_status_filter_uses_stored_unit_status(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        by_scenario = _seed(client)
        ready = _listing(client, "?review_status=READY_FOR_REVIEW")
        ready_ids = {row["id"] for row in ready["purchases"]}
        assert by_scenario["case1"] in ready_ids
        assert by_scenario["case6"] not in ready_ids
        nhr = _listing(client, "?review_status=NHR_ENGINE")
        assert {row["id"] for row in nhr["purchases"]} == {by_scenario["case4"]}
        bad = _listing_raw(client, "?review_status=PENDING")
        assert bad.status_code == 422


def test_review_status_covers_all_stored_values(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _run(client, "case1", "status-seed")
        statuses = [
            "READY_FOR_REVIEW",
            "APPROVED",
            "APPROVED_FOR_PURSUIT",
            "REJECTED",
            "DETERMINATION_ERROR",
            "NHR_ENGINE",
            "SENT_TO_ADVISOR",
        ]
        wanted: dict[str, str] = {}
        for index, status in enumerate(statuses):
            purchase_id = _insert_purchase(
                client,
                invoice_number=f"SYN-ST-{index:02d}",
            )
            _set_determination(
                client,
                purchase_id,
                label="ENGINE_SUPPORTED"
                if status not in ("NHR_ENGINE", "DETERMINATION_ERROR")
                else ("NEEDS_HUMAN_REVIEW" if status == "NHR_ENGINE" else None),
                direction="LIABILITY"
                if status not in ("NHR_ENGINE", "DETERMINATION_ERROR")
                else None,
                unit_status=status,
            )
            wanted[status] = purchase_id
        for status, purchase_id in wanted.items():
            body = _listing(client, f"?review_status={status}")
            assert purchase_id in {row["id"] for row in body["purchases"]}, status
        bad = _listing_raw(client, "?review_status=APPROVED_LATE")
        assert bad.status_code == 422


def test_review_status_superseded_uses_history(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        created = _run(client, "case1", "supersede-seed")
        purchase_id = str(created["purchase_id"])
        detail = client.get(
            f"/api/v1/purchases/{purchase_id}",
            headers={"X-Requested-With": "taxleak"},
        ).json()
        response = client.post(
            f"/api/v1/purchases/{purchase_id}/facts",
            json={
                "field": "user_location",
                "value": "Seattle, WA",
                "source": "seat export",
                "reason": "correct location",
                "expected_version": detail["facts_version"],
            },
            headers=_headers("supersede-fact"),
        )
        assert response.status_code == 200, response.text
        poll_once(client.app.state.ctx)
        body = _listing(client, "?review_status=SUPERSEDED")
        assert purchase_id in {row["id"] for row in body["purchases"]}


def test_cursor_survives_filter_change_after_approve(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _run(client, "case1", "cursor-approve-1")
        _run(client, "case2", "cursor-approve-2")
        _run(client, "case3", "cursor-approve-3")
        first = _listing(client, "?review_status=READY_FOR_REVIEW&limit=2")
        assert len(first["purchases"]) == 2
        assert first.get("next_cursor")
        last_id = first["purchases"][-1]["id"]
        detail = client.get(
            f"/api/v1/purchases/{last_id}",
            headers={"X-Requested-With": "taxleak"},
        ).json()
        assert detail["determinations"]
        determination = detail["determinations"][0]
        approval_path = "LIABILITY"
        session = client.app.state.ctx.session_factory()
        try:
            stored = session.get(Determination, determination["id"])
            assert stored is not None
            if stored.direction == "REFUND_CANDIDATE":
                approval_path = "REFUND_PURSUIT"
            elif stored.direction == "CHARGED_CORRECTLY":
                approval_path = "CHARGED_CORRECTLY"
        finally:
            session.close()
        approved = client.post(
            f"/api/v1/determinations/{determination['id']}/review",
            json={
                "action": "approve",
                "approval_path": approval_path,
                "reason": "reviewed",
                "expected_version": determination["version"],
            },
            headers=_headers("cursor-approve"),
        )
        assert approved.status_code == 200, approved.text
        second = _listing_raw(
            client,
            f"?review_status=READY_FOR_REVIEW&limit=2&cursor={first['next_cursor']}",
        )
        assert second.status_code == 200, second.text
        first_ids = {row["id"] for row in first["purchases"]}
        second_ids = {row["id"] for row in second.json()["purchases"]}
        assert last_id not in second_ids
        assert not first_ids & second_ids


def test_malformed_period_values_are_422(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _run(client, "case1", "malformed-period-seed")
        for query in (
            "?period=2026Q4%0A",
            "?period=2026Q4 ",
            "?period=2026Q45",
            "?period=26Q4",
            "?period=2026Q0",
            "?period=2026Q5",
        ):
            response = _listing_raw(client, query)
            assert response.status_code == 422, query


def test_invalid_numeric_cursor_is_422(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _run(client, "case1", "bad-cursor-seed")
        for query in ("?cursor=%C2%B2", "?cursor=999999999"):
            response = _listing_raw(client, query)
            assert response.status_code == 422, query


def test_unknown_values_are_422(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _login(client)
        _run(client, "case1", "unknown-seed")
        for query in (
            "?period=not-a-quarter",
            "?label=BOGUS",
            "?direction=BOGUS",
            "?hold=sometimes",
            "?review_status=BOGUS",
            "?limit=bogus",
        ):
            response = _listing_raw(client, query)
            assert response.status_code == 422, query
