"""اعلان‌های هوشمند زمان‌بندی‌شده (فقط یک نمونه اجرا می‌کند؛ ارسال از طریق صف با محدودیت نرخ):

- دعوت به بازگشت: کاربری که N روز غیرفعال بوده، حداکثر هر ۳۰ روز یک پیام.
- یادآوری پایان پیشنهاد اختصاصی: ۲۴ ساعت قبل از انقضا، یک بار.
برای جلوگیری از ارسال تکراری، ثبت «ارسال شد» در reward_claims (کلید یکتا) قبل از صف کردن انجام می‌شود.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from . import notify
from .db import ago, now

from .worker import Context, periodic

log = logging.getLogger(__name__)
DEFAULT_COMEBACK = "👋 دلمان برایتان تنگ شده! سری به فروشگاه بزنید؛ قیمت‌ها به‌روز است."
BATCH = 500


async def _mark(ctx: Context, user_id: int, kind: str, day: str) -> bool:
    async with ctx.db.tx() as c:
        r = await c.execute(text("INSERT INTO reward_claims(user_id, kind, day, amount, created_at) "
                                 "VALUES(:u, :k, :d, 0, :t) ON CONFLICT DO NOTHING"),
                            {"u": user_id, "k": kind, "d": day, "t": now()})
        return r.rowcount == 1


@periodic("comeback", 6 * 3600)
async def comeback(ctx: Context) -> None:
    cfg = await ctx.db.get_json("comeback", {}) or {}
    if not cfg.get("enabled") or not await notify.enabled(ctx.db, "comeback"):
        return
    days = int(cfg.get("days", 14))
    rows = await ctx.db.all(
        "SELECT u.id FROM users u WHERE u.banned = 0 AND u.blocked = 0 AND u.last_seen < :since AND NOT EXISTS ("
        "SELECT 1 FROM reward_claims r WHERE r.user_id = u.id AND r.kind = 'comeback' AND r.day >= :recent) "
        "ORDER BY u.id LIMIT :n", {"since": ago(days), "recent": ago(30)[:10], "n": BATCH})
    sent = 0
    for r in rows:
        if await _mark(ctx, r["id"], "comeback", now()[:10]):
            await ctx.queue.enqueue("notify", {"chat_id": r["id"], "text": cfg.get("text") or DEFAULT_COMEBACK},
                                    max_attempts=3)
            sent += 1
    if sent:
        log.info("comeback: queued %s messages", sent)


@periodic("offer_ending", 3600)
async def offer_ending(ctx: Context) -> None:
    if not await notify.enabled(ctx.db, "offer_ending"):
        return
    for c in await ctx.db.expiring_offers(24):
        if (c["max_uses"] and c["used"] >= c["max_uses"]) or await ctx.db.coupon_used_by(c["code"], c["user_id"]):
            continue
        if await _mark(ctx, c["user_id"], "offer_end", c["expires_at"][:10]):
            await ctx.queue.enqueue("notify", {"chat_id": c["user_id"], "text": (
                f"⏰ پیشنهاد اختصاصی شما (<code>{c['code']}</code>، {c['percent']:g}% تخفیف) کمتر از ۲۴ ساعت دیگر "
                "تمام می‌شود. موقع خرید در پیش‌فاکتور «🎟 کد تخفیف دارم» را بزنید.")}, max_attempts=3)



