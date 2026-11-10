"""Alembic baseline creates the same tables as Base.metadata on empty SQLite."""

from pathlib import Path

from alembic import command
from alembic.autogenerate.api import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app import models  # noqa: F401
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
    model_tables = set(Base.metadata.tables)
    assert model_tables <= names
    assert not any("float" in str(change).lower() for change in diff)
