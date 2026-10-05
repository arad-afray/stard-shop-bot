"""v3: production schema (audit, jobs, locks, rate limits, flags, pricing, VIP, webhooks, API keys, refunds)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

T = sa.String(20)


def upgrade() -> None:
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("language_code", sa.String(16)))
        b.add_column(sa.Column("ban_reason", sa.String(256)))
        b.add_column(sa.Column("risk_score", sa.Integer, nullable=False, server_default="0"))
        b.add_column(sa.Column("last_seen", T))
    op.create_index("idx_users_username", "users", ["username"])
    op.create_index("idx_users_last_seen", "users", ["last_seen"])
    op.create_index("idx_users_referrer", "users", ["referrer_id"])

    with op.batch_alter_table("orders") as b:
        b.add_column(sa.Column("checkout_id", sa.String(64)))
        b.add_column(sa.Column("is_test", sa.Integer, nullable=False, server_default="0"))
    op.create_index("idx_orders_created", "orders", ["created_at"])
    op.create_index("uq_orders_checkout", "orders", ["checkout_id"], unique=True)

    with op.batch_alter_table("topups") as b:
        b.add_column(sa.Column("file_unique_id", sa.String(128)))
    op.create_index("idx_topups_status", "topups", ["status"])
    op.create_index("uq_topups_receipt", "topups", ["user_id", "file_unique_id"], unique=True)

    op.create_index("idx_ledger_kind", "ledger", ["kind"])
    op.create_index("idx_ledger_created", "ledger", ["created_at"])

    with op.batch_alter_table("coupons") as b:
        b.add_column(sa.Column("user_id", sa.BigInteger))
        b.add_column(sa.Column("category", sa.String(32)))
        b.add_column(sa.Column("expires_at", T))

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("admin_id", sa.BigInteger),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger),
        sa.Column("order_id", sa.Integer),
        sa.Column("ref", sa.String(128)),
        sa.Column("amount", sa.BigInteger),
        sa.Column("before", sa.Text),
        sa.Column("after", sa.Text),
        sa.Column("reason", sa.String(512)),
        sa.Column("created_at", T, nullable=False),
    )
    op.create_index("idx_audit_created", "audit_log", ["created_at"])
    op.create_index("idx_audit_user", "audit_log", ["user_id"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload", sa.Text, nullable=False, server_default="{}"),
        sa.Column("dedupe_key", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="8"),
        sa.Column("run_at", T, nullable=False),
        sa.Column("locked_by", sa.String(64)),
        sa.Column("locked_until", T),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", T, nullable=False),
        sa.Column("updated_at", T, nullable=False),
    )
    op.create_index("idx_jobs_ready", "jobs", ["status", "run_at"])
    op.create_index("uq_jobs_dedupe", "jobs", ["dedupe_key"], unique=True)

    op.create_table(
        "heartbeats",
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("instance", sa.String(64), nullable=False),
        sa.Column("last_seen", T, nullable=False),
        sa.Column("started_at", T, nullable=False),
        sa.Column("info", sa.Text),
        sa.PrimaryKeyConstraint("service", "instance"),
    )
    op.create_table("locks", sa.Column("name", sa.String(64), primary_key=True),
                    sa.Column("owner", sa.String(64), nullable=False), sa.Column("expires_at", T, nullable=False))
    op.create_table("rate_limits", sa.Column("key", sa.String(128), primary_key=True),
                    sa.Column("window", sa.BigInteger, nullable=False), sa.Column("count", sa.Integer, nullable=False))
    op.create_table("alert_state", sa.Column("key", sa.String(64), primary_key=True),
                    sa.Column("active", sa.Integer, nullable=False, server_default="0"),
                    sa.Column("last_sent", T), sa.Column("count", sa.Integer, nullable=False, server_default="0"))
    op.create_table(
        "risk_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("weight", sa.Integer, nullable=False),
        sa.Column("created_at", T, nullable=False),
    )
    op.create_index("idx_risk_user_time", "risk_events", ["user_id", "created_at"])
    op.create_table("feature_flags", sa.Column("key", sa.String(64), primary_key=True),
                    sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
                    sa.Column("rollout", sa.Integer, nullable=False, server_default="100"))
    op.create_table(
        "price_rules",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(32)),
        sa.Column("product_id", sa.BigInteger),
        sa.Column("percent", sa.Float, nullable=False),
        sa.Column("segment", sa.String(32), nullable=False, server_default="all"),
        sa.Column("starts_at", T),
        sa.Column("ends_at", T),
        sa.Column("active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", T, nullable=False),
    )
    op.create_table(
        "product_controls",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("stock", sa.BigInteger),
        sa.Column("sold", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("daily_limit", sa.Integer),
        sa.Column("hidden", sa.Integer, nullable=False, server_default="0"),
        sa.Column("languages", sa.String(128)),
    )
    op.create_table(
        "vip_levels",
        sa.Column("level", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("discount", sa.Float, nullable=False, server_default="0"),
        sa.Column("daily_limit", sa.Integer),
        sa.Column("min_spent", sa.BigInteger),
    )
    op.create_table(
        "user_vip",
        sa.Column("user_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("starts_at", T, nullable=False),
        sa.Column("ends_at", T),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="1"),
        sa.Column("response", sa.String(256)),
        sa.Column("payload", sa.Text),
        sa.Column("received_at", T, nullable=False),
    )
    op.create_index("idx_webhooks_time", "webhook_events", ["received_at"])
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.String(256), nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.Column("last_used_at", T),
        sa.Column("revoked_at", T),
    )
    op.create_index("uq_api_keys_hash", "api_keys", ["hash"], unique=True)
    op.create_table(
        "refunds",
        sa.Column("order_id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(256)),
        sa.Column("admin_id", sa.BigInteger),
        sa.Column("error", sa.String(256)),
        sa.Column("created_at", T, nullable=False),
        sa.Column("updated_at", T, nullable=False),
    )
    op.create_table(
        "reward_claims",
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("created_at", T, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "kind", "day"),
    )
    # سفارش‌های برگشتی قبلی در جدول refunds ثبت شوند تا مرکز بازپرداخت کامل باشد
    op.execute("INSERT INTO refunds(order_id, user_id, amount, status, reason, created_at, updated_at) "
               "SELECT id, user_id, price, 'completed', failure_reason, updated_at, updated_at FROM orders "
               "WHERE refunded = 1")


def downgrade() -> None:
    for t in ("reward_claims", "refunds", "api_keys", "webhook_events", "user_vip", "vip_levels", "product_controls",
              "price_rules", "feature_flags", "risk_events", "alert_state", "rate_limits", "locks", "heartbeats",
              "jobs", "audit_log"):
        op.drop_table(t)
    for idx, table in (("uq_topups_receipt", "topups"), ("idx_topups_status", "topups"),
                       ("uq_orders_checkout", "orders"), ("idx_orders_created", "orders"),
                       ("idx_ledger_kind", "ledger"), ("idx_ledger_created", "ledger"),
                       ("idx_users_username", "users"), ("idx_users_last_seen", "users"),
                       ("idx_users_referrer", "users")):
        op.drop_index(idx, table_name=table)
    with op.batch_alter_table("coupons") as b:
        for c in ("user_id", "category", "expires_at"):
            b.drop_column(c)
    with op.batch_alter_table("topups") as b:
        b.drop_column("file_unique_id")
    with op.batch_alter_table("orders") as b:
        b.drop_column("checkout_id")
        b.drop_column("is_test")
    with op.batch_alter_table("users") as b:
        for c in ("language_code", "ban_reason", "risk_score", "last_seen"):
            b.drop_column(c)
