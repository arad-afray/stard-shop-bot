"""v2 baseline (شِمای نسخه‌ی 2.0.0)

Revision ID: 0001
Revises:
Create Date: 2026-10-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

T = sa.String(20)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("username", sa.String(64)),
        sa.Column("first_name", sa.String(256)),
        sa.Column("balance", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("banned", sa.Integer, nullable=False, server_default="0"),
        sa.Column("referrer_id", sa.BigInteger),
        sa.Column("created_at", T, nullable=False),
        sa.CheckConstraint("balance >= 0", name="ck_users_balance_nonneg"),
    )
    op.create_table("settings", sa.Column("key", sa.String(128), primary_key=True),
                    sa.Column("value", sa.Text, nullable=False))
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("product_id", sa.BigInteger),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
        sa.Column("duration", sa.Integer),
        sa.Column("recipient", sa.String(256)),
        sa.Column("gift_message", sa.String(256)),
        sa.Column("quote_id", sa.String(128)),
        sa.Column("base_amount", sa.BigInteger, nullable=False),
        sa.Column("price", sa.BigInteger, nullable=False),
        sa.Column("discount", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("coupon", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stard_ref", sa.String(128)),
        sa.Column("failure_reason", sa.String(256)),
        sa.Column("refunded", sa.Integer, nullable=False, server_default="0"),
        sa.Column("ref_paid", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", T, nullable=False),
        sa.Column("updated_at", T, nullable=False),
    )
    op.create_index("idx_orders_user", "orders", ["user_id"])
    op.create_index("idx_orders_status", "orders", ["status"])
    op.create_table(
        "topups",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("photo_id", sa.String(256)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("admin_id", sa.BigInteger),
        sa.Column("created_at", T, nullable=False),
    )
    op.create_table(
        "ledger",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ref", sa.String(128)),
        sa.Column("created_at", T, nullable=False),
    )
    op.create_index("idx_ledger_user", "ledger", ["user_id"])
    op.create_table(
        "coupons",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("percent", sa.Float, nullable=False),
        sa.Column("max_uses", sa.Integer, nullable=False, server_default="0"),
        sa.Column("used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", T, nullable=False),
    )
    op.create_table(
        "coupon_uses",
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("order_id", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("code", "user_id"),
    )


def downgrade() -> None:
    for t in ("coupon_uses", "coupons", "ledger", "topups", "orders", "settings", "users"):
        op.drop_table(t)
