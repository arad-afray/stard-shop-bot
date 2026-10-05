"""قیمت لحظه‌ای دلار، TON و استارز؛ در گروه‌ها با نوشتن «قیمت دلار»، «قیمت تون»، «قیمت استارز» یا «قیمت» جواب می‌دهد.

نکته: برای اینکه ربات پیام‌های عادی گروه را ببیند، باید در @BotFather حالت Group Privacy را
خاموش کنید (Bot Settings → Group Privacy → Turn off) یا ربات را در گروه ادمین کنید.
"""
from __future__ import annotations

import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..db import Database
from ..pricing import fmt_toman
from ..shop import Shop
from ..stard_api import StardError
from ..ui import GPrice, price_group_menu

log = logging.getLogger(__name__)
router = Router(name="group")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

COOLDOWN = 3.0          # ثانیه؛ ضد اسپم در هر گروه
MAX_TRIGGER_LEN = 40    # پیام‌های طولانی بررسی نمی‌شوند تا ربات وسط گفتگو نپرد
_last: dict[int, float] = {}

_KEYWORDS = {
    "usd": ("دلار", "usd", "dollar"),
    "ton": ("تون", "تن", "ton", "toncoin"),
    "stars": ("استارز", "استار", "stars", "star", "ستاره"),
}
_TRIGGER = ("قیمت", "نرخ", "price")


def _norm(text: str) -> str:
    text = text.replace("ي", "ی").replace("ك", "ک").replace("‌", " ").lower()
    return re.sub(r"[^\w\s]", " ", text).strip()


def detect(text: str | None) -> str | None:
    """کدام قیمت خواسته شده: usd | ton | stars | all | None"""
    if not text or len(text) > MAX_TRIGGER_LEN or text.startswith("/"):
        return None
    words = _norm(text).split()
    if not words:
        return None
    asked = {k for k, kws in _KEYWORDS.items() if any(w in kws for w in words)}
    has_trigger = any(w in _TRIGGER or w.startswith("قیمت") for w in words)
    if has_trigger:
        if len(asked) == 1:
            return asked.pop()
        return "all"
    # فقط یک کلمه‌ی کلیدی تنها، مثل «دلار؟»
    if len(words) == 1 and len(asked) == 1:
        return asked.pop()
    return None


async def prices_text(shop: Shop, what: str) -> str:
    r = await shop.rates()
    star = int(r["star"]["amount"])
    ton = r.get("ton") or {}
    usd = (r.get("usd") or {}).get("amount")
    star_sell = await shop.star_unit_sell()
    parts = ["💹 <b>قیمت لحظه‌ای</b>\n"]
    if what in ("all", "usd") and usd:
        parts.append(f"💵 دلار: <b>{fmt_toman(int(usd))}</b>")
    if what in ("all", "ton") and ton.get("amount"):
        usd_ton = f" (≈ ${ton['usd']})" if ton.get("usd") else ""
        parts.append(f"💎 TON: <b>{fmt_toman(int(ton['amount']))}</b>{usd_ton}")
    if what in ("all", "stars"):
        parts.append(f"⭐ هر استارز: <b>{fmt_toman(star_sell)}</b>")
        parts.append(f"⭐ ۵۰ استارز: <b>{fmt_toman(await shop.sell('stars', star * 50))}</b>")
        parts.append(f"⭐ ۱۰۰ استارز: <b>{fmt_toman(await shop.sell('stars', star * 100))}</b>")
    updated = (r.get("updated_at") or "")[11:16]
    if updated:
        parts.append(f"\n🕒 به‌روزرسانی: {updated} UTC")
    return "\n".join(parts)


async def _enabled(db: Database) -> bool:
    return (await db.get_setting("group_prices", "1")) == "1"


@router.message(Command("price", "prices", "gheymat"))
@router.message(F.text.func(detect).as_("what"))
async def group_price(message: Message, shop: Shop, db: Database, bot: Bot, what: str = "all"):
    if not await _enabled(db):
        return
    if time.monotonic() - _last.get(message.chat.id, 0) < COOLDOWN:
        return
    _last[message.chat.id] = time.monotonic()
    try:
        text = await prices_text(shop, what if isinstance(what, str) else "all")
    except StardError as e:
        log.warning("group prices failed: %s", e)
        return
    me = await bot.me()
    await message.reply(text, reply_markup=price_group_menu(me.username))


@router.callback_query(GPrice.filter())
async def group_refresh(cb: CallbackQuery, shop: Shop):
    try:
        text = await prices_text(shop, "all")
    except StardError:
        await cb.answer("⚠️ الان ممکن نیست؛ کمی بعد.", show_alert=True)
        return
    try:
        await cb.message.edit_text(text, reply_markup=cb.message.reply_markup)
    except Exception:
        pass  # تغییری نکرده
    await cb.answer("🔄 به‌روز شد")
