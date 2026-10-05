"""broadcast queue + blocked users

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

T = sa.String(20)


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("blocked", sa.Integer, nullable=False, server_default="0"))
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("admin_id", sa.BigInteger, nullable=False),
        sa.Column("from_chat", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger, nullable=False),
        sa.Column("segment", sa.String(32), nullable=False, server_default="all"),  # all | inactive:<days>
        sa.Column("cursor", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sent", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("blocked", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("created_at", T, nullable=False),
        sa.Column("finished_at", T),
    )


def downgrade() -> None:
    op.drop_table("broadcasts")
    with op.batch_alter_table("users") as b:
        b.drop_column("blocked")
