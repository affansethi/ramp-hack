"""Startup mode guards (docs/architecture.md §1).

The app refuses to start unless DATA_MODE=synthetic and
ENGINE_MODE=mock_reviewed_ruleset; LLM_MODE is live, record or replay.
DEMO_MODE is "true" per Technical Spec v2. Only synthetic data exists,
in every environment.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.contracts.enums import DataMode, DemoMode, EngineMode, LLMMode


class ModeGuardError(RuntimeError):
    """Raised at startup when a mode variable is missing or disallowed."""


@dataclass(frozen=True)
class Settings:
    data_mode: DataMode
    engine_mode: EngineMode
    demo_mode: DemoMode
    llm_mode: LLMMode
    database_url: str  # SQLite is local scratch only; hosted state is Postgres.
    llm_replay: bool


def _parse_mode[M: StrEnum](
    environ: Mapping[str, str], name: str, enum: type[M], default: str | None
) -> M:
    raw = environ.get(name, default)
    if raw is None:
        raise ModeGuardError(f"{name} must be set; refusing to start")
    try:
        return enum(raw)
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        raise ModeGuardError(
            f"{name}={raw!r} not allowed (must be one of: {allowed}); refusing to start"
        ) from None


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load and validate startup modes. Raises ModeGuardError on any violation."""
    env = os.environ if environ is None else environ
    llm_mode = _parse_mode(env, "LLM_MODE", LLMMode, LLMMode.REPLAY.value)
    return Settings(
        data_mode=_parse_mode(env, "DATA_MODE", DataMode, None),
        engine_mode=_parse_mode(env, "ENGINE_MODE", EngineMode, None),
        demo_mode=_parse_mode(env, "DEMO_MODE", DemoMode, None),
        llm_mode=llm_mode,
        database_url=env.get("DATABASE_URL", "sqlite:///./taxleak-local.db"),
        llm_replay=llm_mode is not LLMMode.LIVE,
    )
