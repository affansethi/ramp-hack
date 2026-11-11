"""RG-6/RG-7: sourced facts, official rule URLs, and durable single effects.

No goldens or network: money comparisons copy API values. Only the shared
signed_client fixture signs temporary rules; the URL gate reads repo rules.
"""

import hashlib
import hmac
import json
from urllib.parse import urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.domain.ruleset import load_ruleset, rule_is_active
from app.main import create_app
from app.models import (
    Determination,
    Document,
    Evidence,
    Fact,
    Job,
    LiabilityLine,
    Purchase,
    PurchaseLine,
    ReviewDecision,
    SourceEvent,
    Workpaper,
    WorkpaperLine,
)
from app.worker.poller import JobPoller, poll_once
from tests.gates.conftest import ROOT, _headers, _run


def _get(client: TestClient, path: str) -> dict:
    response = client.get(f"/api/v1/{path}")
    assert response.status_code == 200, response.text
    return response.json()


def _post(client: TestClient, path: str, body: dict, key: str) -> dict:
    response = client.post(f"/api/v1/{path}", json=body, headers=_headers(key))
    assert response.status_code == 200, response.text
    return response.json()


def _webhook(
    client: TestClient, event: str, kind: str, object_id: str, key: str
) -> dict:
    # Ramp intake carries IDs only; purchase creation belongs to the fetch job.
    raw = json.dumps({"id": event, "type": kind, "data": {"id": object_id}}).encode()
    signature = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    response = client.post(
        "/api/v1/webhooks/ramp",
        content=raw,
        headers={
            "Idempotency-Key": key,
            "X-Ramp-Signature": signature,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _fetch_and_advance(client: TestClient, intake: dict) -> str:
    ctx = client.app.state.ctx
    assert intake["purchase_id"] is None
    assert poll_once(ctx) == 1, "RG-7: IDs-only webhook fetch did not run"
    with ctx.session_factory() as session:
        job = session.get(Job, intake["job_id"])
        assert job is not None and job.status == "SUCCEEDED"
        purchase_id = job.purchase_id
        assert isinstance(purchase_id, str)
    assert poll_once(ctx) == 1, "RG-7: fetched purchase did not advance"
    assert poll_once(ctx) == 0
    return purchase_id


def _approve(client: TestClient, purchase_id: str) -> dict:
    determination = _get(client, f"purchases/{purchase_id}")["determinations"][0]
    assert determination["direction"] == "LIABILITY"
    assert determination["engine_amount"] is not None
    _post(
        client,
        f"determinations/{determination['id']}/review",
        {
            "action": "approve",
            "approval_path": "LIABILITY",
            "reason": "Reviewed synthetic evidence and engine result",
            "expected_version": determination["version"],
        },
        "approve",
    )
    return determination


@pytest.mark.parametrize("scenario", [f"case{i}" for i in range(1, 8)])
def test_rg6_every_current_demo_fact_has_evidence(
    signed_client: TestClient, scenario: str
) -> None:
    created = _run(signed_client, scenario, "evidence")
    purchase_id = created["purchase_id"]
    detail = _get(signed_client, f"purchases/{purchase_id}")
    with signed_client.app.state.ctx.session_factory() as session:
        facts = session.scalars(
            select(Fact).where(
                Fact.purchase_id == purchase_id, Fact.is_current.is_(True)
            )
        ).all()
        assert facts, f"RG-6: {scenario} has no current facts to check"
        assert {fact.id for fact in facts} == {fact["id"] for fact in detail["facts"]}
        for fact in facts:
            message = f"RG-6: {scenario} current fact {fact.field_key} ({fact.id})"
            evidence = session.scalars(
                select(Evidence).where(Evidence.fact_id == fact.id)
            ).all()
            assert evidence, f"{message} has no evidence row"
            for row in evidence:
                assert row.passage.strip() or row.provenance.strip(), (
                    f"{message} has empty passage and provenance"
                )
                if row.document_id is not None:
                    document = session.get(Document, row.document_id)
                    assert document is not None, f"{message} cites a missing document"
                    assert document.purchase_id == purchase_id, (
                        f"{message} cites another purchase's document"
                    )
                    assert 1 <= row.page <= document.pages, (
                        f"{message} cites a nonexistent page"
                    )
                else:
                    assert row.provenance.strip(), f"{message} has no named provenance"
            api_fact = next(item for item in detail["facts"] if item["id"] == fact.id)
            assert {item["id"] for item in api_fact["evidence"]} == {
                row.id for row in evidence
            }, f"{message} evidence is absent from the API"


def test_rg6_active_reviewed_rules_have_official_https_sources() -> None:
    # Read actual review metadata, never activate a rule to satisfy this gate.
    allowlist = yaml.safe_load(
        (ROOT / "rules/evidence/allowlist.yaml").read_text(encoding="utf-8")
    )["domains"]
    for rule in load_ruleset(ROOT / "rules").rules:
        if not rule_is_active(rule):
            continue
        url = rule.primary_source_url
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        assert (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and parsed.path not in ("", "/")
            and not any(character.isspace() for character in url)
            and any(
                host == domain or host.endswith("." + domain) for domain in allowlist
            )
        ), f"RG-6: active reviewed rule {rule.id} lacks an official HTTPS source: {url}"


def test_rg7_duplicate_webhook_event_id_has_one_purchase(
    signed_client: TestClient,
) -> None:
    client = signed_client
    first = _webhook(
        client, "duplicate-event", "transactions.cleared", "evt_case1", "a"
    )
    # A new request key must exercise event dedup, not the HTTP response cache.
    pending = _webhook(
        client, "duplicate-event", "transactions.cleared", "evt_case1", "b"
    )
    assert pending["duplicate"] is True
    assert pending["job_id"] == first["job_id"]
    purchase_id = _fetch_and_advance(client, first)
    again = _webhook(
        client, "duplicate-event", "transactions.cleared", "evt_case1", "c"
    )
    assert again["duplicate"] is True
    assert again["purchase_id"] == purchase_id
    assert poll_once(client.app.state.ctx) == 0
    with client.app.state.ctx.session_factory() as session:
        assert list(session.scalars(select(Purchase.id))) == [purchase_id], (
            "RG-7: duplicate webhook event id added a purchase"
        )
        assert len(session.scalars(select(SourceEvent)).all()) == 1
        assert len(session.scalars(select(Job)).all()) == 2  # fetch + advance
        assert len(session.scalars(select(PurchaseLine)).all()) == 1


def test_rg7_card_plus_matching_bill_is_one_purchase(
    signed_client: TestClient,
) -> None:
    client = signed_client
    ramp = client.app.state.ctx.ramp
    # Copy matching corroborating values; no tax expectations are authored here.
    ramp.bills["matching-bill"] = dict(ramp.transactions["evt_case1"])
    card = _run(client, "case1", "card")
    purchase_id = str(card["purchase_id"])
    _approve(client, purchase_id)
    before = _get(client, f"purchases/{purchase_id}")
    totals = _get(client, "totals")
    bill = _webhook(client, "bill-event", "bills.created", "matching-bill", "bill")
    merged_id = _fetch_and_advance(client, bill)
    message = (
        "RG-7 card-plus-bill gap: matching card and bill must merge into one purchase"
    )
    with client.app.state.ctx.session_factory() as session:
        assert list(session.scalars(select(Purchase.id))) == [purchase_id], message
        assert merged_id == purchase_id, message
        events = session.scalars(select(SourceEvent)).all()
        assert len(events) == 2 and {row.purchase_id for row in events} == {
            purchase_id
        }, message
        assert len(session.scalars(select(PurchaseLine)).all()) == 1, message
    after = _get(client, f"purchases/{purchase_id}")
    assert after["determinations"] == before["determinations"], message
    assert _get(client, "totals") == totals, message


def test_rg7_second_mark_ready_keeps_one_ready_fingerprint(
    signed_client: TestClient,
) -> None:
    client = signed_client
    created = _run(client, "case1", "ready-case")
    determination = _approve(client, str(created["purchase_id"]))
    draft = _post(
        client, "workpapers", {"jurisdiction": "WA", "period": "2026Q4"}, "draft"
    )
    body = {
        "expected_version": draft["version"],
        "fingerprint": draft["fingerprint"],
        "reason": "Freeze synthetic workpaper",
    }
    first = _post(client, f"workpapers/{draft['id']}/mark-ready", body, "ready-first")
    second = _post(client, f"workpapers/{draft['id']}/mark-ready", body, "ready-second")
    assert second == first, "RG-7: duplicate mark-ready changed the frozen version"
    assert first["approved_liabilities"][0]["amount"] == determination["engine_amount"]
    with client.app.state.ctx.session_factory() as session:
        ready = session.scalars(
            select(Workpaper).where(Workpaper.status == "READY_TO_FILE_SIMULATED")
        ).all()
        assert len(ready) == 1, "RG-7: duplicate mark-ready added a ready version"
        assert ready[0].fingerprint == draft["fingerprint"]
        assert ready[0].id == first["id"] and ready[0].version == first["version"]
        assert len(session.scalars(select(WorkpaperLine)).all()) == 1


@pytest.mark.parametrize("effect_exists", [False, True], ids=["unfinished", "replayed"])
def test_rg7_worker_restart_recovers_expired_lease_once(
    signed_client: TestClient, effect_exists: bool
) -> None:
    ctx = signed_client.app.state.ctx
    _post(signed_client, "demo/events", {"scenario": "case1"}, "restart-case")
    assert poll_once(ctx) == 1  # fetch creates purchase and queues advance
    if effect_exists:
        assert poll_once(ctx) == 1
    with ctx.session_factory() as session:
        job = session.scalars(select(Job).where(Job.kind == "advance")).one()
        job_id, purchase_id = job.id, job.purchase_id
        previous = list(session.scalars(select(Determination.id)))
        assert len(previous) == int(effect_exists)
        # Model loss of a worker with an unfinished durable lease. The replayed
        # variant also proves an existing effect survives re-execution unchanged.
        job.status, job.attempts = "RUNNING", 1
        job.lease_until = "2000-01-01T00:00:00Z"
        session.commit()
    # Fresh app context/session factory against the same disk DB, no background
    # thread or sleep: deterministic restart and claim of the expired lease.
    with TestClient(create_app(ctx.settings, start_worker=False)) as restarted:
        fresh = restarted.app.state.ctx
        assert fresh is not ctx and fresh.session_factory is not ctx.session_factory
        worker = JobPoller(fresh)
        assert worker.poll_once() == 1, "RG-7: restart lost the expired-lease job"
        assert worker.poll_once() == 0
        with fresh.session_factory() as session:
            recovered = session.get(Job, job_id)
            assert recovered is not None
            assert (recovered.status, recovered.attempts, recovered.lease_until) == (
                "SUCCEEDED",
                2,
                None,
            )
            rows = session.scalars(select(Determination)).all()
            assert len(rows) == 1, (
                "RG-7: expired-lease recovery duplicated a determination"
            )
            assert rows[0].purchase_id == purchase_id and rows[0].is_current
            if effect_exists:
                assert [rows[0].id] == previous
            assert not session.scalars(select(ReviewDecision)).all()
            assert not session.scalars(select(LiabilityLine)).all()


def test_case6_hold_has_no_determination_or_tax_zero(
    signed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_engine_call(*args: object, **kwargs: object) -> None:
        pytest.fail("Case 6 hold must never call the tax engine")

    monkeypatch.setattr("app.domain.pipeline.evaluate_line", forbidden_engine_call)
    created = _run(signed_client, "case6", "held-case")
    detail = _get(signed_client, f"purchases/{created['purchase_id']}")
    assert detail["queue"] == "held" and detail["holds"]
    assert detail["determinations"] == [], "Case 6 hold must have no tax result or zero"
    totals = _get(signed_client, "totals")
    assert totals["approved_liabilities"]["by_jurisdiction"] == []
    with signed_client.app.state.ctx.session_factory() as session:
        assert not session.scalars(select(Determination)).all(), (
            "Case 6 hold must not store a determination, including a tax zero"
        )
        assert not session.scalars(select(ReviewDecision)).all()
        assert not session.scalars(select(LiabilityLine)).all()


def test_case7_replay_preserves_approved_totals_and_rows(
    signed_client: TestClient,
) -> None:
    client = signed_client
    created = _run(client, "case1", "original")
    determination = _approve(client, str(created["purchase_id"]))
    before = _get(client, "totals")
    assert (
        before["approved_liabilities"]["by_jurisdiction"][0]["total"]
        == (determination["engine_amount"])
    )
    replay = _run(client, "case7", "replay-new-request-key")
    assert replay["duplicate"] is True
    assert replay["purchase_id"] == created["purchase_id"]
    assert _get(client, "totals") == before, "Case 7 replay changed totals"
    with client.app.state.ctx.session_factory() as session:
        for model in (
            Purchase,
            PurchaseLine,
            Determination,
            ReviewDecision,
            LiabilityLine,
        ):
            assert len(session.scalars(select(model)).all()) == 1, (
                f"Case 7 replay duplicated {model.__tablename__}"
            )
        assert len(session.scalars(select(SourceEvent)).all()) == 1
        assert len(session.scalars(select(Job)).all()) == 2
