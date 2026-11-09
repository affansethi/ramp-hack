"""Runtime handles for one process: settings, sessions, rules directory."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db import init_database

_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class AppContext:
    settings: Settings
    session_factory: sessionmaker[Session]
    rules_dir: Path


def build_context(settings: Settings) -> AppContext:
    rules_dir = Path(settings.rules_dir)
    if not rules_dir.is_absolute():
        rules_dir = _ROOT / rules_dir
    return AppContext(
        settings=settings,
        session_factory=init_database(settings.database_url),
        rules_dir=rules_dir,
    )
