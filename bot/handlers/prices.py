"""قیمت لحظه‌ای دلار، TON و استارز در گروه‌ها.

ربات به «قیمت»، «قیمت تون»، «تون»، «10 تون»، «۵۰۰ استارز چنده؟»، «100 دلار» و … جواب می‌دهد.
همه‌چیز از پنل ← 🛍 مدیریت فروشگاه ← 💹 قیمت در گروه قابل تنظیم است (منطق در bot/group_prices.py).

نکته: برای اینکه ربات پیام‌های عادی گروه را ببیند، باید در @BotFather حالت Group Privacy را
خاموش کنید (Bot Settings → Group Privacy → Turn off) یا ربات را در گروه ادمین کنید.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, ChatMemberUpdated, InlineQuery, InlineQueryResultArticle,
                           InputTextMessageContent, Message)

from .. import group_prices as gp
from ..db import Database
from ..pricing import fmt_toman
from ..shop import Shop
from ..stard_api import StardError
from ..ui import GPrice, price_group_menu

log = logging.getLogger(__name__)
router = Router(name="group")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

CHAT_MIN_GAP = 1.0      # ثانیه؛ حداقل فاصله‌ی دو جواب در یک گروه (ضد سیل)
_last: dict[tuple, float] = {}
_tasks: set[asyncio.Task] = set()
STATS: dict[str, int] = {"usd": 0, "ton": 0, "stars": 0, "all": 0, "amount": 0}


def detect(text: str | None) -> str | None:
    """سازگاری با نسخه‌ی قبل: فقط نوع سؤال (usd | ton | stars | all | None)."""
    q = gp.parse(text)
    return q.kind if q else None


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
    if what != "all":
        parts.append("\n💡 با تعداد هم بپرسید، مثلاً «10 تون» یا «۵۰۰ استارز»")
    updated = (r.get("updated_at") or "")[11:16]
    if updated:
        parts.append(f"\n🕒 به‌روزرسانی: {updated} UTC")
    return "\n".join(parts)


async def answer_for(shop: Shop, q: gp.Query) -> tuple[str, int | None]:
    """متن جواب و (برای استارز) تعداد قابل خرید."""
    if q.amount is None:
        return await prices_text(shop, q.kind), None
    r = await shop.rates()
    star_sell = await shop.star_unit_sell()
    total = None
    if q.kind == "stars":
        total = await shop.sell("stars", int(r["star"]["amount"]) * int(q.amount))
    text, buy = gp.amount_text(q, r, star_sell, total)
    updated = (r.get("updated_at") or "")[11:16]
    return text + (f"\n\n🕒 به‌روزرسانی: {updated} UTC" if updated else ""), buy


async def _remember_chat(db: Database, chat) -> None:
    known = await db.get_json("group_chats", {}) or {}
    key = str(chat.id)
    title = (chat.title or "")[:60]
    if known.get(key) != title:
        known[key] = title
        await db.set_json("group_chats", known)


def _delete_later(bot: Bot, chat_id: int, message_id: int, minutes: int) -> None:
    async def run():
        await asyncio.sleep(minutes * 60)
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass    # قبلاً پاک شده یا دسترسی نیست
    t = asyncio.create_task(run())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


@router.message(Command("price", "prices", "gheymat"))
async def group_price_command(message: Message, shop: Shop, db: Database, bot: Bot):
    arg = (message.text or "").split(maxsplit=1)
    q = gp.parse(arg[1], await gp.get_config(db)) if len(arg) > 1 else None
    await _reply(message, shop, db, bot, q or gp.Query("all"), command=True)


@router.message(F.text.len() <= gp.MAX_LEN)
async def group_price(message: Message, shop: Shop, db: Database, bot: Bot):
    cfg = await gp.get_config(db)
    q = gp.parse(message.text, cfg)
    if q is None:
        return
    await _reply(message, shop, db, bot, q, cfg=cfg)


async def _reply(message: Message, shop: Shop, db: Database, bot: Bot, q: gp.Query, *,
                 cfg: dict | None = None, command: bool = False) -> None:
    cfg = cfg or await gp.get_config(db)
    if not cfg["enabled"] or not gp.chat_allowed(cfg, message.chat.id):
        return
    if not command and not gp.kind_enabled(cfg, q):
        return
    now = time.monotonic()
    key = (message.chat.id, q.kind, str(q.amount or ""))
    if now - _last.get(key, 0) < float(cfg.get("cooldown", 3)) or now - _last.get((message.chat.id,), 0) < CHAT_MIN_GAP:
        return
    _last[key] = _last[(message.chat.id,)] = now
    try:
        text, buy = await answer_for(shop, q)
    except StardError as e:
        log.warning("group prices failed: %s", e)
        return
    me = await bot.me()
    sent = await message.reply(text, reply_markup=price_group_menu(
        me.username, q.kind, str(q.amount or ""), buy_stars=buy, buy_button=cfg.get("buy_button", True)))
    STATS["amount" if q.amount is not None else q.kind] += 1
    await _remember_chat(db, message.chat)
    if int(cfg.get("delete_after") or 0) > 0 and sent:
        _delete_later(bot, sent.chat.id, sent.message_id, int(cfg["delete_after"]))


@router.callback_query(GPrice.filter())
async def group_refresh(cb: CallbackQuery, callback_data: GPrice, shop: Shop):
    try:
        amount = Decimal(callback_data.amt) if callback_data.amt else None
    except InvalidOperation:
        amount = None
    q = gp.Query(callback_data.what if callback_data.what in (*gp.KINDS, "all") else "all", amount)
    try:
        text, _ = await answer_for(shop, q)
    except StardError:
        await cb.answer("⚠️ الان ممکن نیست؛ کمی بعد.", show_alert=True)
        return
    try:
        await cb.message.edit_text(text, reply_markup=cb.message.reply_markup)
    except Exception:
        pass  # تغییری نکرده
    await cb.answer("🔄 به‌روز شد")


@router.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def bot_membership(event: ChatMemberUpdated, db: Database):
    """گروه‌هایی که ربات در آن‌هاست برای انتخاب در پنل ثبت می‌شوند."""
    known = await db.get_json("group_chats", {}) or {}
    if event.new_chat_member.status in ("left", "kicked"):
        known.pop(str(event.chat.id), None)
    else:
        known[str(event.chat.id)] = (event.chat.title or "")[:60]
    await db.set_json("group_chats", known)


# ---------- حالت اینلاین: «@ربات 10 تون» در هر چتی ----------
INLINE_SAMPLES = (gp.Query("all"), gp.Query("stars", Decimal(100)), gp.Query("ton", Decimal(1)),
                  gp.Query("usd", Decimal(100)))


def _preview(body: str) -> str:
    """خط اول بعد از عنوان، بدون HTML، برای پیش‌نمایش نتیجه‌ی اینلاین."""
    lines = [re.sub(r"<[^>]+>", "", x).strip() for x in body.splitlines()]
    lines = [x for x in lines if x]
    return (lines[1] if len(lines) > 1 else lines[0] if lines else "")[:100]


def _title(q: gp.Query) -> str:
    if q.amount is None:
        return {"all": "💹 همه‌ی قیمت‌ها", "usd": "💵 قیمت دلار", "ton": "💎 قیمت TON", "stars": "⭐ قیمت استارز"}[q.kind]
    unit = {"usd": "دلار", "ton": "TON", "stars": "استارز"}[q.kind]
    return f"🧮 {gp.fmt_num(q.amount, 4)} {unit}"


@router.inline_query()
async def inline_prices(iq: InlineQuery, shop: Shop, db: Database, bot: Bot):
    cfg = await gp.get_config(db)
    if not cfg["enabled"] or not cfg.get("inline", True):
        await iq.answer([], cache_time=60, is_personal=False)
        return
    text = (iq.query or "").strip()
    q = gp.parse(text, cfg | {"bare_word": True, "amounts": True}) if text else None
    queries = [q] if q else list(INLINE_SAMPLES)
    me = await bot.me()
    results = []
    for i, item in enumerate(queries):
        try:
            body, buy = await answer_for(shop, item)
        except StardError:
            await iq.answer([], cache_time=5)
            return
        results.append(InlineQueryResultArticle(
            id=f"{item.kind}-{item.amount or 0}-{i}", title=_title(item),
            description=_preview(body),
            input_message_content=InputTextMessageContent(message_text=body, parse_mode="HTML"),
            reply_markup=price_group_menu(me.username, item.kind, str(item.amount or ""), buy_stars=buy,
                                          buy_button=cfg.get("buy_button", True))))
    STATS["inline"] = STATS.get("inline", 0) + 1
    await iq.answer(results, cache_time=30, is_personal=False)
