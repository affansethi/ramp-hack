"""Small startup upgrades until the consolidated Alembic baseline lands."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def upgrade_session_csrf(engine: Engine) -> None:
    """Add the login CSRF column without rewriting sessions created before it."""
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if "sessions" not in tables:
            return
        columns = {
            column["name"] for column in inspect(connection).get_columns("sessions")
        }
        if "csrf_token" in columns:
            return
        connection.execute(
            text("ALTER TABLE sessions ADD COLUMN csrf_token VARCHAR(128)")
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_sessions_csrf_token "
                "ON sessions (csrf_token)"
            )
        )


def upgrade_audit_hashes(engine: Engine) -> None:
    """Add nullable hashes to existing history without rewriting any old rows."""
    with engine.begin() as connection:
        columns = {
            column["name"] for column in inspect(connection).get_columns("audit_events")
        }
        for name in ("before_hash", "after_hash"):
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE audit_events ADD COLUMN {name} VARCHAR(64)")
                )
