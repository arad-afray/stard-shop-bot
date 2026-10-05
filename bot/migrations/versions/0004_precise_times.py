"""millisecond timestamps for jobs, locks and heartbeats (VARCHAR(20) → VARCHAR(32))

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

COLUMNS = {
    "jobs": ("run_at", "locked_until", "created_at", "updated_at"),
    "locks": ("expires_at",),
    "heartbeats": ("last_seen", "started_at"),
}


def upgrade() -> None:
    for table, cols in COLUMNS.items():
        with op.batch_alter_table(table) as b:
            for c in cols:
                b.alter_column(c, type_=sa.String(32), existing_type=sa.String(20))


def downgrade() -> None:
    for table, cols in COLUMNS.items():
        with op.batch_alter_table(table) as b:
            for c in cols:
                b.alter_column(c, type_=sa.String(20), existing_type=sa.String(32))
