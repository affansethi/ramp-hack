"""Task 3: on App Service the app refuses to start without managed Postgres."""

import logging

import pytest

from app.config import ModeGuardError, load_settings

_MODES = {
    "DATA_MODE": "synthetic",
    "ENGINE_MODE": "mock_reviewed_ruleset",
    "DEMO_MODE": "true",
    "LLM_MODE": "replay",
}
_HOSTED = {**_MODES, "WEBSITE_SITE_NAME": "ramphack-affan"}


@pytest.mark.parametrize("url", [None, "", "sqlite:///./taxleak.db", "sqlite://"])
def test_app_service_refuses_missing_or_sqlite_database(
    url: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    env = dict(_HOSTED)
    if url is not None:
        env["DATABASE_URL"] = url
    with (
        caplog.at_level(logging.ERROR, logger="taxleak.startup"),
        pytest.raises(ModeGuardError, match="PostgreSQL"),
    ):
        load_settings(env)
    assert len(caplog.records) == 1


def test_app_service_accepts_postgres() -> None:
    url = "postgresql://app@db.example:5432/taxleak?sslmode=require"
    assert load_settings({**_HOSTED, "DATABASE_URL": url}).database_url == url


def test_local_runs_keep_the_sqlite_default() -> None:
    assert load_settings(dict(_MODES)).database_url.startswith("sqlite")
