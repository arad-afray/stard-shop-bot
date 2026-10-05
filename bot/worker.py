"""کار پس‌زمینه: پیگیری خودکار سفارش‌ها، اطلاع‌رسانی به کاربر و پرداخت پاداش معرف."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from . import notify
from .shop import Shop
from .stard_api import StardError

log = logging.getLogger(__name__)


async def on_status_change(bot: Bot, shop: Shop, oid: int) -> None:
    """بعد از هر تغییر وضعیت: خبر به کاربر و کانال گزارش، و اگر انجام شد پاداش معرف."""
    await notify.order_changed(bot, shop.db, oid)
    o = await shop.db.get_order(oid)
    if o is not None and o["status"] == "completed":
        paid = await shop.after_complete(oid)
        if paid:
            await notify.referral_paid(bot, shop.db, *paid)


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
            await on_status_change(bot, shop, o["id"])
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
