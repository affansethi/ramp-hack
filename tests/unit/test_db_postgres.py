"""Postgres URL handling: psycopg driver normalization and TLS enforcement."""

import pytest

from app.db import make_engine, normalize_database_url

_REMOTE = "postgresql://app:secret@db.example.com:5432/taxleak"


def test_postgresql_scheme_uses_psycopg_driver() -> None:
    engine = make_engine(_REMOTE + "?sslmode=require")
    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


def test_psycopg_scheme_passes_through() -> None:
    url = _REMOTE.replace("postgresql://", "postgresql+psycopg://")
    engine = make_engine(url + "?sslmode=require")
    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


def test_normalize_keeps_query_and_path() -> None:
    assert (
        normalize_database_url(_REMOTE + "?sslmode=require&connect_timeout=10")
        == "postgresql+psycopg://app:secret@db.example.com:5432/taxleak"
        "?sslmode=require&connect_timeout=10"
    )


def test_sqlite_url_unchanged() -> None:
    engine = make_engine("sqlite:///:memory:")
    try:
        assert engine.url.drivername == "sqlite"
    finally:
        engine.dispose()


def test_nonlocal_postgres_requires_sslmode() -> None:
    with pytest.raises(ValueError, match="sslmode=require"):
        make_engine(_REMOTE)


def test_nonlocal_postgres_rejects_weak_sslmode() -> None:
    with pytest.raises(ValueError, match="sslmode=require"):
        make_engine(_REMOTE + "?sslmode=disable")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_local_postgres_needs_no_sslmode(host: str) -> None:
    engine = make_engine(f"postgresql://app:secret@{host}:5432/taxleak")
    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()
