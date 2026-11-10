"""SQLite locally, Postgres when DATABASE_URL says so. Money stays text."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def driver_url(url: str) -> str:
    """Pin the psycopg (v3) driver for a bare ``postgresql://`` URL."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str) -> Engine:
    url = driver_url(url)
    connect_args = (
        {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    )
    return create_engine(url, connect_args=connect_args)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_database(url: str) -> sessionmaker[Session]:
    from app import models  # noqa: F401  (register tables)
    from app.seed import seed

    engine = make_engine(url)
    if url.startswith(("postgresql", "postgres://")):
        # Hosted Postgres: Alembic is the only schema path.
        _alembic_upgrade(url)
    else:
        # Local SQLite scratch: create_all, then the legacy local upgrades so an
        # older scratch database keeps working.
        from app.migrations import (
            upgrade_audit_hashes,
            upgrade_fetch_input,
            upgrade_session_csrf,
            upgrade_workpapers,
        )

        Base.metadata.create_all(engine)
        upgrade_audit_hashes(engine)
        upgrade_fetch_input(engine)
        upgrade_session_csrf(engine)
        upgrade_workpapers(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        seed(session)
    return factory


def _alembic_upgrade(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    # ConfigParser interpolates "%"; percent-encoded credentials need "%%".
    config.set_main_option("sqlalchemy.url", driver_url(url).replace("%", "%%"))
    command.upgrade(config, "head")


def database_ok(factory: sessionmaker[Session]) -> bool:
    session = factory()
    try:
        session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        session.close()


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
