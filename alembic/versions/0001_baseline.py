"""Create the current application tables.

Downgrade is omitted: this is a baseline revision for hosted Postgres.
Money columns are VARCHAR decimal strings, never float.
"""

from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)


def downgrade() -> None:
    raise NotImplementedError("baseline revision has no downgrade")
