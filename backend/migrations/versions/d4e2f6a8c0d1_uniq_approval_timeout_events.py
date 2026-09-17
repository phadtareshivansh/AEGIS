"""unique approval_timeout per conflict

Revision ID: d4e2f6a8c0d1
Revises: 008962697f76
Create Date: 2026-09-14 18:45:00.000000

Guarantees at most one approval_timeout event per conflict per scenario, so
the lazy lazy-emit path (replay/GET) and the in-process watchdog timer cannot
race and emit duplicates.
"""

from alembic import op
import sqlalchemy as sa

revision = "d4e2f6a8c0d1"
down_revision = "008962697f76"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_events_approval_timeout_uniq",
        "events",
        ["scenario_id", sa.text("(data ->> 'conflict_id')")],
        unique=True,
        postgresql_where=sa.text("type = 'approval_timeout'"),
    )


def downgrade() -> None:
    op.drop_index("ix_events_approval_timeout_uniq", table_name="events")