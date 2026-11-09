"""UTC timestamps stored as strings so SQLite never hands back a float clock."""

from datetime import UTC, datetime


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
