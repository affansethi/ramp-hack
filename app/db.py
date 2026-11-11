"""SQLite locally, Postgres when DATABASE_URL says so. Money stays text.

Postgres URLs use the psycopg v3 driver: ``postgresql://`` is normalized to
``postgresql+psycopg://``. Non-local Postgres hosts must carry
``sslmode=require`` (hosted Postgres requires TLS); local hosts
(localhost, 127.0.0.1, ::1, or an empty host for a Unix socket) are exempt.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_POSTGRES_SCHEMES = ("postgresql", "postgresql+psycopg")
_LOCAL_POSTGRES_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


def normalize_database_url(url: str) -> str:
    """Map ``postgresql://`` to the psycopg v3 driver scheme.

    ``postgresql+psycopg://`` and SQLite URLs pass through unchanged.
    """
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def _require_postgres_tls(url: str) -> None:
    """Refuse non-local Postgres URLs without ``sslmode=require``."""
    parsed = urlparse(url)
    if parsed.scheme not in _POSTGRES_SCHEMES:
        return
    if (parsed.hostname or "") in _LOCAL_POSTGRES_HOSTS:
        return
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if params.get("sslmode") != "require":
        raise ValueError(
            "Refusing non-local Postgres DATABASE_URL without sslmode=require "
            f"(host {parsed.hostname!r}); hosted Postgres requires TLS."
        )


def make_engine(url: str) -> Engine:
    normalized = normalize_database_url(url)
    _require_postgres_tls(normalized)
    connect_args = (
        {"check_same_thread": False, "timeout": 30}
        if normalized.startswith("sqlite")
        else {}
    )
    return create_engine(normalized, connect_args=connect_args)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_database(url: str) -> sessionmaker[Session]:
    from app import models  # noqa: F401  (register tables)
    from app.migrations import (
        upgrade_audit_hashes,
        upgrade_document_blob_key,
        upgrade_fetch_input,
        upgrade_session_csrf,
        upgrade_workpapers,
    )
    from app.seed import seed

    engine = make_engine(url)
    Base.metadata.create_all(engine)
    upgrade_audit_hashes(engine)
    upgrade_fetch_input(engine)
    upgrade_session_csrf(engine)
    upgrade_document_blob_key(engine)
    upgrade_workpapers(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        seed(session)
    return factory


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
