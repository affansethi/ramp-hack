"""Fixtures for the release-gate tests.

Self-contained: the gates must stay green even if `tests/api`
refactors its private helpers, so this module defines its own
client, CSRF reset, header, and scenario-run helpers instead of
importing them from `tests.api.test_flow`.
"""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.guards import CSRF_HEADER
from app.api.routes import COOKIE
from app.config import load_settings
from app.main import create_app
from app.models import Job
from app.worker.poller import poll_once

ROOT = Path(__file__).resolve().parents[2]
GOOD_ENV = {
    "DATA_MODE": "synthetic",
    "ENGINE_MODE": "mock_reviewed_ruleset",
    "DEMO_MODE": "true",
    "LLM_MODE": "replay",
}


_csrf_token: str | None = None


def reset_csrf() -> None:
    global _csrf_token
    _csrf_token = None


@pytest.fixture(autouse=True)
def _reset_csrf_token() -> Iterator[None]:
    reset_csrf()
    yield
    reset_csrf()


def _headers(key: str, *, csrf: bool = True) -> dict[str, str]:
    headers = {"Idempotency-Key": key, "X-Requested-With": "taxleak"}
    if csrf and _csrf_token:
        headers[CSRF_HEADER] = _csrf_token
    return headers


def login(client: TestClient, key: str = "login") -> str:
    """Sign in, keep the HttpOnly cookie, and remember the CSRF token."""
    global _csrf_token
    _csrf_token = None
    response = client.post("/api/v1/demo/login", json={}, headers=_headers(key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert "session_id" not in body
    token = body["csrf_token"]
    assert isinstance(token, str) and token
    cookie = client.cookies.get(COOKIE)
    assert isinstance(cookie, str) and cookie != token
    assert cookie not in response.text
    _csrf_token = token
    return token


def _activate(dest: Path) -> None:
    dest.mkdir()
    for name in ("entries.yaml", "rates.yaml", "catalog.yaml"):
        text = (ROOT / "rules" / name).read_text(encoding="utf-8")
        text = text.replace("review_status: candidate", "review_status: reviewed")
        text = text.replace("status: candidate", "status: signed")
        text = text.replace("reviewed_by:", "reviewed_by: AF")
        text = text.replace("signed_off_by:", "signed_off_by: AF")
        (dest / name).write_text(text, encoding="utf-8")


@pytest.fixture
def signed_client(tmp_path: Path, _reset_csrf_token: None) -> Iterator[TestClient]:
    rules = tmp_path / "rules"
    _activate(rules)
    database = tmp_path / "app.db"
    settings = replace(
        load_settings(GOOD_ENV),
        database_url="sqlite:///" + database.resolve().as_posix(),
        rules_dir=str(rules),
        ramp_webhook_secret="test-secret",
    )
    app = create_app(settings, start_worker=False)
    with TestClient(app) as client:
        login(client)
        yield client


def _run(client: TestClient, scenario: str, key: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/demo/events",
        json={"scenario": scenario},
        headers=_headers(key),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    ctx = client.app.state.ctx
    assert poll_once(ctx) == 1 or body["duplicate"] is True
    if body.get("purchase_id") is None:
        # IDs-only intake (#25): the fetch job creates the purchase, then the
        # advance job runs the pipeline.
        with ctx.session_factory() as session:
            job = session.get(Job, body["job_id"])
            assert job is not None
            body["purchase_id"] = job.purchase_id
        assert poll_once(ctx) == 1
    return body
