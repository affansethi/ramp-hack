"""Startup mode guards (architecture §1): the app refuses to start unless
DATA_MODE=synthetic and ENGINE_MODE=mock_reviewed_ruleset; LLM_MODE is
live, record or replay."""

import pytest

from app.config import ModeGuardError, load_settings
from app.contracts import DataMode, EngineMode, LLMMode
from app.main import create_app

GOOD_ENV = {
    "DATA_MODE": "synthetic",
    "ENGINE_MODE": "mock_reviewed_ruleset",
    "DEMO_MODE": "true",
}


def test_load_settings_accepts_synthetic_modes() -> None:
    settings = load_settings(GOOD_ENV)
    assert settings.data_mode is DataMode.SYNTHETIC
    assert settings.engine_mode is EngineMode.MOCK_REVIEWED_RULESET
    assert settings.llm_mode is LLMMode.REPLAY  # default for local runs and CI
    assert settings.llm_replay is True


def test_refuses_unset_data_mode() -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != "DATA_MODE"}
    with pytest.raises(ModeGuardError, match="DATA_MODE"):
        load_settings(env)


def test_refuses_non_synthetic_data_mode() -> None:
    with pytest.raises(ModeGuardError, match="DATA_MODE"):
        load_settings({**GOOD_ENV, "DATA_MODE": "production"})


def test_refuses_non_mock_engine_mode() -> None:
    with pytest.raises(ModeGuardError, match="ENGINE_MODE"):
        load_settings({**GOOD_ENV, "ENGINE_MODE": "live_ruleset"})


def test_refuses_unset_engine_mode() -> None:
    env = {k: v for k, v in GOOD_ENV.items() if k != "ENGINE_MODE"}
    with pytest.raises(ModeGuardError, match="ENGINE_MODE"):
        load_settings(env)


@pytest.mark.parametrize("mode", ["Synthetic", "SYNTHETIC", " synthetic", "synthetic "])
def test_refuses_data_mode_case_and_spacing_variants(mode: str) -> None:
    with pytest.raises(ModeGuardError, match="DATA_MODE"):
        load_settings({**GOOD_ENV, "DATA_MODE": mode})


@pytest.mark.parametrize(
    "mode",
    [
        "MOCK_REVIEWED_RULESET",
        "Mock_Reviewed_Ruleset",
        " mock_reviewed_ruleset",
        "mock_reviewed_ruleset ",
    ],
)
def test_refuses_engine_mode_case_and_spacing_variants(mode: str) -> None:
    with pytest.raises(ModeGuardError, match="ENGINE_MODE"):
        load_settings({**GOOD_ENV, "ENGINE_MODE": mode})


def test_create_app_refuses_production_data_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_MODE", "synthetic")
    monkeypatch.setenv("ENGINE_MODE", "mock_reviewed_ruleset")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DATA_MODE", "production")
    with pytest.raises(ModeGuardError, match="DATA_MODE"):
        create_app()


def test_create_app_refuses_live_engine_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_MODE", "synthetic")
    monkeypatch.setenv("ENGINE_MODE", "mock_reviewed_ruleset")
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("ENGINE_MODE", "live_ruleset")
    with pytest.raises(ModeGuardError, match="ENGINE_MODE"):
        create_app()


@pytest.mark.parametrize("mode", ["live", "record", "replay"])
def test_llm_mode_accepts_the_three_allowed_values(mode: str) -> None:
    settings = load_settings({**GOOD_ENV, "LLM_MODE": mode})
    assert settings.llm_mode is LLMMode(mode)


def test_refuses_unknown_llm_mode() -> None:
    with pytest.raises(ModeGuardError, match="LLM_MODE"):
        load_settings({**GOOD_ENV, "LLM_MODE": "cached"})
