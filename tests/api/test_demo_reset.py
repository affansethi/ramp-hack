"""Demo reset and local backup commands (#48).

The reset script drives the documented API contract: POST /api/v1/demo/reset
wipes demo rows while the seed org/person/setup survive, and a repeated key
replays the stored response. The Makefile targets wire that script and the
SQLite replay backup used when Azure is down.
"""

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import IdempotencyKey, Organization, Person, Purchase, SourceEvent
from app.seed import ORG_ID, PERSON_ID
from tests.api.test_flow import GOOD_ENV, _headers, _run, login, signed_client  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def _count(client: TestClient, model: object) -> int:
    session = client.app.state.ctx.session_factory()
    try:
        found = session.scalar(select(func.count()).select_from(model))
        return int(found or 0)
    finally:
        session.close()


def test_reset_wipes_demo_data_and_keeps_seed(
    signed_client: TestClient,  # noqa: F811
) -> None:
    created = _run(signed_client, "case1", "seed-case1")
    first_id = created["purchase_id"]
    listing = signed_client.get("/api/v1/purchases").json()
    assert len(listing["purchases"]) == 1
    assert _count(signed_client, SourceEvent) == 1

    response = signed_client.post("/api/v1/demo/reset", headers=_headers("reset"))
    assert response.status_code == 200, response.text
    assert response.json()["reset"] is True

    assert _count(signed_client, Purchase) == 0
    assert _count(signed_client, SourceEvent) == 0
    listing = signed_client.get("/api/v1/purchases").json()
    assert listing["purchases"] == []

    # The seed org and reviewer survive the wipe, so the run starts clean.
    session = signed_client.app.state.ctx.session_factory()
    try:
        assert session.get(Organization, ORG_ID) is not None
        assert session.get(Person, PERSON_ID) is not None
    finally:
        session.close()

    scenarios = signed_client.get("/api/v1/demo/scenarios")
    assert scenarios.status_code == 200, scenarios.text
    assert set(scenarios.json()["scenarios"]) >= {"case1", "case7"}
    totals = signed_client.get("/api/v1/totals").json()
    assert totals["held"]["count"] == 0

    # The reset consumed its own key; a fresh key works after the wipe.
    created = _run(signed_client, "case1", "after-reset")
    assert created["purchase_id"] != first_id
    assert created["duplicate"] is False


def test_reset_replays_under_the_same_key(
    signed_client: TestClient,  # noqa: F811
) -> None:
    first = signed_client.post("/api/v1/demo/reset", headers=_headers("reset-once"))
    assert first.status_code == 200, first.text
    second = signed_client.post("/api/v1/demo/reset", headers=_headers("reset-once"))
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert _count(signed_client, IdempotencyKey) >= 1


def test_demo_reset_script_reports_its_usage() -> None:
    script = ROOT / "scripts" / "demo_reset.py"
    assert script.exists(), "scripts/demo_reset.py must exist"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


def test_makefile_wires_demo_reset_and_demo_local() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "demo-reset:" in text
    assert "scripts/demo_reset.py" in text
    assert "demo-local:" in text
    local = text.split("demo-local:")[1].split("\n\n")[0]
    assert "DATA_MODE=synthetic" in local
    assert "ENGINE_MODE=mock_reviewed_ruleset" in local
    assert "DEMO_MODE=true" in local
    assert "LLM_MODE=replay" in local
    assert "sqlite" in local.lower()
