"""Small startup upgrades until the consolidated Alembic baseline lands."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


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
