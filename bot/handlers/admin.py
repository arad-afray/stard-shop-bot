"""پنل مدیریت: آمار، سود، شارژها، کاربران، سفارش‌ها، بخش‌ها، جوین اجباری، کد تخفیف،
زیرمجموعه‌گیری، قیمت در گروه، کیف پول Stard، پیام همگانی، تنظیمات، پشتیبان و مدیرها."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import __version__, notify
from ..admins import Admins
from ..config import Settings
from ..db import Database, InsufficientBalance
from ..middlewares import JoinChecker
from ..pricing import CATEGORIES, fmt_toman, to_float, to_int
from ..shop import COUPON_RE, Shop, ShopError
from ..stard_api import StardError
from ..ui import (BTN_ADMIN, STATUS_LABEL, Adm, admin_menu, back_admin, cancel_menu, drop_markup, edit_or_send,
                  main_menu, topup_review_menu)
from ..worker import on_status_change

log = logging.getLogger(__name__)
router = Router(name="admin")


class IsAdmin(Filter):
    async def __call__(self, event: Any, is_admin: bool = False) -> bool:
        return is_admin


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminForm(StatesGroup):
    profit = State()
    find_user = State()
    balance = State()
    user_msg = State()
    broadcast = State()
    setting = State()
    join_add = State()
    coupon = State()
    referral = State()
    add_admin = State()
    find_order = State()


SETTINGS = {
    "card_number": "💳 شماره کارت",
    "card_holder": "👤 نام صاحب کارت",
    "min_topup": "⬇️ حداقل شارژ (تومان)",
    "support_text": "🆘 متن پشتیبانی",
    "welcome_text": "👋 متن خوش‌آمد",
    "log_channel": "📝 کانال گزارش سفارش‌ها",
    "round_to": "🔢 گرد کردن قیمت (تومان)",
}
SETTING_HINT = {
    "log_channel": "آیدی عددی (مثل -1001234567890) یا یوزرنیم کانال (@mylog). ربات باید ادمین کانال باشد. "
                   "برای حذف، کلمه‌ی «حذف» را بفرستید.",
    "round_to": "یکی از 1، 10، 100، 1000 یا 10000",
    "welcome_text": "برای برگشت به متن پیش‌فرض، کلمه‌ی «حذف» را بفرستید.",
}


async def _home_text(shop: Shop, db: Database) -> str:
    s = await db.stats()
    return (f"⚙️ <b>پنل مدیریت</b> — نسخه {__version__}\n\n"
            f"وضعیت فروشگاه: {'🟢 باز' if await shop.is_open() else '🔴 بسته'}\n"
            f"سود پیش‌فرض: {await shop.get_profit():g}%\n"
            f"👥 کاربران: {s['users']:,} | ⏳ سفارش باز: {s['active']:,}\n"
            f"🧑‍💻 سفارش دستی منتظر: {s['manual']:,} | 💳 شارژ منتظر: {s['pending_topups']:,}")


async def _home(shop: Shop, db: Database, settings: Settings, is_owner: bool):
    return await _home_text(shop, db), admin_menu(await shop.is_open(), settings.is_test, is_owner)


@router.message(F.text == BTN_ADMIN)
@router.message(Command("admin", "panel"))
async def admin_home(message: Message, state: FSMContext, shop: Shop, db: Database, settings: Settings,
                     is_owner: bool):
    await state.clear()
    text, kb = await _home(shop, db, settings, is_owner)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Adm.filter(F.name == "home"))
async def cb_home(cb: CallbackQuery, state: FSMContext, shop: Shop, db: Database, settings: Settings, is_owner: bool):
    await state.clear()
    text, kb = await _home(shop, db, settings, is_owner)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "toggle"))
async def cb_toggle(cb: CallbackQuery, db: Database, shop: Shop, settings: Settings, is_owner: bool):
    await db.set_setting("shop_open", "0" if await shop.is_open() else "1")
    text, kb = await _home(shop, db, settings, is_owner)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("انجام شد")


# ---------- آمار ----------
@router.callback_query(Adm.filter(F.name == "stats"))
async def cb_stats(cb: CallbackQuery, db: Database):
    s = await db.stats()
    lines = ["📊 <b>آمار فروشگاه</b>\n",
             f"👥 کاربران: {s['users']:,} (مسدود: {s['banned']:,})",
             f"💰 مجموع موجودی کاربران: {fmt_toman(s['balances'])}",
             f"💳 مجموع شارژهای تأییدشده: {fmt_toman(s['topups'])}",
             f"✅ سفارش انجام‌شده: {s['done']:,}",
             f"⏳ سفارش در جریان: {s['active']:,} (دستی: {s['manual']:,})",
             f"↩️ سفارش برگشتی: {s['refunded']:,}",
             f"💵 فروش کل: {fmt_toman(s['sales'])}",
             f"📈 سود خالص: <b>{fmt_toman(s['profit'] - s['referral'])}</b>"
             + (f" (پس از {fmt_toman(s['referral'])} پاداش معرف)" if s["referral"] else ""),
             ""]
    for days, label in ((1, "۲۴ ساعت اخیر"), (7, "۷ روز اخیر"), (30, "۳۰ روز اخیر")):
        p = await db.period_stats(days)
        lines.append(f"📅 <b>{label}</b>: {p['users']:,} کاربر تازه | {p['done']:,} سفارش | "
                     f"فروش {fmt_toman(p['sales'])} | سود {fmt_toman(p['profit'])}")
    cats = await db.category_stats()
    if cats:
        lines.append("\n🗂 <b>به تفکیک بخش</b>")
        for c in cats:
            lines.append(f"{CATEGORIES.get(c['category'], c['category'])}: {c['n']:,} سفارش | "
                         f"فروش {fmt_toman(c['sales'])} | سود {fmt_toman(c['profit'])}")
    top = await db.top_buyers(5)
    if top:
        lines.append("\n🏆 <b>بهترین خریداران</b>")
        for i, t in enumerate(top, 1):
            name = f"@{t['username']}" if t["username"] else (t["first_name"] or str(t["id"]))
            lines.append(f"{i}. {escape(name)} (<code>{t['id']}</code>): {fmt_toman(t['total'])} در {t['n']:,} سفارش")
    await edit_or_send(cb.message, "\n".join(lines), back_admin())
    await cb.answer()


# ---------- درصد سود ----------
async def _profit_view(shop: Shop):
    b = InlineKeyboardBuilder()
    lines = ["💰 <b>درصد سود</b>\n",
             f"قیمت فروش = قیمت Stard × (۱ + درصد سود)، گرد به بالا تا {await shop.round_to():,} تومان.\n",
             f"🌐 پیش‌فرض (همه‌ی بخش‌ها): <b>{await shop.get_profit():g}%</b>"]
    b.button(text="🌐 تغییر سود پیش‌فرض", callback_data=Adm(name="pset", arg="global"))
    for key, label in CATEGORIES.items():
        own = await shop.db.get_setting(f"profit:{key}")
        lines.append(f"{label}: <b>{await shop.get_profit(key):g}%</b>" + ("" if own is not None else " (پیش‌فرض)"))
        b.button(text=f"✏️ {label}", callback_data=Adm(name="pset", arg=key))
        if own is not None:
            b.button(text="♻️ پیش‌فرض", callback_data=Adm(name="pclr", arg=key))
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


@router.callback_query(Adm.filter(F.name == "profit"))
async def cb_profit(cb: CallbackQuery, shop: Shop):
    text, kb = await _profit_view(shop)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "pclr"))
async def cb_profit_clear(cb: CallbackQuery, callback_data: Adm, shop: Shop):
    if callback_data.arg in CATEGORIES:
        await shop.clear_category_profit(callback_data.arg)
    text, kb = await _profit_view(shop)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("به پیش‌فرض برگشت")


@router.callback_query(Adm.filter(F.name == "pset"))
async def cb_profit_set(cb: CallbackQuery, callback_data: Adm, state: FSMContext):
    cat = callback_data.arg
    if cat != "global" and cat not in CATEGORIES:
        await cb.answer()
        return
    await state.set_state(AdminForm.profit)
    await state.update_data(cat=cat)
    label = "پیش‌فرض" if cat == "global" else CATEGORIES[cat]
    await cb.message.answer(f"درصد سود جدید برای «{label}» را بفرستید (مثلاً 12.5):", reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.profit)
async def profit_value(message: Message, state: FSMContext, shop: Shop):
    value = to_float(message.text)
    if value is None:
        await message.answer("❗️ یک عدد بفرستید، مثلاً 10")
        return
    cat = (await state.get_data()).get("cat", "global")
    try:
        await shop.set_profit(value, None if cat == "global" else cat)
    except ShopError as e:
        await message.answer(f"❗️ {e}")
        return
    await state.clear()
    await message.answer(f"✅ سود روی {value:g}% تنظیم شد.", reply_markup=main_menu(True))
    text, kb = await _profit_view(shop)
    await message.answer(text, reply_markup=kb)


# ---------- شارژها ----------
@router.callback_query(Adm.filter(F.name == "topups"))
async def cb_topups(cb: CallbackQuery, db: Database, bot: Bot):
    rows = await db.pending_topups()
    if not rows:
        await cb.answer("درخواست شارژ در انتظاری نیست ✅", show_alert=True)
        return
    await cb.answer()
    for t in rows[:20]:
        u = await db.get_user(t["user_id"])
        uname = f"@{escape(u.username)}" if u and u.username else "—"
        try:
            await bot.send_photo(cb.from_user.id, t["photo_id"],
                                 caption=f"💳 <b>درخواست شارژ #{t['id']}</b>\n👤 {uname} 🆔 <code>{t['user_id']}</code>\n"
                                         f"💵 مبلغ: <b>{fmt_toman(t['amount'])}</b>\n📅 {t['created_at']}",
                                 reply_markup=topup_review_menu(t["id"]))
        except TelegramBadRequest as e:
            log.warning("topup photo %s: %s", t["id"], e)
    if len(rows) > 20:
        await cb.message.answer(f"… و {len(rows) - 20:,} درخواست دیگر. بعد از بررسی این‌ها دوباره بزنید.")


@router.callback_query(Adm.filter(F.name.in_({"tp_ok", "tp_no"})))
async def cb_topup_resolve(cb: CallbackQuery, callback_data: Adm, db: Database, bot: Bot):
    approve = callback_data.name == "tp_ok"
    tid = to_int(callback_data.arg)
    row = await db.resolve_topup(tid, cb.from_user.id, approve) if tid else None
    if row is None:
        await cb.answer("این درخواست قبلاً بررسی شده است.", show_alert=True)
        await drop_markup(cb.message)
        return
    verdict = "✅ تأیید شد" if approve else "❌ رد شد"
    with contextlib.suppress(TelegramBadRequest, AttributeError):
        await cb.message.edit_caption(caption=f"{cb.message.caption or ''}\n\n{verdict} توسط {cb.from_user.id}")
    user = await db.get_user(row["user_id"])
    if approve:
        await notify.safe_send(bot, row["user_id"], f"✅ شارژ {fmt_toman(row['amount'])} تأیید شد.\n"
                                                    f"💰 موجودی جدید: <b>{fmt_toman(user.balance)}</b>")
    else:
        await notify.safe_send(bot, row["user_id"],
                               f"❌ درخواست شارژ #{row['id']} رد شد. برای پیگیری با پشتیبانی تماس بگیرید.")
    await cb.answer(verdict)


# ---------- مدیریت کاربر ----------
@router.callback_query(Adm.filter(F.name == "user"))
async def cb_user(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.find_user)
    await cb.message.answer("🆔 آیدی عددی یا یوزرنیم کاربر را بفرستید:", reply_markup=cancel_menu())
    await cb.answer()


async def _user_card(db: Database, uid: int):
    u = await db.get_user(uid)
    if u is None:
        return "❗️ کاربر پیدا نشد.", back_admin()
    b = InlineKeyboardBuilder()
    b.button(text="➕ افزایش موجودی", callback_data=Adm(name="bal_add", arg=str(uid)))
    b.button(text="➖ کاهش موجودی", callback_data=Adm(name="bal_sub", arg=str(uid)))
    b.button(text="📦 سفارش‌ها", callback_data=Adm(name="u_orders", arg=str(uid)))
    b.button(text="📒 تراکنش‌ها", callback_data=Adm(name="u_ledger", arg=str(uid)))
    b.button(text="✉️ پیام به کاربر", callback_data=Adm(name="u_msg", arg=str(uid)))
    b.button(text="✅ رفع مسدودی" if u.banned else "🚫 مسدود کردن", callback_data=Adm(name="ban", arg=str(uid)))
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    b.adjust(2, 2, 2, 1)
    orders = await db.count_user_orders(uid)
    ref = f"\n👤 معرف: <code>{u.referrer_id}</code>" if u.referrer_id else ""
    text = (f"👤 <b>{escape(u.first_name or '')}</b> {('@' + escape(u.username)) if u.username else ''}\n"
            f"🆔 <code>{u.id}</code>\n💰 موجودی: <b>{fmt_toman(u.balance)}</b>\n"
            f"📦 سفارش‌ها: {orders:,} | 🛒 خرید: {fmt_toman(await db.user_spent(uid))}\n"
            f"👥 زیرمجموعه: {await db.count_referrals(uid):,} | 🎁 پاداش: {fmt_toman(await db.referral_earnings(uid))}"
            f"{ref}\nوضعیت: {'🚫 مسدود' if u.banned else '✅ فعال'}\n📅 عضویت: {u.created_at[:10]}")
    return text, b.as_markup()


@router.message(AdminForm.find_user)
async def find_user(message: Message, state: FSMContext, db: Database):
    q = message.text or ""
    n = to_int(q)
    u = await db.find_user(str(n) if n is not None else q)
    if not u:
        await message.answer("❗️ کاربر پیدا نشد. کاربر باید حداقل یک بار ربات را استارت کرده باشد.")
        return
    await state.clear()
    await message.answer("کاربر پیدا شد:", reply_markup=main_menu(True))
    text, kb = await _user_card(db, u.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Adm.filter(F.name == "ucard"))
async def cb_user_card(cb: CallbackQuery, callback_data: Adm, db: Database):
    text, kb = await _user_card(db, to_int(callback_data.arg) or 0)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "ban"))
async def cb_ban(cb: CallbackQuery, callback_data: Adm, db: Database, admins: Admins):
    uid = to_int(callback_data.arg) or 0
    u = await db.get_user(uid)
    if u is None:
        await cb.answer("کاربر پیدا نشد.", show_alert=True)
        return
    if admins.is_admin(uid):
        await cb.answer("مدیر را نمی‌شود مسدود کرد.", show_alert=True)
        return
    await db.set_banned(uid, not u.banned)
    text, kb = await _user_card(db, uid)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("انجام شد")


def _back_to_user(uid: int):
    b = InlineKeyboardBuilder()
    b.button(text="🔙 کارت کاربر", callback_data=Adm(name="ucard", arg=str(uid)))
    return b.as_markup()


@router.callback_query(Adm.filter(F.name == "u_orders"))
async def cb_user_orders(cb: CallbackQuery, callback_data: Adm, db: Database):
    uid = to_int(callback_data.arg) or 0
    rows = await db.user_orders(uid, limit=15)
    if not rows:
        await cb.answer("این کاربر سفارشی ندارد.", show_alert=True)
        return
    lines = [f"📦 <b>سفارش‌های کاربر {uid}</b>\n"] + [_order_short(o) for o in rows]
    lines.append("\nبرای جزئیات و مدیریت: 🧾 سفارش‌ها ← 🔎 جستجوی سفارش")
    await edit_or_send(cb.message, "\n".join(lines), _back_to_user(uid))
    await cb.answer()


LEDGER_KIND = {"topup": "💳 شارژ", "order": "🛒 خرید", "refund": "↩️ برگشت", "admin": "👮 مدیر",
               "referral": "🎁 زیرمجموعه"}


@router.callback_query(Adm.filter(F.name == "u_ledger"))
async def cb_user_ledger(cb: CallbackQuery, callback_data: Adm, db: Database):
    uid = to_int(callback_data.arg) or 0
    rows = await db.user_ledger(uid, 20)
    if not rows:
        await cb.answer("تراکنشی ثبت نشده.", show_alert=True)
        return
    lines = [f"📒 <b>۲۰ تراکنش آخر کاربر {uid}</b>\n"]
    for r in rows:
        sign = "+" if r["amount"] > 0 else "−"
        lines.append(f"{LEDGER_KIND.get(r['kind'], r['kind'])} {sign}{fmt_toman(abs(r['amount']))} "
                     f"| {escape(r['ref'] or '')} | {r['created_at'][:16].replace('T', ' ')}")
    await edit_or_send(cb.message, "\n".join(lines), _back_to_user(uid))
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "u_msg"))
async def cb_user_msg(cb: CallbackQuery, callback_data: Adm, state: FSMContext):
    await state.set_state(AdminForm.user_msg)
    await state.update_data(uid=to_int(callback_data.arg))
    await cb.message.answer("✉️ پیام (متن، عکس، …) را بفرستید تا برای کاربر ارسال شود:", reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.user_msg)
async def user_msg(message: Message, state: FSMContext, bot: Bot):
    uid = (await state.get_data()).get("uid")
    await state.clear()
    try:
        await bot.send_message(uid, "📩 <b>پیام از پشتیبانی:</b>")
        await bot.copy_message(uid, message.chat.id, message.message_id)
        await message.answer("✅ ارسال شد.", reply_markup=main_menu(True))
    except Exception as e:
        await message.answer(f"❗️ ارسال نشد: {escape(str(e))[:200]}", reply_markup=main_menu(True))


@router.callback_query(Adm.filter(F.name.in_({"bal_add", "bal_sub"})))
async def cb_balance(cb: CallbackQuery, callback_data: Adm, state: FSMContext):
    await state.set_state(AdminForm.balance)
    await state.update_data(uid=to_int(callback_data.arg), sign=1 if callback_data.name == "bal_add" else -1)
    await cb.message.answer("💵 مبلغ (تومان) را بفرستید:", reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.balance)
async def balance_value(message: Message, state: FSMContext, db: Database, bot: Bot):
    amount = to_int(message.text)
    if amount is None or amount <= 0:
        await message.answer("❗️ یک عدد مثبت بفرستید.")
        return
    data = await state.get_data()
    uid = data["uid"]
    try:
        if data["sign"] > 0:
            new = await db.credit(uid, amount, "admin", f"by:{message.from_user.id}")
        else:
            new = await db.debit(uid, amount, "admin", f"by:{message.from_user.id}")
    except InsufficientBalance:
        await message.answer("❗️ موجودی کاربر کمتر از این مبلغ است.")
        return
    except ValueError:
        await state.clear()
        await message.answer("❗️ کاربر پیدا نشد.", reply_markup=main_menu(True))
        return
    await state.clear()
    sign = "+" if data["sign"] > 0 else "−"
    await message.answer(f"✅ {sign}{fmt_toman(amount)} | موجودی جدید: {fmt_toman(new)}", reply_markup=main_menu(True))
    await notify.safe_send(bot, uid, f"💰 موجودی شما توسط مدیر {'افزایش' if data['sign'] > 0 else 'کاهش'} یافت: "
                                     f"{sign}{fmt_toman(amount)}\nموجودی جدید: <b>{fmt_toman(new)}</b>")
    text, kb = await _user_card(db, uid)
    await message.answer(text, reply_markup=kb)


# ---------- سفارش‌ها ----------
def _order_short(o) -> str:
    return (f"<b>#{o['id']}</b> 🆔{o['user_id']} | {escape(o['title'])} → {escape(o['recipient'] or '')}\n"
            f"   {STATUS_LABEL.get(o['status'], o['status'])} | فروش {fmt_toman(o['price'])} | "
            f"خرید {fmt_toman(o['base_amount'])}"
            + (f"\n   ⚠️ {escape(o['failure_reason'])}" if o["failure_reason"] else ""))


def _orders_menu():
    b = InlineKeyboardBuilder()
    b.button(text="🕘 اخیر", callback_data=Adm(name="orders", arg="recent"))
    b.button(text="⏳ باز", callback_data=Adm(name="orders", arg="open"))
    b.button(text="🧑‍💻 دستی منتظر", callback_data=Adm(name="orders", arg="manual"))
    b.button(text="🔎 جستجوی سفارش", callback_data=Adm(name="o_find"))
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    b.adjust(3, 1, 1)
    return b


@router.callback_query(Adm.filter(F.name == "orders"))
async def cb_orders(cb: CallbackQuery, callback_data: Adm, db: Database):
    kind = callback_data.arg or "recent"
    if kind == "open":
        rows, title = await db.orders_by_status(("new", "pending", "processing", "manual"), 15), "سفارش‌های باز"
    elif kind == "manual":
        rows, title = await db.orders_by_status(("manual",), 15), "سفارش‌های دستی منتظر"
    else:
        rows, title = await db.recent_orders(15), "۱۵ سفارش اخیر"
    lines = [f"🧾 <b>{title}</b>\n"] + ([_order_short(o) for o in rows] or ["— موردی نیست —"])
    b = _orders_menu()
    if kind == "manual" and rows:
        actions = InlineKeyboardBuilder()
        for o in rows[:8]:
            actions.button(text=f"#{o['id']} مدیریت", callback_data=Adm(name="ocard", arg=str(o["id"])))
        actions.adjust(4)
        actions.attach(b)
        b = actions
    text = "\n".join(lines)
    await edit_or_send(cb.message, text[:4000], b.as_markup())
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "o_find"))
async def cb_order_find(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.find_order)
    await cb.message.answer("🔎 شماره‌ی سفارش را بفرستید (مثلاً 42):", reply_markup=cancel_menu())
    await cb.answer()


async def _order_card(db: Database, oid: int):
    o = await db.get_order(oid)
    if o is None:
        return None, None
    b = InlineKeyboardBuilder()
    if o["status"] == "manual":
        b.button(text="✅ انجام شد", callback_data=Adm(name="o_done", arg=str(oid)))
    if o["stard_ref"] and o["status"] in ("new", "pending", "processing"):
        b.button(text="🔄 استعلام از Stard", callback_data=Adm(name="o_sync", arg=str(oid)))
    if not o["refunded"] and o["status"] != "completed":
        b.button(text="↩️ لغو و برگشت پول", callback_data=Adm(name="o_refund", arg=str(oid)))
    b.button(text="👤 کاربر", callback_data=Adm(name="ucard", arg=str(o["user_id"])))
    b.button(text="🔙 سفارش‌ها", callback_data=Adm(name="orders"))
    b.adjust(2)
    text = (notify.order_line(o) + f"\n🔗 Stard: <code>{escape(o['stard_ref'] or '—')}</code>"
            f"\n📅 {o['created_at'][:16].replace('T', ' ')} | به‌روز: {o['updated_at'][:16].replace('T', ' ')}"
            + (f"\n⚠️ {escape(o['failure_reason'])}" if o["failure_reason"] else ""))
    return text, b.as_markup()


@router.message(AdminForm.find_order)
async def find_order(message: Message, state: FSMContext, db: Database):
    oid = to_int((message.text or "").lstrip("#"))
    text, kb = await _order_card(db, oid or 0)
    if text is None:
        await message.answer("❗️ سفارش پیدا نشد.")
        return
    await state.clear()
    await message.answer("سفارش پیدا شد:", reply_markup=main_menu(True))
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(Adm.filter(F.name == "ocard"))
async def cb_order_card(cb: CallbackQuery, callback_data: Adm, db: Database):
    text, kb = await _order_card(db, to_int(callback_data.arg) or 0)
    if text is None:
        await cb.answer("سفارش پیدا نشد.", show_alert=True)
        return
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "o_sync"))
async def cb_order_sync(cb: CallbackQuery, callback_data: Adm, db: Database, shop: Shop, bot: Bot):
    oid = to_int(callback_data.arg) or 0
    try:
        change = await shop.sync_order(oid)
    except StardError as e:
        await cb.answer(f"خطا: {e.code}", show_alert=True)
        return
    if change:
        await on_status_change(bot, shop, oid)
    text, kb = await _order_card(db, oid)
    if text:
        await edit_or_send(cb.message, text, kb)
    await cb.answer("به‌روز شد" if change else "تغییری نکرده")


@router.callback_query(Adm.filter(F.name == "o_done"))
async def cb_order_done(cb: CallbackQuery, callback_data: Adm, db: Database, shop: Shop, bot: Bot):
    oid = to_int(callback_data.arg) or 0
    if not await shop.complete_manual(oid):
        await cb.answer("این سفارش قبلاً بررسی شده است.", show_alert=True)
        await drop_markup(cb.message)
        return
    await on_status_change(bot, shop, oid)
    await drop_markup(cb.message)
    await cb.message.answer(f"✅ سفارش #{oid} انجام‌شده ثبت شد و به کاربر خبر داده شد.")
    await cb.answer("انجام شد")


@router.callback_query(Adm.filter(F.name == "o_refund"))
async def cb_order_refund(cb: CallbackQuery, callback_data: Adm, shop: Shop, bot: Bot):
    oid = to_int(callback_data.arg) or 0
    try:
        ok = await shop.admin_refund(oid, f"admin:{cb.from_user.id}")
    except ShopError as e:
        await cb.answer(str(e)[:190], show_alert=True)
        return
    except StardError as e:
        await cb.answer(f"خطا: {e.code}", show_alert=True)
        return
    if not ok:
        await cb.answer("این سفارش قبلاً بررسی شده یا انجام شده است.", show_alert=True)
        await drop_markup(cb.message)
        return
    await notify.order_changed(bot, shop.db, oid)
    await drop_markup(cb.message)
    await cb.message.answer(f"↩️ سفارش #{oid} لغو و مبلغ به کیف پول کاربر برگشت.")
    await cb.answer("انجام شد")


# ---------- بخش‌های فروشگاه ----------
async def _cats_view(shop: Shop):
    b = InlineKeyboardBuilder()
    for key, label in CATEGORIES.items():
        on = await shop.category_enabled(key)
        b.button(text=f"{'🟢' if on else '🔴'} {label}", callback_data=Adm(name="cat_t", arg=key))
    b.adjust(1)
    text = ("🗂 <b>بخش‌های فروشگاه</b>\n\nبا زدن هر دکمه، آن بخش روشن/خاموش می‌شود.\n"
            "❤️ ریکشن استارزی در Stard API نیست؛ سفارشش برای شما می‌آید تا دستی انجام دهید.")
    return text, back_admin(b)


@router.callback_query(Adm.filter(F.name == "cats"))
async def cb_cats(cb: CallbackQuery, shop: Shop):
    text, kb = await _cats_view(shop)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "cat_t"))
async def cb_cat_toggle(cb: CallbackQuery, callback_data: Adm, shop: Shop, db: Database):
    if callback_data.arg in CATEGORIES:
        on = await shop.category_enabled(callback_data.arg)
        await db.set_setting(f"cat:{callback_data.arg}", "0" if on else "1")
    text, kb = await _cats_view(shop)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("انجام شد")


# ---------- جوین اجباری ----------
async def _join_view(db: Database):
    chans = await db.get_json("force_join", [])
    on = (await db.get_setting("force_join_on", "0")) == "1"
    b = InlineKeyboardBuilder()
    b.button(text="🔴 خاموش کردن" if on else "🟢 روشن کردن", callback_data=Adm(name="join_t"))
    b.button(text="➕ افزودن کانال", callback_data=Adm(name="join_add"))
    for i, ch in enumerate(chans):
        b.button(text=f"🗑 {ch.get('title') or ch['chat']}", callback_data=Adm(name="join_rm", arg=str(i)))
    b.adjust(2, *([1] * len(chans)))
    lines = ["📣 <b>جوین اجباری</b>\n", f"وضعیت: {'🟢 روشن' if on else '🔴 خاموش'}",
             "کاربر تا عضو همه‌ی کانال‌ها نشود نمی‌تواند از ربات استفاده کند. ربات باید در هر کانال <b>ادمین</b> باشد.\n"]
    lines += [f"• {escape(ch.get('title') or '')} — <code>{escape(str(ch['chat']))}</code>" for ch in chans] or ["— کانالی ثبت نشده —"]
    return "\n".join(lines), back_admin(b)


@router.callback_query(Adm.filter(F.name == "join"))
async def cb_join(cb: CallbackQuery, db: Database):
    text, kb = await _join_view(db)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "join_t"))
async def cb_join_toggle(cb: CallbackQuery, db: Database, joins: JoinChecker):
    on = (await db.get_setting("force_join_on", "0")) == "1"
    if not on and not await db.get_json("force_join", []):
        await cb.answer("اول یک کانال اضافه کنید.", show_alert=True)
        return
    await db.set_setting("force_join_on", "0" if on else "1")
    joins.reset()
    text, kb = await _join_view(db)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("انجام شد")


@router.callback_query(Adm.filter(F.name == "join_rm"))
async def cb_join_remove(cb: CallbackQuery, callback_data: Adm, db: Database, joins: JoinChecker):
    chans = await db.get_json("force_join", [])
    i = to_int(callback_data.arg)
    if i is not None and 0 <= i < len(chans):
        chans.pop(i)
        await db.set_json("force_join", chans)
        if not chans:
            await db.set_setting("force_join_on", "0")
        joins.reset()
    text, kb = await _join_view(db)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("حذف شد")


@router.callback_query(Adm.filter(F.name == "join_add"))
async def cb_join_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.join_add)
    await cb.message.answer("📣 یوزرنیم کانال (مثل @mychannel)، آیدی عددی (مثل -1001234567890)، "
                            "یا یک پیام فورواردی از کانال را بفرستید.\nاول ربات را در کانال ادمین کنید.",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.join_add)
async def join_add(message: Message, state: FSMContext, db: Database, bot: Bot, joins: JoinChecker):
    fwd = getattr(message, "forward_from_chat", None)
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "chat", None) is not None:
        fwd = origin.chat
    ref: int | str | None = fwd.id if fwd else None
    if ref is None:
        t = (message.text or "").strip()
        n = to_int(t.lstrip("-"))
        if t.startswith("-") and n:
            ref = -n
        elif t:
            ref = "@" + t.replace("https://t.me/", "").replace("t.me/", "").lstrip("@").strip("/")
    if ref is None:
        await message.answer("❗️ ورودی نامعتبر است.")
        return
    try:
        chat = await bot.get_chat(ref)
        me = await bot.me()
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception as e:
        await message.answer(f"❗️ کانال پیدا نشد یا ربات عضو آن نیست: {escape(str(e))[:150]}")
        return
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer("❗️ ربات در این کانال ادمین نیست. اول ربات را ادمین کنید.")
        return
    url = f"https://t.me/{chat.username}" if chat.username else chat.invite_link
    if not url:
        try:
            url = await bot.export_chat_invite_link(chat.id)
        except Exception:
            await message.answer("❗️ لینک دعوت کانال خصوصی ساخته نشد؛ به ربات دسترسی «دعوت کاربران» بدهید.")
            return
    chans = await db.get_json("force_join", [])
    if any(c["chat"] == chat.id for c in chans):
        await message.answer("این کانال قبلاً اضافه شده است.")
        return
    chans.append({"chat": chat.id, "title": chat.title or chat.username or str(chat.id), "url": url})
    await db.set_json("force_join", chans)
    joins.reset()
    await state.clear()
    await message.answer(f"✅ کانال «{escape(chat.title or '')}» اضافه شد.", reply_markup=main_menu(True))
    text, kb = await _join_view(db)
    await message.answer(text, reply_markup=kb)


# ---------- کد تخفیف ----------
async def _coupons_view(db: Database):
    rows = await db.list_coupons()
    b = InlineKeyboardBuilder()
    b.button(text="➕ ساخت کد تخفیف", callback_data=Adm(name="cp_new"))
    for c in rows:
        b.button(text=f"🗑 {c['code']}", callback_data=Adm(name="cp_rm", arg=c["code"]))
    b.adjust(1, *([2] * ((len(rows) + 1) // 2)))
    lines = ["🎟 <b>کدهای تخفیف</b>\n", "تخفیف هیچ‌وقت قیمت را زیر قیمت خرید از Stard نمی‌برد. هر کاربر از هر کد یک بار.\n"]
    for c in rows:
        cap = f"{c['used']}/{c['max_uses']}" if c["max_uses"] else f"{c['used']}/∞"
        lines.append(f"• <code>{escape(c['code'])}</code> — {c['percent']:g}٪ | استفاده: {cap}")
    if not rows:
        lines.append("— کدی ساخته نشده —")
    return "\n".join(lines), back_admin(b)


@router.callback_query(Adm.filter(F.name == "coupons"))
async def cb_coupons(cb: CallbackQuery, db: Database):
    text, kb = await _coupons_view(db)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "cp_rm"))
async def cb_coupon_remove(cb: CallbackQuery, callback_data: Adm, db: Database):
    await db.delete_coupon(callback_data.arg)
    text, kb = await _coupons_view(db)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("حذف شد")


@router.callback_query(Adm.filter(F.name == "cp_new"))
async def cb_coupon_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.coupon)
    await cb.message.answer("🎟 به این شکل بفرستید:\n<code>کد درصد [حداکثر_استفاده]</code>\n\n"
                            "مثال: <code>YALDA 15 100</code> (۱۵٪، برای ۱۰۰ نفر)\n"
                            "مثال: <code>VIP 10</code> (۱۰٪، نامحدود)", reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.coupon)
async def coupon_create(message: Message, state: FSMContext, db: Database):
    parts = (message.text or "").split()
    code = parts[0].upper() if parts else ""
    pct = to_float(parts[1]) if len(parts) > 1 else None
    max_uses = to_int(parts[2]) if len(parts) > 2 else 0
    if not COUPON_RE.match(code) or pct is None or not 0 < pct <= 100 or max_uses is None:
        await message.answer("❗️ نامعتبر. کد ۳ تا ۳۲ حرف انگلیسی/عدد، درصد بین ۰ تا ۱۰۰. مثال: YALDA 15 100")
        return
    if not await db.create_coupon(code, pct, max_uses):
        await message.answer("❗️ این کد قبلاً ساخته شده است.")
        return
    await state.clear()
    await message.answer(f"✅ کد <code>{code}</code> با {pct:g}٪ تخفیف ساخته شد.", reply_markup=main_menu(True))
    text, kb = await _coupons_view(db)
    await message.answer(text, reply_markup=kb)


# ---------- زیرمجموعه‌گیری ----------
@router.callback_query(Adm.filter(F.name == "ref"))
async def cb_ref(cb: CallbackQuery, state: FSMContext, shop: Shop):
    await state.set_state(AdminForm.referral)
    pct = await shop.referral_percent()
    await cb.message.answer(
        f"🎁 <b>زیرمجموعه‌گیری</b>\n\nدرصد فعلی: <b>{pct:g}٪</b>\n"
        "هر کاربر لینک دعوت اختصاصی دارد. وقتی خرید زیرمجموعه انجام شود، این درصد از مبلغ خرید به کیف پول معرف "
        "اضافه می‌شود (هیچ‌وقت بیشتر از سود همان سفارش نمی‌شود).\n\n"
        "درصد جدید را بفرستید (0 = خاموش، حداکثر 50):", reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.referral)
async def ref_value(message: Message, state: FSMContext, db: Database):
    v = to_float(message.text)
    if v is None or not 0 <= v <= 50:
        await message.answer("❗️ یک عدد بین ۰ تا ۵۰ بفرستید.")
        return
    await db.set_setting("referral_percent", v)
    await state.clear()
    await message.answer(f"✅ پاداش زیرمجموعه روی {v:g}٪ تنظیم شد.", reply_markup=main_menu(True))


# ---------- قیمت در گروه ----------
@router.callback_query(Adm.filter(F.name == "group"))
async def cb_group(cb: CallbackQuery, callback_data: Adm, db: Database):
    if callback_data.arg == "t":
        on = (await db.get_setting("group_prices", "1")) == "1"
        await db.set_setting("group_prices", "0" if on else "1")
    on = (await db.get_setting("group_prices", "1")) == "1"
    b = InlineKeyboardBuilder()
    b.button(text="🔴 خاموش کردن" if on else "🟢 روشن کردن", callback_data=Adm(name="group", arg="t"))
    await edit_or_send(cb.message,
                       f"💹 <b>جواب قیمت در گروه</b>\n\nوضعیت: {'🟢 روشن' if on else '🔴 خاموش'}\n\n"
                       "ربات را به گروه اضافه کنید. با نوشتن «قیمت دلار»، «قیمت تون»، «قیمت استارز» یا «قیمت» "
                       "(یا دستور /price) قیمت لحظه‌ای را جواب می‌دهد.\n\n"
                       "⚠️ برای دیدن پیام‌های عادی گروه، در @BotFather گزینه‌ی "
                       "<b>Bot Settings → Group Privacy → Turn off</b> را بزنید، یا ربات را در گروه ادمین کنید.",
                       back_admin(b))
    await cb.answer()


# ---------- کیف پول Stard ----------
@router.callback_query(Adm.filter(F.name == "wallet"))
async def cb_wallet(cb: CallbackQuery, shop: Shop):
    try:
        w = await shop.api.wallet()
        ping = await shop.api.ping()
    except StardError as e:
        await cb.answer(f"خطا: {e.code}", show_alert=True)
        return
    bal = "\n".join(f"• {b['currency']}: <b>{b['amount']:,}</b>" if isinstance(b.get("amount"), int)
                    else f"• {b.get('currency')}: <b>{b.get('amount')}</b>" for b in w.get("balances", []))
    env = "🧪 test (پول آزمایشی)" if w.get("environment") == "test" else "🔴 live (پول واقعی)"
    status_line = ""
    with contextlib.suppress(StardError, KeyError, TypeError):
        st = await shop.api.status()
        bad = [k for k, v in (st.get("services") or {}).items() if v != "operational"]
        status_line = ("\n🟢 همه‌ی سرویس‌های Stard سالم‌اند" if not bad
                       else "\n⚠️ سرویس‌های مشکل‌دار: " + ", ".join(escape(x) for x in bad))
    tx_lines = ""
    with contextlib.suppress(StardError, KeyError, TypeError):
        txs = (await shop.api.transactions(limit=5)).get("data") or []
        if txs:
            items = []
            for t in txs:
                amt = t.get("amount")
                if isinstance(amt, dict):
                    amt = f"{amt.get('amount')} {amt.get('currency', '')}"
                items.append(f"• {escape(str(t.get('type') or t.get('kind') or ''))} {escape(str(amt))} "
                             f"| {escape(str(t.get('created_at') or '')[:16])}")
            tx_lines = "\n\n🧾 <b>تراکنش‌های اخیر API</b>\n" + "\n".join(items)
    key = ping.get("key") or {}
    await edit_or_send(
        cb.message,
        f"🏦 <b>کیف پول Stard API</b>\n\nمحیط: {env}\n{bal or '—'}\n"
        f"{escape(w.get('note') or '')}{status_line}\n\n🔑 کلید: <code>{escape(str(key.get('display', '')))}</code>\n"
        f"دسترسی‌ها: {escape(', '.join(key.get('scopes', [])))}{tx_lines}",
        back_admin())
    await cb.answer()


# ---------- پیام همگانی ----------
@router.callback_query(Adm.filter(F.name == "broadcast"))
async def cb_broadcast(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.broadcast)
    await cb.message.answer("📢 پیام (متن، عکس، ویدیو…) را بفرستید. قبل از ارسال یک بار تأیید می‌گیریم:",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.broadcast)
async def broadcast_preview(message: Message, state: FSMContext, db: Database):
    await state.update_data(bc_chat=message.chat.id, bc_msg=message.message_id)
    n = len(await db.all_user_ids())
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ ارسال برای {n:,} کاربر", callback_data=Adm(name="bc_go"))
    b.button(text="❌ انصراف", callback_data=Adm(name="home"))
    b.adjust(1)
    await message.answer("پیام بالا برای همه ارسال شود؟", reply_markup=b.as_markup())


@router.callback_query(Adm.filter(F.name == "bc_go"))
async def broadcast_go(cb: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    data = await state.get_data()
    if await state.get_state() != AdminForm.broadcast.state or "bc_msg" not in data:
        await cb.answer("پیامی برای ارسال نیست.", show_alert=True)
        return
    await state.clear()
    await cb.answer("شروع شد")
    await drop_markup(cb.message)
    ids = await db.all_user_ids()
    status = await cb.message.answer(f"⏳ ارسال برای {len(ids):,} کاربر…", reply_markup=main_menu(True))
    ok = fail = 0
    for i, uid in enumerate(ids, 1):
        for _ in range(3):
            try:
                await bot.copy_message(uid, data["bc_chat"], data["bc_msg"])
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except (TelegramForbiddenError, TelegramBadRequest, Exception):
                fail += 1
                break
        if i % 200 == 0:
            with contextlib.suppress(TelegramBadRequest):
                await status.edit_text(f"⏳ {i:,}/{len(ids):,} | ✅ {ok:,} | ❌ {fail:,}")
        await asyncio.sleep(0.05)  # زیر سقف ۳۰ پیام در ثانیه‌ی تلگرام
    with contextlib.suppress(TelegramBadRequest):
        await status.edit_text(f"📢 ارسال تمام شد.\n✅ موفق: {ok:,}\n❌ ناموفق (ربات را بلاک کرده‌اند): {fail:,}")


# ---------- تنظیمات ----------
@router.callback_query(Adm.filter(F.name == "settings"))
async def cb_settings(cb: CallbackQuery, db: Database):
    b = InlineKeyboardBuilder()
    lines = ["🛠 <b>تنظیمات</b>\n"]
    for key, label in SETTINGS.items():
        val = await db.get_setting(key)
        shown = escape(val[:60] + ("…" if len(val) > 60 else "")) if val else "— تنظیم نشده"
        lines.append(f"{label}: {shown}")
        b.button(text=f"✏️ {label}", callback_data=Adm(name="sset", arg=key))
    b.adjust(1)
    await edit_or_send(cb.message, "\n".join(lines), back_admin(b))
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "sset"))
async def cb_setting_edit(cb: CallbackQuery, callback_data: Adm, state: FSMContext):
    if callback_data.arg not in SETTINGS:
        await cb.answer()
        return
    await state.set_state(AdminForm.setting)
    await state.update_data(key=callback_data.arg)
    hint = SETTING_HINT.get(callback_data.arg, "")
    await cb.message.answer(f"مقدار جدید «{SETTINGS[callback_data.arg]}» را بفرستید:\n{hint}",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.setting, F.text)
async def setting_value(message: Message, state: FSMContext, db: Database, bot: Bot, shop: Shop):
    key = (await state.get_data()).get("key")
    if key not in SETTINGS:
        await state.clear()
        return
    value = message.text.strip()
    if key in ("log_channel", "welcome_text") and value in ("حذف", "delete", "-"):
        await db.del_setting(key)
        await state.clear()
        await message.answer(f"✅ «{SETTINGS[key]}» حذف شد.", reply_markup=main_menu(True))
        return
    if key == "min_topup":
        n = to_int(value)
        if n is None or n < 1000:
            await message.answer("❗️ یک عدد حداقل ۱۰۰۰ بفرستید.")
            return
        value = str(n)
    elif key == "round_to":
        n = to_int(value)
        if n not in (1, 10, 100, 1000, 10000):
            await message.answer("❗️ یکی از 1، 10، 100، 1000 یا 10000 بفرستید.")
            return
        value = str(n)
    elif key == "card_number":
        digits = "".join(str(to_int(ch)) for ch in value if to_int(ch) is not None)
        if len(digits) != 16:
            await message.answer("❗️ شماره کارت باید ۱۶ رقم باشد.")
            return
        value = "-".join(digits[i:i + 4] for i in range(0, 16, 4))
    elif key == "log_channel":
        n = to_int(value.lstrip("-"))
        target: int | str = -n if value.startswith("-") and n else "@" + value.lstrip("@")
        try:
            await bot.send_message(target, "✅ کانال گزارش ربات فروشگاه وصل شد.")
        except Exception as e:
            await message.answer(f"❗️ ارسال به این کانال ممکن نشد (ربات ادمین است؟): {escape(str(e))[:150]}")
            return
        value = str(target)
    await db.set_setting(key, value[:2000])
    await state.clear()
    await message.answer(f"✅ «{SETTINGS[key]}» ذخیره شد.", reply_markup=main_menu(True))


# ---------- پشتیبان ----------
@router.callback_query(Adm.filter(F.name == "backup"))
async def cb_backup(cb: CallbackQuery, db: Database, bot: Bot):
    await cb.answer("⏳ در حال ساخت پشتیبان…")
    fd, path = tempfile.mkstemp(suffix=".db", prefix="shop-backup-")
    os.close(fd)
    try:
        await db.backup(path)
        await bot.send_document(cb.from_user.id, FSInputFile(path, filename="shop-backup.db"),
                                caption="💾 پشتیبان کامل پایگاه داده. این فایل را جای امن نگه دارید.")
    except Exception as e:
        log.exception("backup failed")
        await cb.message.answer(f"❗️ پشتیبان‌گیری ناموفق: {escape(str(e))[:200]}")
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)


# ---------- مدیرها (فقط مالک) ----------
async def _admins_view(admins: Admins, db: Database):
    b = InlineKeyboardBuilder()
    b.button(text="➕ افزودن مدیر", callback_data=Adm(name="adm_add"))
    lines = ["👮 <b>مدیرها</b>\n", "مدیرها به همه‌ی بخش‌های پنل دسترسی دارند (جز این بخش).\n"]
    for uid in sorted(admins.owners):
        lines.append(f"👑 <code>{uid}</code> (مالک — از فایل .env)")
    for uid in sorted(admins.extra):
        u = await db.get_user(uid)
        name = f"@{u.username}" if u and u.username else (u.first_name if u else "")
        lines.append(f"👮 <code>{uid}</code> {escape(name or '')}")
        b.button(text=f"🗑 {uid}", callback_data=Adm(name="adm_rm", arg=str(uid)))
    b.adjust(1)
    return "\n".join(lines), back_admin(b)


@router.callback_query(Adm.filter(F.name == "admins"))
async def cb_admins(cb: CallbackQuery, admins: Admins, db: Database, is_owner: bool):
    if not is_owner:
        await cb.answer("فقط مالک ربات.", show_alert=True)
        return
    text, kb = await _admins_view(admins, db)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "adm_rm"))
async def cb_admin_remove(cb: CallbackQuery, callback_data: Adm, admins: Admins, db: Database, is_owner: bool):
    if not is_owner:
        await cb.answer("فقط مالک ربات.", show_alert=True)
        return
    await admins.remove(to_int(callback_data.arg) or 0)
    text, kb = await _admins_view(admins, db)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("حذف شد")


@router.callback_query(Adm.filter(F.name == "adm_add"))
async def cb_admin_add(cb: CallbackQuery, state: FSMContext, is_owner: bool):
    if not is_owner:
        await cb.answer("فقط مالک ربات.", show_alert=True)
        return
    await state.set_state(AdminForm.add_admin)
    await cb.message.answer("🆔 آیدی عددی یا یوزرنیم مدیر جدید را بفرستید (باید ربات را استارت کرده باشد):",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.add_admin)
async def admin_add(message: Message, state: FSMContext, admins: Admins, db: Database, bot: Bot, is_owner: bool):
    if not is_owner:
        await state.clear()
        return
    q = message.text or ""
    n = to_int(q)
    u = await db.find_user(str(n) if n is not None else q)
    if u is None:
        await message.answer("❗️ کاربر پیدا نشد. اول باید ربات را استارت کند.")
        return
    await state.clear()
    if await admins.add(u.id):
        await db.set_banned(u.id, False)
        await message.answer(f"✅ <code>{u.id}</code> مدیر شد.", reply_markup=main_menu(True))
        await notify.safe_send(bot, u.id, "👮 شما مدیر ربات شدید. /admin را بزنید.")
    else:
        await message.answer("این کاربر از قبل مدیر است.", reply_markup=main_menu(True))
    text, kb = await _admins_view(admins, db)
    await message.answer(text, reply_markup=kb)


# ---------- شبیه‌سازی (فقط test) ----------
@router.callback_query(Adm.filter(F.name == "sim"))
async def cb_sim(cb: CallbackQuery, db: Database, settings: Settings):
    if not settings.is_test:
        await cb.answer("فقط با کلید sk_test_", show_alert=True)
        return
    rows = [o for o in await db.active_orders() if o["stard_ref"]][:10]
    if not rows:
        await cb.answer("سفارش در جریانی نیست. (سفارش test بعد از ۱۵ ثانیه خودکار completed می‌شود)", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for o in rows:
        b.button(text=f"#{o['id']} ✅", callback_data=Adm(name="simdo", arg=f"{o['id']}:completed"))
        b.button(text=f"#{o['id']} ❌", callback_data=Adm(name="simdo", arg=f"{o['id']}:failed"))
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    b.adjust(2)
    await edit_or_send(cb.message, "🧪 نتیجه‌ی سفارش‌های آزمایشی را تعیین کنید:\n"
                                   "✅ = completed، ❌ = failed (پول کاربر برمی‌گردد)", b.as_markup())
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "simdo"))
async def cb_sim_do(cb: CallbackQuery, callback_data: Adm, db: Database, shop: Shop, settings: Settings):
    if not settings.is_test or ":" not in callback_data.arg:
        await cb.answer()
        return
    oid_s, outcome = callback_data.arg.split(":", 1)
    o = await db.get_order(to_int(oid_s) or 0)
    if o is None or not o["stard_ref"] or outcome not in ("completed", "failed"):
        await cb.answer("نامعتبر", show_alert=True)
        return
    try:
        await shop.api.simulate(o["stard_ref"], outcome)
    except StardError as e:
        await cb.answer(f"خطا: {e.code}", show_alert=True)
        return
    await cb.answer(f"#{o['id']} → {outcome}. ربات تا چند ثانیه‌ی دیگر وضعیت را به‌روز می‌کند.", show_alert=True)

