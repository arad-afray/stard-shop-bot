"""گزارش روزانه‌ی فروش برای مدیرها (به وقت تهران، روزی یک بار، فقط روی یک نمونه).

تنظیم در پنل ← ⚙️ تنظیمات ← 📨 گزارش روزانه؛ ذخیره در تنظیم daily_report: {"enabled": bool, "hour": 0..23}.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import finance, notify
from .db import Database, ts
from .pricing import fmt_toman
from .worker import Context, periodic

log = logging.getLogger(__name__)
TEHRAN = ZoneInfo("Asia/Tehran")
DEFAULT = {"enabled": True, "hour": 23}


async def get_config(db: Database) -> dict:
    cfg = dict(DEFAULT)
    cfg.update(await db.get_json("daily_report", {}) or {})
    return cfg


def day_bounds(local_now: datetime) -> tuple[str, str, str]:
    """شروع و پایان «امروز» به وقت تهران، به UTC (قالب created_at)، و تاریخ محلی."""
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return ts(start.astimezone(timezone.utc)), ts(end.astimezone(timezone.utc)), start.date().isoformat()


async def build(db: Database, local_now: datetime | None = None) -> str:
    local_now = local_now or datetime.now(TEHRAN)
    a, b, day = day_bounds(local_now)
    s = await finance.summary(db, a, b)
    prev = await finance.summary(db, *day_bounds(local_now - timedelta(days=1))[:2])
    new_users = await db.scalar("SELECT COUNT(*) FROM users WHERE created_at >= :a AND created_at < :b",
                                {"a": a, "b": b})
    failed = await db.scalar("SELECT COUNT(*) FROM orders WHERE status = 'failed' AND is_test = 0 "
                             "AND created_at >= :a AND created_at < :b", {"a": a, "b": b})
    topups = await db.one("SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS amount FROM topups "
                          "WHERE status = 'approved' AND created_at >= :a AND created_at < :b", {"a": a, "b": b})
    st = await db.stats()
    return (
        f"📨 <b>گزارش روزانه</b> — {day}\n\n"
        f"🧾 سفارش موفق: <b>{s.orders:,}</b> ({finance.growth(s.orders, prev.orders)} نسبت به دیروز)\n"
        f"💵 فروش: <b>{fmt_toman(s.revenue)}</b> ({finance.growth(s.revenue, prev.revenue)})\n"
        f"📈 سود خالص: <b>{fmt_toman(s.net_profit)}</b> | حاشیه‌ی سود: {s.margin}%\n"
        f"↩️ برگشت پول: {fmt_toman(s.refunds)} در {s.refunded_orders:,} سفارش\n"
        f"❌ سفارش ناموفق: {failed:,}\n"
        f"💳 شارژ تأییدشده: {topups['n']:,} ({fmt_toman(topups['amount'])})\n"
        f"👥 کاربر جدید: {new_users:,} | کل کاربران: {st['users']:,}\n\n"
        f"⏳ در انتظار شما: {st['pending_topups']:,} شارژ | {st['manual']:,} سفارش دستی"
    )


async def _claim(db: Database, day: str) -> bool:
    """فقط یک بار در روز (حتی با چند نمونه یا ری‌استارت)."""
    from sqlalchemy import text
    async with db.tx() as c:
        r = await c.execute(text("INSERT INTO reward_claims(user_id, kind, day, amount, created_at) "
                                 "VALUES(0, 'daily_report', :d, 0, :t) ON CONFLICT DO NOTHING"),
                            {"d": day, "t": ts(datetime.now(timezone.utc))})
        return r.rowcount == 1


@periodic("daily_report", 10 * 60)
async def daily_report(ctx: Context) -> None:
    cfg = await get_config(ctx.db)
    local_now = datetime.now(TEHRAN)
    if not cfg.get("enabled") or local_now.hour < int(cfg.get("hour", 23)):
        return
    if not await _claim(ctx.db, local_now.date().isoformat()):
        return
    await notify.to_admins(ctx.bot, ctx.admins, await build(ctx.db, local_now))
