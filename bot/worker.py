"""کار پس‌زمینه: پیگیری خودکار سفارش‌ها و اطلاع‌رسانی به کاربر."""
from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot

from .db import Database
from .pricing import fmt_toman
from .shop import Shop
from .stard_api import StardError
from .ui import STATUS_LABEL

log = logging.getLogger(__name__)


async def notify_change(bot: Bot, db: Database, oid: int) -> None:
    o = await db.get_order(oid)
    if o is None:
        return
    text = (f"📦 <b>سفارش #{o['id']}</b>\n{escape(o['title'])} → {escape(o['recipient'] or '')}\n\n"
            f"وضعیت: <b>{STATUS_LABEL.get(o['status'], o['status'])}</b>")
    if o["status"] == "completed":
        text += "\n\n🎉 تحویل داده شد. از خرید شما ممنونیم!"
    elif o["refunded"]:
        u = await db.get_user(o["user_id"])
        text += f"\n\n💰 مبلغ {fmt_toman(o['price'])} به کیف پول شما برگشت.\nموجودی: <b>{fmt_toman(u.balance)}</b>"
    try:
        await bot.send_message(o["user_id"], text)
    except Exception as e:
        log.warning("notify %s failed: %s", o["user_id"], e)


async def sync_once(shop: Shop, bot: Bot) -> int:
    """یک دور همگام‌سازی همه‌ی سفارش‌های باز. تعداد تغییرها را برمی‌گرداند."""
    changed = 0
    for o in await shop.db.active_orders():
        try:
            change = await shop.sync_order(o["id"])
        except StardError as e:
            log.warning("sync order %s: %s", o["id"], e)
            if e.status == 429:
                break
            continue
        if change:
            changed += 1
            old, new = change
            log.info("order %s: %s -> %s", o["id"], old, new)
            await notify_change(bot, shop.db, o["id"])
        await asyncio.sleep(0.3)  # فاصله برای ماندن زیر سقف ۶۰ درخواست در دقیقه
    return changed


async def order_worker(shop: Shop, bot: Bot, interval: int) -> None:
    log.info("order worker started (every %ss)", interval)
    while True:
        try:
            await sync_once(shop, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("order worker error")
        await asyncio.sleep(interval)
