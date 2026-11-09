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
from pathlib import Path

from app.contracts.enums import DataMode, DemoMode, EngineMode, LLMMode

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DATABASE_URL = "sqlite:///" + (_ROOT / "taxleak-local.db").as_posix()


class ModeGuardError(RuntimeError):
    """Raised at startup when a mode variable is missing or disallowed."""


@dataclass(frozen=True)
class Settings:
    data_mode: DataMode
    engine_mode: EngineMode
    demo_mode: DemoMode
    llm_mode: LLMMode
    database_url: str  # SQLite is local scratch only; hosted state is Postgres.
    rules_dir: str
    ramp_webhook_secret: str
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


def _load_dotenv() -> None:
    """Fill unset variables from a gitignored `.env`. Never overrides the environment."""
    path = _ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load and validate startup modes. Raises ModeGuardError on any violation."""
    if environ is None:
        _load_dotenv()
        env: Mapping[str, str] = os.environ
    else:
        env = environ
    llm_mode = _parse_mode(env, "LLM_MODE", LLMMode, LLMMode.REPLAY.value)
    return Settings(
        data_mode=_parse_mode(env, "DATA_MODE", DataMode, None),
        engine_mode=_parse_mode(env, "ENGINE_MODE", EngineMode, None),
        demo_mode=_parse_mode(env, "DEMO_MODE", DemoMode, None),
        llm_mode=llm_mode,
        database_url=env.get("DATABASE_URL", _DEFAULT_DATABASE_URL),
        rules_dir=env.get("RULES_DIR", "rules"),
        ramp_webhook_secret=env.get("RAMP_WEBHOOK_SECRET", ""),
        llm_replay=llm_mode is not LLMMode.LIVE,
    )
