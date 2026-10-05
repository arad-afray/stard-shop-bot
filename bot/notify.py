"""ارسال پیام به کاربر، مدیرها و کانال گزارش؛ خطای تلگرام هیچ‌وقت جریان اصلی را نمی‌شکند."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from .admins import Admins
from .db import Database
from .pricing import CATEGORIES, fmt_toman
from .ui import STATUS_LABEL, manual_order_menu

log = logging.getLogger(__name__)


async def safe_send(bot: Bot, chat_id: int | str, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
        return True
    except Exception as e:  # کاربر ربات را بلاک کرده، چت پیدا نشد، …
        log.warning("send to %s failed: %s", chat_id, e)
        return False


async def to_admins(bot: Bot, admins: Admins, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    for aid in admins.all():
        await safe_send(bot, aid, text, markup)


async def to_log_channel(bot: Bot, db: Database, text: str) -> None:
    chat = await db.get_setting("log_channel")
    if chat:
        await safe_send(bot, chat, text)


def order_line(o) -> str:
    extra = f"\n💌 {escape(o['gift_message'])}" if o["gift_message"] else ""
    disc = f" (تخفیف {fmt_toman(o['discount'])})" if o["discount"] else ""
    return (f"📦 <b>سفارش #{o['id']}</b> | {CATEGORIES.get(o['category'], o['category'])}\n"
            f"{escape(o['title'])} → {escape(o['recipient'] or '—')}{extra}\n"
            f"💵 فروش {fmt_toman(o['price'])}{disc} | خرید {fmt_toman(o['base_amount'])}\n"
            f"🆔 کاربر <code>{o['user_id']}</code> | {STATUS_LABEL.get(o['status'], o['status'])}")


async def order_placed(bot: Bot, db: Database, admins: Admins, oid: int) -> None:
    """اطلاع ثبت سفارش: به کانال گزارش، و برای سفارش دستی به همه‌ی مدیرها با دکمه‌ی انجام/رد."""
    o = await db.get_order(oid)
    if o is None:
        return
    await to_log_channel(bot, db, "🆕 " + order_line(o))
    if o["status"] == "manual":
        await to_admins(bot, admins, "🧑‍💻 <b>سفارش دستی جدید</b> — بعد از انجام، دکمه‌ی «انجام شد» را بزنید.\n\n"
                        + order_line(o), manual_order_menu(oid))


async def order_changed(bot: Bot, db: Database, oid: int) -> None:
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
    await safe_send(bot, o["user_id"], text)
    if o["status"] == "completed" or o["refunded"]:
        await to_log_channel(bot, db, ("✅ " if o["status"] == "completed" else "↩️ ") + order_line(o))


async def referral_paid(bot: Bot, db: Database, referrer: int, amount: int) -> None:
    u = await db.get_user(referrer)
    await safe_send(bot, referrer, f"🎁 پاداش زیرمجموعه: <b>+{fmt_toman(amount)}</b>\n"
                                   f"💰 موجودی: <b>{fmt_toman(u.balance if u else 0)}</b>")
