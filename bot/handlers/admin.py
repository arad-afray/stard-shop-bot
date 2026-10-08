"""پنل مدیریت: آمار، سود، شارژها، کاربران، سفارش‌ها، بخش‌ها، جوین اجباری، کد تخفیف،
زیرمجموعه‌گیری، قیمت در گروه، کیف پول Stard، پیام همگانی، تنظیمات، پشتیبان و مدیرها."""
from __future__ import annotations

import contextlib
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import group_prices as gp, notify, reports
from ..admins import Admins
from ..backups import BackupError, BackupManager
from ..config import Settings
from ..db import Database, InsufficientBalance
from ..middlewares import JoinChecker
from ..pricing import CATEGORIES, fmt_toman, to_float, to_int
from ..shop import COUPON_RE, Shop, ShopError
from ..stard_api import StardError
from ..ui import (BTN_ADMIN, STATUS_LABEL, Adm, admin_menu, back_admin, cancel_menu, drop_markup, edit_or_send,
                  main_menu, quick_actions, topup_review_menu)
from ..locks import RateLimiter, allow
from ..queue import JobQueue
from ..worker import on_status_change, start_broadcast
from .filters import IsAdmin

log = logging.getLogger(__name__)
router = Router(name="admin")


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
    group_words = State()
    group_test = State()


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


async def _home(shop: Shop, db: Database, settings: Settings, is_owner: bool, queue=None, services=None):
    """صفحه‌ی اصلی پنل = مرکز فرمان (STARD COMMAND CENTER) + دکمه‌های سریع + بخش‌ها."""
    from .ops import command_center_text
    s = await db.stats()
    maint, _ = await shop.maintenance()
    kb = admin_menu(await shop.is_open(), settings.is_test, is_owner, maintenance=maint,
                    pending_topups=s["pending_topups"], manual=s["manual"])
    quick = quick_actions()
    quick.attach(InlineKeyboardBuilder.from_markup(kb))
    return await command_center_text(db, shop, queue, settings, services), quick.as_markup()


@router.message(F.text == BTN_ADMIN)
@router.message(Command("admin", "panel"))
async def admin_home(message: Message, state: FSMContext, shop: Shop, db: Database, settings: Settings,
                     is_owner: bool, queue=None, services=None):
    await state.clear()
    text, kb = await _home(shop, db, settings, is_owner, queue, services)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Adm.filter(F.name == "home"))
async def cb_home(cb: CallbackQuery, state: FSMContext, shop: Shop, db: Database, settings: Settings, is_owner: bool,
                  queue=None, services=None):
    await state.clear()
    text, kb = await _home(shop, db, settings, is_owner, queue, services)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "toggle"))
async def cb_toggle(cb: CallbackQuery, db: Database, shop: Shop, settings: Settings, is_owner: bool,
                    queue=None, services=None):
    was = await shop.is_open()
    await db.set_setting("shop_open", "0" if was else "1")
    await db.audit(admin_id=cb.from_user.id, action="shop_open", after={"open": not was})
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
async def profit_value(message: Message, state: FSMContext, shop: Shop, db: Database):
    value = to_float(message.text)
    if value is None:
        await message.answer("❗️ یک عدد بفرستید، مثلاً 10")
        return
    cat = (await state.get_data()).get("cat", "global")
    try:
        old = await shop.get_profit(None if cat == "global" else cat)
        await shop.set_profit(value, None if cat == "global" else cat)
    except ShopError as e:
        await message.answer(f"❗️ {e}")
        return
    await db.audit(admin_id=message.from_user.id, action="profit_set", ref=cat, before={"percent": old},
                   after={"percent": value})
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
    """کارت کاربر با آمار کامل: سفارش‌ها (کل/موفق/ناموفق)، مجموع خرید، سود، موجودی، زیرمجموعه، آخرین فعالیت،
    تاریخ عضویت، VIP و امتیاز ریسک."""
    from ..commerce import Commerce
    from ..ui import SA
    u = await db.get_user(uid)
    if u is None:
        return "❗️ کاربر پیدا نشد.", back_admin()
    b = InlineKeyboardBuilder()
    b.button(text="➕ افزایش موجودی", callback_data=Adm(name="bal_add", arg=str(uid)))
    b.button(text="➖ کاهش موجودی", callback_data=Adm(name="bal_sub", arg=str(uid)))
    b.button(text="📦 تاریخچه‌ی خرید", callback_data=Adm(name="u_orders", arg=str(uid)))
    b.button(text="📒 تراکنش‌ها", callback_data=Adm(name="u_ledger", arg=str(uid)))
    b.button(text="🎁 پیشنهاد اختصاصی", callback_data=SA(a="uoffer", v=str(uid)))
    b.button(text="👑 VIP", callback_data=SA(a="uvip", v=str(uid)))
    b.button(text="✉️ پیام به کاربر", callback_data=Adm(name="u_msg", arg=str(uid)))
    b.button(text="♻️ صفر کردن ریسک", callback_data=SA(a="urisk", v=str(uid)))
    b.button(text="✅ رفع مسدودی" if u.banned else "🚫 مسدود کردن", callback_data=Adm(name="ban", arg=str(uid)))
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    b.adjust(2, 2, 2, 2, 1, 1)
    st = await db.user_order_stats(uid)
    level = await Commerce(db).user_level(uid)
    offers = [c for c in await db.list_coupons(user_id=uid) if c["active"]]
    ref = f"\n👤 معرف: <code>{u.referrer_id}</code>" if u.referrer_id else ""
    text = (f"👤 <b>{escape(u.first_name or '')}</b> {('@' + escape(u.username)) if u.username else ''}\n"
            f"🆔 <code>{u.id}</code> | زبان: {escape(u.language_code or '—')}\n"
            f"💰 موجودی: <b>{fmt_toman(u.balance)}</b>\n"
            f"📦 کل سفارش‌ها: {st['total']:,} | ✅ موفق: {st['ok']:,} | ❌ ناموفق: {st['failed']:,}\n"
            f"🛒 کل خرید: {fmt_toman(st['spent'])} | 📈 سود از این کاربر: {fmt_toman(st['profit'])}\n"
            f"👥 زیرمجموعه‌ها: {await db.count_referrals(uid):,} | 🎁 پاداش: {fmt_toman(await db.referral_earnings(uid))}{ref}\n"
            f"👑 VIP: {escape(level['name']) + (' (خودکار)' if level['source'] == 'auto' else '') if level else '—'}"
            f"{(' تا ' + level['ends_at'][:10]) if level and level.get('ends_at') else ''}\n"
            f"🎟 پیشنهادهای فعال: {', '.join(c['code'] for c in offers) or '—'}\n"
            f"🛡 امتیاز ریسک: {u.risk_score}{' | ⛔️ ' + escape(u.ban_reason or '') if u.banned else ''}\n"
            f"🕒 آخرین فعالیت: {(u.last_seen or '—')[:16].replace('T', ' ')} | آخرین سفارش: "
            f"{(st['last_order'] or '—')[:10]}\n"
            f"📅 تاریخ عضویت: {u.created_at[:10]} | وضعیت: {'🚫 مسدود' if u.banned else ('⛔️ ربات را بلاک کرده' if u.blocked else '✅ فعال')}")
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
    await db.set_banned(uid, not u.banned, admin_id=cb.from_user.id, reason="manual")
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
            new = await db.credit(uid, amount, "admin", f"by:{message.from_user.id}", admin_id=message.from_user.id)
        else:
            new = await db.debit(uid, amount, "admin", f"by:{message.from_user.id}", admin_id=message.from_user.id)
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
    if not await shop.complete_manual(oid, admin_id=cb.from_user.id):
        await cb.answer("این سفارش قبلاً بررسی شده است.", show_alert=True)
        await drop_markup(cb.message)
        return
    await on_status_change(bot, shop, oid)
    await drop_markup(cb.message)
    await cb.message.answer(f"✅ سفارش #{oid} انجام‌شده ثبت شد و به کاربر خبر داده شد.")
    await cb.answer("انجام شد")


@router.callback_query(Adm.filter(F.name == "o_refund"))
async def cb_order_refund(cb: CallbackQuery, callback_data: Adm, shop: Shop, bot: Bot,
                          limiter: RateLimiter | None = None):
    oid = to_int(callback_data.arg) or 0
    if not await allow(limiter, "admin_refund", cb.from_user.id):
        await cb.answer("⏳ سقف برگشت پول در دقیقه پر شده است.", show_alert=True)
        return
    try:
        ok = await shop.admin_refund(oid, f"admin:{cb.from_user.id}", admin_id=cb.from_user.id)
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


# ---------- بخش‌های فروشگاه (نسخه‌ی ۲) → مدیریت دکمه‌ها ----------
@router.callback_query(Adm.filter(F.name == "cats"))
async def cb_cats(cb: CallbackQuery, features=None, shop: Shop = None):
    from .admin_shop import buttons_view
    await buttons_view(cb, shop)


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
    await db.delete_coupon(callback_data.arg, admin_id=cb.from_user.id)
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
    if not await db.create_coupon(code, pct, max_uses, admin_id=message.from_user.id):
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
GP_COOLDOWNS = (0, 3, 10, 30, 60, 300)
GP_DELETES = (0, 1, 5, 15, 60)
GP_MODES = ("all", "allow", "deny")


def _on(v: bool) -> str:
    return "✅" if v else "⬜️"


async def _group_page(db: Database) -> tuple[str, InlineKeyboardBuilder]:
    from .prices import STATS
    cfg = await gp.get_config(db)
    known = await db.get_json("group_chats", {}) or {}
    k = cfg["kinds"]
    words = "، ".join(f"{gp.KIND_LABELS[x]}: {'، '.join(cfg['words'].get(x) or [])}" for x in gp.KINDS
                     if cfg["words"].get(x)) or "—"
    cd = int(cfg["cooldown"])
    dl = int(cfg["delete_after"])
    text = (
        "💹 <b>جواب قیمت در گروه</b>\n\n"
        f"وضعیت: {'🟢 روشن' if cfg['enabled'] else '🔴 خاموش'}\n\n"
        "<b>به چه سؤال‌هایی جواب بدهد</b>\n"
        + "\n".join(f"{_on(k.get(x, True))} {gp.KIND_LABELS[x]}" for x in (*gp.KINDS, "all")) + "\n"
        f"{_on(cfg['bare_word'])} یک کلمه‌ی تنها (مثل «تون» یا «استارز؟»)\n"
        f"{_on(cfg['amounts'])} سؤال با تعداد (مثل «10 تون»، «۵۰۰ استارز»، «100 دلار»)\n\n"
        "<b>رفتار</b>\n"
        f"{_on(cfg['buy_button'])} دکمه‌ی خرید زیر جواب\n"
        f"{_on(cfg['inline'])} حالت اینلاین («@ربات 10 تون» در هر چتی؛ یک بار در @BotFather دستور /setinline)\n"
        f"⏱ فاصله‌ی دو جواب یکسان در یک گروه: {cd} ثانیه\n"
        f"🗑 پاک شدن خودکار جواب: {f'بعد از {dl} دقیقه' if dl else 'هرگز'}\n"
        f"👥 گروه‌ها: {gp.MODE_LABELS[cfg['mode']]} ({len(known)} گروه شناخته‌شده)\n"
        f"✏️ کلمه‌های اضافه: {escape(words)}\n\n"
        f"📊 جواب‌ها از آخرین روشن شدن: دلار {STATS['usd']} | تون {STATS['ton']} | استارز {STATS['stars']} | "
        f"همه {STATS['all']} | با تعداد {STATS['amount']} | اینلاین {STATS.get('inline', 0)}\n\n"
        "⚠️ برای دیدن پیام‌های عادی گروه، در @BotFather گزینه‌ی "
        "<b>Bot Settings → Group Privacy → Turn off</b> (تنظیمات ربات ← حریم گروه ← خاموش) را بزنید، "
        "یا ربات را در گروه ادمین کنید."
    )
    b = InlineKeyboardBuilder()
    b.button(text="🔴 خاموش کردن کل قابلیت" if cfg["enabled"] else "🟢 روشن کردن", callback_data=Adm(name="group", arg="t"))
    for x in (*gp.KINDS, "all"):
        b.button(text=f"{_on(k.get(x, True))} {gp.KIND_LABELS[x].split(' (')[0].split(' («')[0]}",
                 callback_data=Adm(name="group", arg=f"k|{x}"))
    b.button(text=f"{_on(cfg['bare_word'])} کلمه‌ی تنها", callback_data=Adm(name="group", arg="bare"))
    b.button(text=f"{_on(cfg['amounts'])} با تعداد", callback_data=Adm(name="group", arg="amt"))
    b.button(text=f"{_on(cfg['buy_button'])} دکمه‌ی خرید", callback_data=Adm(name="group", arg="buy"))
    b.button(text=f"⏱ فاصله: {cd} ثانیه", callback_data=Adm(name="group", arg="cd"))
    b.button(text=f"🗑 پاک شدن: {f'{dl} دقیقه' if dl else 'هرگز'}", callback_data=Adm(name="group", arg="del"))
    b.button(text=f"{_on(cfg['inline'])} حالت اینلاین", callback_data=Adm(name="group", arg="inl"))
    b.button(text="👥 انتخاب گروه‌ها", callback_data=Adm(name="grp_chats"))
    b.button(text="✏️ کلمه‌های اضافه", callback_data=Adm(name="grp_words"))
    b.button(text="🧪 امتحان یک پیام", callback_data=Adm(name="grp_test"))
    b.adjust(1, 2, 2, 2, 2, 2, 2, 1)
    return text, b


@router.callback_query(Adm.filter(F.name == "group"))
async def cb_group(cb: CallbackQuery, callback_data: Adm, db: Database):
    arg = callback_data.arg
    if arg:
        cfg = await gp.get_config(db)
        if arg == "t":
            cfg["enabled"] = not cfg["enabled"]
        elif arg.startswith("k|") and arg[2:] in (*gp.KINDS, "all"):
            cfg["kinds"][arg[2:]] = not cfg["kinds"].get(arg[2:], True)
        elif arg in ("bare", "amt", "buy", "inl"):
            key = {"bare": "bare_word", "amt": "amounts", "buy": "buy_button", "inl": "inline"}[arg]
            cfg[key] = not cfg[key]
        elif arg == "cd":
            cur = int(cfg["cooldown"])
            cfg["cooldown"] = GP_COOLDOWNS[(GP_COOLDOWNS.index(cur) + 1) % len(GP_COOLDOWNS)] if cur in GP_COOLDOWNS else 3
        elif arg == "del":
            cur = int(cfg["delete_after"])
            cfg["delete_after"] = GP_DELETES[(GP_DELETES.index(cur) + 1) % len(GP_DELETES)] if cur in GP_DELETES else 0
        await gp.save_config(db, cfg)
        await db.audit(admin_id=cb.from_user.id, action="group_prices.update", after={"arg": arg})
    text, b = await _group_page(db)
    await edit_or_send(cb.message, text, back_admin(b))
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "grp_chats"))
async def cb_group_chats(cb: CallbackQuery, callback_data: Adm, db: Database):
    cfg = await gp.get_config(db)
    arg = callback_data.arg
    if arg == "mode":
        cfg["mode"] = GP_MODES[(GP_MODES.index(cfg["mode"]) + 1) % len(GP_MODES)]
        await gp.save_config(db, cfg)
    elif arg.lstrip("-").isdigit():
        chats = set(cfg["chats"])
        chats ^= {int(arg)}
        cfg["chats"] = sorted(chats)
        await gp.save_config(db, cfg)
    known = await db.get_json("group_chats", {}) or {}
    listed = set(cfg["chats"])
    b = InlineKeyboardBuilder()
    b.button(text=f"🔁 حالت: {gp.MODE_LABELS[cfg['mode']]}", callback_data=Adm(name="grp_chats", arg="mode"))
    for cid, title in sorted(known.items(), key=lambda kv: kv[1])[:40]:
        b.button(text=f"{_on(int(cid) in listed)} {title or cid}"[:40], callback_data=Adm(name="grp_chats", arg=cid))
    b.button(text="⬅️ قیمت در گروه", callback_data=Adm(name="group"))
    b.adjust(1)
    hint = {"all": "ربات در همه‌ی گروه‌ها جواب می‌دهد؛ تیک‌ها اثری ندارند.",
            "allow": "ربات فقط در گروه‌های تیک‌خورده ✅ جواب می‌دهد.",
            "deny": "ربات در همه‌ی گروه‌ها جواب می‌دهد، به جز گروه‌های تیک‌خورده ✅."}[cfg["mode"]]
    await edit_or_send(cb.message,
                       f"👥 <b>انتخاب گروه‌ها</b>\n\n{hint}\n\n"
                       + ("" if known else "هنوز گروهی ثبت نشده؛ ربات را به گروه اضافه کنید یا یک بار در گروه "
                                          "«قیمت» بنویسید.\n")
                       + "حالت را با دکمه‌ی بالا عوض کنید و روی هر گروه بزنید تا تیک بخورد.", b.as_markup())
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "grp_words"))
async def cb_group_words(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.group_words)
    await cb.message.answer(
        "✏️ کلمه‌های اضافه را هر کدام در یک خط بفرستید، مثلاً:\n"
        "<code>تون: تنکوین، تون کوین</code>\n<code>استارز: استار تلگرام</code>\n<code>دلار: دلار آمریکا</code>\n\n"
        "برای پاک کردن همه: <code>پاک</code>", reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.group_words, F.text)
async def group_words_value(message: Message, state: FSMContext, db: Database):
    cfg = await gp.get_config(db)
    names = {"دلار": "usd", "usd": "usd", "تون": "ton", "ton": "ton", "استارز": "stars", "stars": "stars"}
    if message.text.strip() == "پاک":
        cfg["words"] = {x: [] for x in gp.KINDS}
    else:
        words = {x: [] for x in gp.KINDS}
        for line in message.text.splitlines():
            if ":" not in line:
                continue
            head, rest = line.split(":", 1)
            kind = names.get(head.strip().lower())
            if not kind:
                await message.answer(f"❗️ «{escape(head.strip())}» شناخته نشد؛ فقط دلار، تون یا استارز.")
                return
            words[kind] += [w.strip() for w in rest.replace("،", ",").split(",") if w.strip()][:20]
        if not any(words.values()):
            await message.answer("❗️ قالب نادرست است. مثال: <code>تون: تنکوین، تون کوین</code>")
            return
        cfg["words"] = words
    await gp.save_config(db, cfg)
    await state.clear()
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))
    text, b = await _group_page(db)
    await message.answer(text, reply_markup=back_admin(b))


@router.callback_query(Adm.filter(F.name == "grp_test"))
async def cb_group_test(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.group_test)
    await cb.message.answer("🧪 یک پیام همان‌طور که کاربرها در گروه می‌نویسند بفرستید (مثلاً «10 تون چنده؟»)؛ "
                            "می‌بینید ربات چه جوابی می‌دهد.", reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.group_test, F.text)
async def group_test_value(message: Message, state: FSMContext, db: Database, shop: Shop):
    from .prices import answer_for
    cfg = await gp.get_config(db)
    q = gp.parse(message.text, cfg)
    if q is None:
        await message.answer("🤐 ربات به این پیام <b>جواب نمی‌دهد</b> (گفتگوی عادی تشخیص داده شد).\n"
                             "پیام دیگری بفرستید یا ❌ لغو.")
        return
    note = ""
    if not cfg["enabled"]:
        note = "\n⚠️ کل قابلیت خاموش است؛ الان در گروه جواب نمی‌دهد."
    elif not gp.kind_enabled(cfg, q):
        note = f"\n⚠️ جواب به {gp.KIND_LABELS[q.kind]} خاموش است؛ الان در گروه جواب نمی‌دهد."
    try:
        text, _ = await answer_for(shop, q)
    except StardError as e:
        text = f"⚠️ خطای API: {escape(e.code)}"
    amount = f" | تعداد: {gp.fmt_num(q.amount, 4)}" if q.amount is not None else ""
    await message.answer(f"✅ تشخیص: {gp.KIND_LABELS[q.kind]}{amount}{note}\n\n— جواب ربات —\n{text}\n\n"
                         "پیام دیگری بفرستید یا ❌ لغو.")


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
async def broadcast_go(cb: CallbackQuery, state: FSMContext, db: Database, queue: JobQueue,
                       limiter: RateLimiter | None = None):
    data = await state.get_data()
    if await state.get_state() != AdminForm.broadcast.state or "bc_msg" not in data:
        await cb.answer("پیامی برای ارسال نیست.", show_alert=True)
        return
    if not await allow(limiter, "broadcast", cb.from_user.id):
        await cb.answer("⏳ سقف پیام همگانی (۳ بار در ساعت) پر شده است.", show_alert=True)
        return
    await state.clear()
    await drop_markup(cb.message)
    segment = data.get("bc_segment", "all")
    bid = await start_broadcast(db, queue, admin_id=cb.from_user.id, from_chat=data["bc_chat"],
                                message_id=data["bc_msg"], segment=segment)
    await cb.answer("در صف ارسال قرار گرفت")
    await cb.message.answer(f"📢 پیام همگانی #{bid} در صف قرار گرفت و در پس‌زمینه ارسال می‌شود "
                            f"(حدود ۲۰ پیام در ثانیه). وضعیت: 🧾 سفارش‌ها/صف ← «📢 پیام‌های همگانی».\n"
                            "در پایان گزارش برایتان فرستاده می‌شود.", reply_markup=main_menu(True))


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
    b.button(text="📨 گزارش روزانه", callback_data=Adm(name="drep"))
    b.adjust(1)
    await edit_or_send(cb.message, "\n".join(lines), back_admin(b))
    await cb.answer()


# ---------- راهنمای پنل ----------
PANEL_HELP = (
    "📖 <b>راهنمای پنل مدیریت</b>\n\n"
    "📊 <b>آمار</b>: تعداد کاربران، سفارش‌ها و فروش.\n"
    "💵 <b>مالی</b>: درآمد، سود خالص، دفتر کل (همه‌ی تغییرات موجودی)، برگشت پول، پرداخت‌های ناموفق، "
    "خروجی اکسل/PDF و ماشین‌حساب کارمزد.\n"
    "👥 <b>کاربران</b>: جستجوی کاربر، افزایش/کاهش موجودی، مسدود کردن، سطح VIP.\n"
    "🧾 <b>سفارش‌ها</b>: سفارش‌های اخیر، استعلام از Stard، برگشت پول دستی.\n"
    "💳 <b>شارژها</b>: رسیدهای کارت‌به‌کارت که منتظر تأیید شما هستند.\n"
    "💰 <b>درصد سود</b>: سود شما روی قیمت Stard (کلی یا برای هر بخش).\n"
    "🛍 <b>مدیریت فروشگاه</b>: روشن/خاموش کردن بخش‌ها، ترتیب دکمه‌ها، حراج و قیمت‌گذاری، VIP، موجودی انبار، "
    "حالت آزمایشی.\n"
    "📣 <b>بازاریابی</b>: پیام همگانی، کد تخفیف، پیشنهاد اختصاصی، پاداش روزانه و گردونه.\n"
    "🛠 <b>سیستم</b>: سلامت ربات، پشتیبان‌گیری، به‌روزرسانی، لاگ‌ها، صف کارها، هشدارها و گزارش رویدادها.\n"
    "💹 <b>قیمت در گروه</b>: جواب خودکار به «قیمت»، «تون»، «10 تون»، «۵۰۰ استارز» در گروه‌ها و حالت اینلاین؛ "
    "هر نوع سؤال جدا روشن/خاموش می‌شود.\n"
    "⚙️ <b>تنظیمات</b>: متن‌ها، کارت بانکی، کانال گزارش، عضویت اجباری و گزارش روزانه.\n"
    "🧹 <b>حالت تعمیر</b>: فروش را موقتاً متوقف می‌کند و به کاربران پیام «در حال تعمیر» نشان می‌دهد.\n"
    "🔴/🟢 <b>بستن/باز کردن فروشگاه</b>: فقط فروش را متوقف یا شروع می‌کند.\n\n"
    "💡 <b>نکته‌ها</b>\n"
    "• پول کاربر هیچ‌وقت دو بار کم نمی‌شود؛ سفارش ناموفق خودکار برگشت می‌خورد.\n"
    "• قبل از هر به‌روزرسانی خودکار پشتیبان گرفته می‌شود و اگر مشکلی پیش بیاید نسخه‌ی قبلی برمی‌گردد.\n"
    "• برای امتحان بدون خرج پول واقعی: 🛍 مدیریت فروشگاه ← 🧪 حالت آزمایشی.\n"
    "• نسخه‌ی فعلی ربات پایین صفحه‌ی اصلی پنل نوشته شده است."
)


@router.callback_query(Adm.filter(F.name == "help"))
async def cb_panel_help(cb: CallbackQuery):
    await edit_or_send(cb.message, PANEL_HELP, back_admin(InlineKeyboardBuilder()))
    await cb.answer()


# ---------- گزارش روزانه ----------
@router.callback_query(Adm.filter(F.name == "drep"))
async def cb_daily_report(cb: CallbackQuery, callback_data: Adm, db: Database):
    cfg = await reports.get_config(db)
    if callback_data.arg == "toggle":
        cfg["enabled"] = not cfg["enabled"]
    elif callback_data.arg in ("h+", "h-"):
        cfg["hour"] = (int(cfg["hour"]) + (1 if callback_data.arg == "h+" else -1)) % 24
    elif callback_data.arg == "now":
        await cb.message.answer(await reports.build(db))
        await cb.answer()
        return
    if callback_data.arg:
        await db.set_json("daily_report", cfg)
        await db.audit(admin_id=cb.from_user.id, action="daily_report.update", after=cfg)
    b = InlineKeyboardBuilder()
    b.button(text="🔴 خاموش کردن" if cfg["enabled"] else "🟢 روشن کردن", callback_data=Adm(name="drep", arg="toggle"))
    b.button(text="➖ یک ساعت زودتر", callback_data=Adm(name="drep", arg="h-"))
    b.button(text="➕ یک ساعت دیرتر", callback_data=Adm(name="drep", arg="h+"))
    b.button(text="📨 همین الان گزارش امروز را بفرست", callback_data=Adm(name="drep", arg="now"))
    b.button(text="⬅️ تنظیمات", callback_data=Adm(name="settings"))
    b.adjust(1, 2, 1, 1)
    await edit_or_send(cb.message,
                       "📨 <b>گزارش روزانه</b>\n\n"
                       f"وضعیت: {'🟢 روشن' if cfg['enabled'] else '🔴 خاموش'}\n"
                       f"ساعت ارسال: <b>{int(cfg['hour']):02d}:00</b> به وقت تهران\n\n"
                       "هر روز در این ساعت خلاصه‌ی فروش، سود، سفارش‌های ناموفق، شارژها و کاربران جدید همان روز "
                       "برای همه‌ی مدیرها فرستاده می‌شود.", b.as_markup())
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
    old = await db.get_setting(key)
    await db.set_setting(key, value[:2000])
    await db.audit(admin_id=message.from_user.id, action="setting_set", ref=key,
                   before=None if key == "card_number" and old is None else {"value": old},
                   after={"value": value[:200]})
    await state.clear()
    await message.answer(f"✅ «{SETTINGS[key]}» ذخیره شد.", reply_markup=main_menu(True))


# ---------- پشتیبان ----------
@router.callback_query(Adm.filter(F.name == "backup"))
async def cb_backup(cb: CallbackQuery, db: Database, bot: Bot, settings: Settings, locks=None,
                    backups: BackupManager | None = None):
    await cb.answer("⏳ در حال ساخت پشتیبان…")
    mgr = backups or BackupManager(db, settings.backup_dir, settings, locks)
    try:
        info = await mgr.create("manual", admin_id=cb.from_user.id)
        check = mgr.verify(info.name)
        await bot.send_document(cb.from_user.id, FSInputFile(info.path, filename=info.name),
                                caption=f"💾 پشتیبان کامل ({info.rows:,} ردیف) — "
                                        f"{'✅ سالم' if check['ok'] else '❌ ' + escape(check['error'] or '')}\n"
                                        "این فایل را جای امن نگه دارید. secretها در پشتیبان نیستند.")
    except BackupError as e:
        await cb.message.answer(f"❗️ {escape(str(e))}")
    except Exception as e:
        log.exception("backup failed")
        await cb.message.answer(f"❗️ پشتیبان‌گیری ناموفق: {escape(type(e).__name__)}")


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
    await admins.remove(to_int(callback_data.arg) or 0, by=cb.from_user.id)
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
    if await admins.add(u.id, by=message.from_user.id):
        await db.set_banned(u.id, False, admin_id=message.from_user.id, reason="promoted to admin")
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
        b.button(text=f"#{o['id']} ✅", callback_data=Adm(name="simdo", arg=f"{o['id']}|completed"))
        b.button(text=f"#{o['id']} ❌", callback_data=Adm(name="simdo", arg=f"{o['id']}|failed"))
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    b.adjust(2)
    await edit_or_send(cb.message, "🧪 نتیجه‌ی سفارش‌های آزمایشی را تعیین کنید:\n"
                                   "✅ = completed، ❌ = failed (پول کاربر برمی‌گردد)", b.as_markup())
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "simdo"))
async def cb_sim_do(cb: CallbackQuery, callback_data: Adm, db: Database, shop: Shop, settings: Settings):
    if not settings.is_test or "|" not in callback_data.arg:
        await cb.answer()
        return
    oid_s, outcome = callback_data.arg.split("|", 1)
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

