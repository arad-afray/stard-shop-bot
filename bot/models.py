"""شِمای پایگاه داده (SQLAlchemy Core) — منبع واحد برای Alembic و تست‌ها.

قراردادها:
- زمان‌ها رشته‌ی ISO 8601 به وقت UTC هستند ("2026-10-05T10:00:00Z")؛ مقایسه‌ی رشته‌ای درست کار می‌کند
  و روی SQLite و PostgreSQL یکسان است.
- مبلغ‌ها عدد صحیح تومان (BigInteger) هستند.
- آیدی تلگرام BigInteger است (در PostgreSQL از ۳۲ بیت بزرگ‌تر می‌شود).
"""
from __future__ import annotations

from sqlalchemy import (BigInteger, CheckConstraint, Column, Float, Index, Integer, MetaData, PrimaryKeyConstraint,
                        String, Table, Text)

metadata = MetaData()

T = String(20)  # زمان ISO

users = Table(
    "users", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=False),
    Column("username", String(64)),
    Column("first_name", String(256)),
    Column("language_code", String(16)),
    Column("balance", BigInteger, nullable=False, server_default="0"),
    Column("banned", Integer, nullable=False, server_default="0"),
    Column("ban_reason", String(256)),
    Column("referrer_id", BigInteger),
    Column("risk_score", Integer, nullable=False, server_default="0"),
    Column("last_seen", T),
    Column("created_at", T, nullable=False),
    CheckConstraint("balance >= 0", name="ck_users_balance_nonneg"),
)
Index("idx_users_username", users.c.username)
Index("idx_users_last_seen", users.c.last_seen)
Index("idx_users_referrer", users.c.referrer_id)

settings = Table(
    "settings", metadata,
    Column("key", String(128), primary_key=True),
    Column("value", Text, nullable=False),
)

orders = Table(
    "orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("type", String(16), nullable=False),          # stars | product | boost | reaction
    Column("category", String(32), nullable=False),
    Column("product_id", BigInteger),
    Column("title", String(256), nullable=False),
    Column("quantity", Integer, nullable=False, server_default="1"),
    Column("duration", Integer),
    Column("recipient", String(256)),
    Column("gift_message", String(256)),
    Column("quote_id", String(128)),
    Column("base_amount", BigInteger, nullable=False),
    Column("price", BigInteger, nullable=False),
    Column("discount", BigInteger, nullable=False, server_default="0"),
    Column("coupon", String(64)),
    Column("status", String(16), nullable=False),
    Column("stard_ref", String(128)),
    Column("failure_reason", String(256)),
    Column("refunded", Integer, nullable=False, server_default="0"),
    Column("ref_paid", Integer, nullable=False, server_default="0"),
    Column("checkout_id", String(64)),                   # کلید یکتای خرید؛ کلیک دوباره سفارش دوم نمی‌سازد
    Column("is_test", Integer, nullable=False, server_default="0"),
    Column("created_at", T, nullable=False),
    Column("updated_at", T, nullable=False),
)
Index("idx_orders_user", orders.c.user_id)
Index("idx_orders_status", orders.c.status)
Index("idx_orders_created", orders.c.created_at)
Index("uq_orders_checkout", orders.c.checkout_id, unique=True)

topups = Table(
    "topups", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("photo_id", String(256)),
    Column("file_unique_id", String(128)),               # یک رسید دو بار ثبت نمی‌شود
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("admin_id", BigInteger),
    Column("created_at", T, nullable=False),
)
Index("idx_topups_status", topups.c.status)
Index("uq_topups_receipt", topups.c.user_id, topups.c.file_unique_id, unique=True)

ledger = Table(
    "ledger", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("kind", String(16), nullable=False),           # topup | order | refund | admin | referral | reward
    Column("ref", String(128)),
    Column("created_at", T, nullable=False),
)
Index("idx_ledger_user", ledger.c.user_id)
Index("idx_ledger_kind", ledger.c.kind)
Index("idx_ledger_created", ledger.c.created_at)

coupons = Table(
    "coupons", metadata,
    Column("code", String(64), primary_key=True),
    Column("percent", Float, nullable=False),
    Column("max_uses", Integer, nullable=False, server_default="0"),
    Column("used", Integer, nullable=False, server_default="0"),
    Column("active", Integer, nullable=False, server_default="1"),
    Column("user_id", BigInteger),                       # پیشنهاد اختصاصی: فقط برای این کاربر
    Column("category", String(32)),                      # فقط برای این بخش
    Column("expires_at", T),
    Column("created_at", T, nullable=False),
)

coupon_uses = Table(
    "coupon_uses", metadata,
    Column("code", String(64), nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("order_id", Integer, nullable=False),
    PrimaryKeyConstraint("code", "user_id"),
)

# ---------- v3 ----------
audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("admin_id", BigInteger),                      # None = سیستم
    Column("action", String(64), nullable=False),
    Column("user_id", BigInteger),
    Column("order_id", Integer),
    Column("ref", String(128)),                          # شناسه‌ی تراکنش، شارژ، کد، …
    Column("amount", BigInteger),
    Column("before", Text),
    Column("after", Text),
    Column("reason", String(512)),
    Column("created_at", T, nullable=False),
)
Index("idx_audit_created", audit_log.c.created_at)
Index("idx_audit_user", audit_log.c.user_id)

jobs = Table(
    "jobs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(32), nullable=False),
    Column("payload", Text, nullable=False, server_default="{}"),
    Column("dedupe_key", String(128)),                   # یک کار فعال برای هر کلید
    Column("status", String(16), nullable=False, server_default="queued"),  # queued|running|done|dead
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="8"),
    Column("run_at", T, nullable=False),
    Column("locked_by", String(64)),
    Column("locked_until", T),
    Column("last_error", Text),
    Column("created_at", T, nullable=False),
    Column("updated_at", T, nullable=False),
)
Index("idx_jobs_ready", jobs.c.status, jobs.c.run_at)
Index("uq_jobs_dedupe", jobs.c.dedupe_key, unique=True)

heartbeats = Table(
    "heartbeats", metadata,
    Column("service", String(32), nullable=False),
    Column("instance", String(64), nullable=False),
    Column("last_seen", T, nullable=False),
    Column("started_at", T, nullable=False),
    Column("info", Text),
    PrimaryKeyConstraint("service", "instance"),
)

locks = Table(
    "locks", metadata,
    Column("name", String(64), primary_key=True),
    Column("owner", String(64), nullable=False),
    Column("expires_at", T, nullable=False),
)

rate_limits = Table(
    "rate_limits", metadata,
    Column("key", String(128), primary_key=True),
    Column("window", BigInteger, nullable=False),
    Column("count", Integer, nullable=False),
)

alert_state = Table(
    "alert_state", metadata,
    Column("key", String(64), primary_key=True),
    Column("active", Integer, nullable=False, server_default="0"),
    Column("last_sent", T),
    Column("count", Integer, nullable=False, server_default="0"),
)

risk_events = Table(
    "risk_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("weight", Integer, nullable=False),
    Column("created_at", T, nullable=False),
)
Index("idx_risk_user_time", risk_events.c.user_id, risk_events.c.created_at)

feature_flags = Table(
    "feature_flags", metadata,
    Column("key", String(64), primary_key=True),
    Column("enabled", Integer, nullable=False, server_default="1"),
    Column("rollout", Integer, nullable=False, server_default="100"),  # درصد کاربران
)

price_rules = Table(
    "price_rules", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(128), nullable=False),
    Column("category", String(32)),                      # None = همه
    Column("product_id", BigInteger),
    Column("percent", Float, nullable=False),            # منفی = تخفیف (Flash Sale)، مثبت = افزایش
    Column("segment", String(32), nullable=False, server_default="all"),  # all | vip | vip:<level>
    Column("starts_at", T),
    Column("ends_at", T),
    Column("active", Integer, nullable=False, server_default="1"),
    Column("created_at", T, nullable=False),
)

product_controls = Table(
    "product_controls", metadata,
    Column("key", String(64), primary_key=True),         # "stars" یا "star_gift:12577"
    Column("stock", BigInteger),                         # None = نامحدود
    Column("sold", BigInteger, nullable=False, server_default="0"),
    Column("daily_limit", Integer),                      # حداکثر خرید هر کاربر در ۲۴ ساعت
    Column("hidden", Integer, nullable=False, server_default="0"),
    Column("languages", String(128)),                    # کدهای زبان مجاز تلگرام، مثل "fa,en"
)

vip_levels = Table(
    "vip_levels", metadata,
    Column("level", Integer, primary_key=True, autoincrement=False),
    Column("name", String(64), nullable=False),
    Column("discount", Float, nullable=False, server_default="0"),
    Column("daily_limit", Integer),                      # سقف تعداد سفارش در روز (None = بدون سقف)
    Column("min_spent", BigInteger),                     # ارتقای خودکار با این مجموع خرید
)

user_vip = Table(
    "user_vip", metadata,
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("level", Integer, nullable=False),
    Column("starts_at", T, nullable=False),
    Column("ends_at", T),
)

webhook_events = Table(
    "webhook_events", metadata,
    Column("id", String(64), primary_key=True),          # Stard-Event-Id: پردازش تکراری ممنوع
    Column("type", String(64), nullable=False),
    Column("status", String(16), nullable=False),        # received | processed | failed | rejected
    Column("attempts", Integer, nullable=False, server_default="1"),
    Column("response", String(256)),
    Column("payload", Text),
    Column("received_at", T, nullable=False),
)
Index("idx_webhooks_time", webhook_events.c.received_at)

api_keys = Table(
    "api_keys", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(64), nullable=False),
    Column("prefix", String(16), nullable=False),
    Column("hash", String(64), nullable=False),
    Column("scopes", String(256), nullable=False),
    Column("created_at", T, nullable=False),
    Column("last_used_at", T),
    Column("revoked_at", T),
)
Index("uq_api_keys_hash", api_keys.c.hash, unique=True)

refunds = Table(
    "refunds", metadata,
    Column("order_id", Integer, primary_key=True, autoincrement=False),  # هر سفارش حداکثر یک برگشت
    Column("user_id", BigInteger, nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),        # pending | completed | failed
    Column("reason", String(256)),
    Column("admin_id", BigInteger),
    Column("error", String(256)),
    Column("created_at", T, nullable=False),
    Column("updated_at", T, nullable=False),
)

reward_claims = Table(
    "reward_claims", metadata,
    Column("user_id", BigInteger, nullable=False),
    Column("kind", String(16), nullable=False),          # daily | spin
    Column("day", String(10), nullable=False),           # YYYY-MM-DD (UTC)
    Column("amount", BigInteger, nullable=False),
    Column("created_at", T, nullable=False),
    PrimaryKeyConstraint("user_id", "kind", "day"),
)
