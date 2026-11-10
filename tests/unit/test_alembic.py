"""Alembic baseline creates the same tables as Base.metadata on empty SQLite."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate.api import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app import db, models  # noqa: F401
from app.db import Base


def test_alembic_upgrade_matches_metadata(tmp_path: Path) -> None:
    database = tmp_path / "alembic.db"
    url = "sqlite:///" + database.resolve().as_posix()
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, Base.metadata)
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "alembic_version" in names
    assert set(Base.metadata.tables) <= names
    assert diff == []


def test_postgres_url_is_the_path_that_runs_alembic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(db, "_alembic_upgrade", calls.append)
    monkeypatch.setattr(db.Base.metadata, "create_all", _refuse)
    monkeypatch.setattr("app.seed.seed", lambda session: None)
    monkeypatch.setattr(db, "session_scope", _no_session)
    url = "postgresql://app:p%40ss@db.example:5432/taxleak"
    db.init_database(url)
    assert calls == [url]


def test_sqlite_url_never_runs_alembic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_alembic_upgrade", _refuse)
    db.init_database("sqlite:///" + (tmp_path / "local.db").resolve().as_posix())


def test_postgres_engine_uses_an_installed_driver() -> None:
    engine = db.make_engine("postgresql://app:p%40ss@db.example:5432/taxleak")
    try:
        assert engine.dialect.driver == "psycopg"
        assert engine.url.password == "p@ss"
    finally:
        engine.dispose()


def test_alembic_config_keeps_percent_encoded_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_upgrade(config: Config, revision: str) -> None:
        seen.append(config.get_main_option("sqlalchemy.url") or "")

    monkeypatch.setattr(command, "upgrade", fake_upgrade)
    db._alembic_upgrade("postgresql://app:p%40ss@db.example:5432/taxleak")
    assert seen == ["postgresql+psycopg://app:p%40ss@db.example:5432/taxleak"]


def _refuse(*args: object, **kwargs: object) -> None:
    raise AssertionError("unexpected schema path")


@contextmanager
def _no_session(factory: object) -> Iterator[None]:
    yield None
