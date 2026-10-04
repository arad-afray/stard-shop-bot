"""پنل مدیریت مالک ربات."""
from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Settings
from ..db import Database, InsufficientBalance
from ..pricing import CATEGORIES, fmt_toman
from ..shop import Shop, ShopError
from ..stard_api import StardError
from ..ui import BTN_ADMIN, STATUS_LABEL, Adm, admin_menu, back_admin, cancel_menu, main_menu

log = logging.getLogger(__name__)
router = Router(name="admin")

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class IsAdmin(Filter):
    async def __call__(self, event: Any, is_admin: bool = False) -> bool:
        return is_admin


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminForm(StatesGroup):
    profit = State()
    find_user = State()
    balance = State()
    broadcast = State()
    setting = State()


SETTINGS = {
    "card_number": "💳 شماره کارت",
    "card_holder": "👤 نام صاحب کارت",
    "min_topup": "⬇️ حداقل شارژ (تومان)",
    "support_text": "🆘 متن پشتیبانی",
}


def _num(text: str | None) -> str:
    return (text or "").translate(PERSIAN_DIGITS).replace(",", "").replace("٬", "").strip()


async def _home_text(shop: Shop) -> str:
    return (f"⚙️ <b>پنل مدیریت</b>\n\nوضعیت فروشگاه: {'🟢 باز' if await shop.is_open() else '🔴 بسته'}\n"
            f"سود پیش‌فرض: {await shop.get_profit():g}%")


@router.message(F.text == BTN_ADMIN)
@router.message(Command("admin"))
async def admin_home(message: Message, state: FSMContext, shop: Shop, settings: Settings):
    await state.clear()
    await message.answer(await _home_text(shop), reply_markup=admin_menu(await shop.is_open(), settings.is_test))


@router.callback_query(Adm.filter(F.name == "home"))
async def cb_home(cb: CallbackQuery, state: FSMContext, shop: Shop, settings: Settings):
    await state.clear()
    await cb.message.edit_text(await _home_text(shop), reply_markup=admin_menu(await shop.is_open(), settings.is_test))
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "toggle"))
async def cb_toggle(cb: CallbackQuery, db: Database, shop: Shop, settings: Settings):
    await db.set_setting("shop_open", "0" if await shop.is_open() else "1")
    await cb.message.edit_text(await _home_text(shop), reply_markup=admin_menu(await shop.is_open(), settings.is_test))
    await cb.answer("انجام شد")


# ---------- آمار ----------
@router.callback_query(Adm.filter(F.name == "stats"))
async def cb_stats(cb: CallbackQuery, db: Database):
    s = await db.stats()
    await cb.message.edit_text(
        "📊 <b>آمار فروشگاه</b>\n\n"
        f"👥 کاربران: {s['users']:,}\n"
        f"💰 مجموع موجودی کاربران: {fmt_toman(s['balances'])}\n"
        f"✅ سفارش‌های انجام‌شده: {s['done']:,}\n"
        f"⏳ سفارش‌های در جریان: {s['active']:,}\n"
        f"↩️ سفارش‌های برگشتی: {s['refunded']:,}\n"
        f"💵 فروش کل: {fmt_toman(s['sales'])}\n"
        f"📈 سود خالص: <b>{fmt_toman(s['profit'])}</b>\n"
        f"💳 شارژهای در انتظار: {s['pending_topups']:,}",
        reply_markup=back_admin())
    await cb.answer()


# ---------- درصد سود ----------
async def _profit_view(shop: Shop):
    b = InlineKeyboardBuilder()
    lines = ["💰 <b>درصد سود</b>\n", "قیمت فروش = قیمت Stard × (۱ + درصد سود)، گرد به بالا تا ۱٬۰۰۰ تومان.\n",
             f"🌐 پیش‌فرض (همه‌ی دسته‌ها): <b>{await shop.get_profit():g}%</b>"]
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
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "pclr"))
async def cb_profit_clear(cb: CallbackQuery, callback_data: Adm, shop: Shop):
    if callback_data.arg in CATEGORIES:
        await shop.clear_category_profit(callback_data.arg)
    text, kb = await _profit_view(shop)
    await cb.message.edit_text(text, reply_markup=kb)
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
    try:
        value = float(_num(message.text).replace("%", "").replace("/", "."))
        cat = (await state.get_data())["cat"]
        await shop.set_profit(value, None if cat == "global" else cat)
    except ValueError:
        await message.answer("❗️ یک عدد بفرستید، مثلاً 10")
        return
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
    from ..ui import topup_review_menu
    rows = await db.pending_topups()
    if not rows:
        await cb.answer("درخواست شارژ در انتظاری نیست ✅", show_alert=True)
        return
    await cb.answer()
    for t in rows[:20]:
        await bot.send_photo(cb.from_user.id, t["photo_id"],
                             caption=f"💳 <b>درخواست شارژ #{t['id']}</b>\n🆔 <code>{t['user_id']}</code>\n"
                                     f"💵 مبلغ: <b>{fmt_toman(t['amount'])}</b>\n📅 {t['created_at']}",
                             reply_markup=topup_review_menu(t["id"]))


@router.callback_query(Adm.filter(F.name.in_({"tp_ok", "tp_no"})))
async def cb_topup_resolve(cb: CallbackQuery, callback_data: Adm, db: Database, bot: Bot):
    approve = callback_data.name == "tp_ok"
    row = await db.resolve_topup(int(callback_data.arg), cb.from_user.id, approve)
    if row is None:
        await cb.answer("این درخواست قبلاً بررسی شده است.", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=None)
        return
    verdict = "✅ تأیید شد" if approve else "❌ رد شد"
    await cb.message.edit_caption(caption=f"{cb.message.caption or ''}\n\n{verdict} توسط {cb.from_user.id}")
    user = await db.get_user(row["user_id"])
    try:
        if approve:
            await bot.send_message(row["user_id"], f"✅ شارژ {fmt_toman(row['amount'])} تأیید شد.\n"
                                                   f"💰 موجودی جدید: <b>{fmt_toman(user.balance)}</b>")
        else:
            await bot.send_message(row["user_id"], f"❌ درخواست شارژ #{row['id']} رد شد. برای پیگیری با پشتیبانی تماس بگیرید.")
    except Exception as e:
        log.warning("notify user %s failed: %s", row["user_id"], e)
    await cb.answer(verdict)


# ---------- مدیریت کاربر ----------
@router.callback_query(Adm.filter(F.name == "user"))
async def cb_user(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.find_user)
    await cb.message.answer("🆔 آیدی عددی یا یوزرنیم کاربر را بفرستید:", reply_markup=cancel_menu())
    await cb.answer()


async def _user_card(db: Database, uid: int):
    u = await db.get_user(uid)
    b = InlineKeyboardBuilder()
    b.button(text="➕ افزایش موجودی", callback_data=Adm(name="bal_add", arg=str(uid)))
    b.button(text="➖ کاهش موجودی", callback_data=Adm(name="bal_sub", arg=str(uid)))
    b.button(text="✅ رفع مسدودی" if u.banned else "🚫 مسدود کردن", callback_data=Adm(name="ban", arg=str(uid)))
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    b.adjust(2, 1, 1)
    orders = await db.count_user_orders(uid)
    text = (f"👤 <b>{escape(u.first_name or '')}</b> {('@' + escape(u.username)) if u.username else ''}\n"
            f"🆔 <code>{u.id}</code>\n💰 موجودی: <b>{fmt_toman(u.balance)}</b>\n📦 سفارش‌ها: {orders}\n"
            f"وضعیت: {'🚫 مسدود' if u.banned else '✅ فعال'}\n📅 عضویت: {u.created_at[:10]}")
    return text, b.as_markup()


@router.message(AdminForm.find_user)
async def find_user(message: Message, state: FSMContext, db: Database):
    u = await db.find_user(_num(message.text) if _num(message.text).isdigit() else (message.text or ""))
    if not u:
        await message.answer("❗️ کاربر پیدا نشد. کاربر باید حداقل یک بار ربات را استارت کرده باشد.")
        return
    await state.clear()
    await message.answer("کاربر پیدا شد:", reply_markup=main_menu(True))
    text, kb = await _user_card(db, u.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Adm.filter(F.name == "ban"))
async def cb_ban(cb: CallbackQuery, callback_data: Adm, db: Database):
    uid = int(callback_data.arg)
    u = await db.get_user(uid)
    await db.set_banned(uid, not u.banned)
    text, kb = await _user_card(db, uid)
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer("انجام شد")


@router.callback_query(Adm.filter(F.name.in_({"bal_add", "bal_sub"})))
async def cb_balance(cb: CallbackQuery, callback_data: Adm, state: FSMContext):
    await state.set_state(AdminForm.balance)
    await state.update_data(uid=int(callback_data.arg), sign=1 if callback_data.name == "bal_add" else -1)
    await cb.message.answer("💵 مبلغ (تومان) را بفرستید:", reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.balance)
async def balance_value(message: Message, state: FSMContext, db: Database, bot: Bot):
    text = _num(message.text)
    if not text.isdigit() or int(text) <= 0:
        await message.answer("❗️ یک عدد مثبت بفرستید.")
        return
    data = await state.get_data()
    amount, uid = int(text), data["uid"]
    try:
        if data["sign"] > 0:
            new = await db.credit(uid, amount, "admin", f"by:{message.from_user.id}")
        else:
            new = await db.debit(uid, amount, "admin", f"by:{message.from_user.id}")
    except InsufficientBalance:
        await message.answer("❗️ موجودی کاربر کمتر از این مبلغ است.")
        return
    await state.clear()
    sign = "+" if data["sign"] > 0 else "−"
    await message.answer(f"✅ {sign}{fmt_toman(amount)} | موجودی جدید: {fmt_toman(new)}", reply_markup=main_menu(True))
    try:
        await bot.send_message(uid, f"💰 موجودی شما توسط مدیر {'افزایش' if data['sign'] > 0 else 'کاهش'} یافت: "
                                    f"{sign}{fmt_toman(amount)}\nموجودی جدید: <b>{fmt_toman(new)}</b>")
    except Exception:
        pass
    text, kb = await _user_card(db, uid)
    await message.answer(text, reply_markup=kb)


# ---------- سفارش‌ها ----------
@router.callback_query(Adm.filter(F.name == "orders"))
async def cb_orders(cb: CallbackQuery, db: Database):
    rows = await db.recent_orders(15)
    if not rows:
        await cb.answer("هنوز سفارشی ثبت نشده.", show_alert=True)
        return
    lines = ["🧾 <b>۱۵ سفارش اخیر</b>\n"]
    for o in rows:
        lines.append(f"<b>#{o['id']}</b> 🆔{o['user_id']} | {escape(o['title'])} → {escape(o['recipient'] or '')}\n"
                     f"   {STATUS_LABEL.get(o['status'], o['status'])} | فروش {fmt_toman(o['price'])} | "
                     f"خرید {fmt_toman(o['base_amount'])}"
                     + (f"\n   ⚠️ {escape(o['failure_reason'])}" if o["failure_reason"] else ""))
    await cb.message.edit_text("\n".join(lines), reply_markup=back_admin())
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
    bal = "\n".join(f"• {b['currency']}: <b>{b['amount']:,}</b>" if isinstance(b["amount"], int)
                    else f"• {b['currency']}: <b>{b['amount']}</b>" for b in w.get("balances", []))
    env = "🧪 test (پول آزمایشی)" if w.get("environment") == "test" else "🔴 live (پول واقعی)"
    await cb.message.edit_text(
        f"🏦 <b>کیف پول Stard API</b>\n\nمحیط: {env}\n{bal or '—'}\n"
        f"{escape(w.get('note') or '')}\n\n🔑 کلید: <code>{escape(ping['key']['display'])}</code>\n"
        f"دسترسی‌ها: {', '.join(ping['key'].get('scopes', []))}",
        reply_markup=back_admin())
    await cb.answer()


# ---------- پیام همگانی ----------
@router.callback_query(Adm.filter(F.name == "broadcast"))
async def cb_broadcast(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.broadcast)
    await cb.message.answer("📢 پیام (متن، عکس، ویدیو…) را بفرستید تا برای همه‌ی کاربران ارسال شود:",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.broadcast)
async def broadcast(message: Message, state: FSMContext, db: Database, bot: Bot):
    await state.clear()
    ids = await db.all_user_ids()
    status = await message.answer(f"⏳ ارسال برای {len(ids):,} کاربر…", reply_markup=main_menu(True))
    ok = fail = 0
    for uid in ids:
        for _ in range(3):
            try:
                await bot.copy_message(uid, message.chat.id, message.message_id)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except (TelegramForbiddenError, Exception):
                fail += 1
                break
        await asyncio.sleep(0.05)  # زیر سقف ۳۰ پیام در ثانیه‌ی تلگرام
    await status.edit_text(f"📢 ارسال تمام شد.\n✅ موفق: {ok:,}\n❌ ناموفق: {fail:,}")


# ---------- تنظیمات ----------
@router.callback_query(Adm.filter(F.name == "settings"))
async def cb_settings(cb: CallbackQuery, db: Database):
    b = InlineKeyboardBuilder()
    lines = ["🛠 <b>تنظیمات</b>\n"]
    for key, label in SETTINGS.items():
        val = await db.get_setting(key)
        lines.append(f"{label}: {escape(val) if val else '— تنظیم نشده'}")
        b.button(text=f"✏️ {label}", callback_data=Adm(name="sset", arg=key))
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    b.adjust(1)
    await cb.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "sset"))
async def cb_setting_edit(cb: CallbackQuery, callback_data: Adm, state: FSMContext):
    if callback_data.arg not in SETTINGS:
        await cb.answer()
        return
    await state.set_state(AdminForm.setting)
    await state.update_data(key=callback_data.arg)
    await cb.message.answer(f"مقدار جدید «{SETTINGS[callback_data.arg]}» را بفرستید:", reply_markup=cancel_menu())
    await cb.answer()


@router.message(AdminForm.setting, F.text)
async def setting_value(message: Message, state: FSMContext, db: Database):
    key = (await state.get_data())["key"]
    value = message.text.strip()
    if key == "min_topup":
        value = _num(value)
        if not value.isdigit() or int(value) < 1000:
            await message.answer("❗️ یک عدد حداقل ۱۰۰۰ بفرستید.")
            return
    if key == "card_number":
        value = _num(value).replace(" ", "").replace("-", "")
        if not value.isdigit() or len(value) != 16:
            await message.answer("❗️ شماره کارت باید ۱۶ رقم باشد.")
            return
        value = "-".join(value[i:i + 4] for i in range(0, 16, 4))
    await db.set_setting(key, value[:1000])
    await state.clear()
    await message.answer(f"✅ «{SETTINGS[key]}» ذخیره شد.", reply_markup=main_menu(True))


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
    await cb.message.edit_text("🧪 نتیجه‌ی سفارش‌های آزمایشی را تعیین کنید:\n✅ = completed، ❌ = failed (پول کاربر برمی‌گردد)",
                               reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(Adm.filter(F.name == "simdo"))
async def cb_sim_do(cb: CallbackQuery, callback_data: Adm, db: Database, shop: Shop, settings: Settings):
    if not settings.is_test:
        await cb.answer()
        return
    oid_s, outcome = callback_data.arg.split(":", 1)
    o = await db.get_order(int(oid_s))
    if o is None or not o["stard_ref"] or outcome not in ("completed", "failed"):
        await cb.answer("نامعتبر", show_alert=True)
        return
    try:
        await shop.api.simulate(o["stard_ref"], outcome)
    except StardError as e:
        await cb.answer(f"خطا: {e.code}", show_alert=True)
        return
    await cb.answer(f"#{o['id']} → {outcome}. ربات تا چند ثانیه‌ی دیگر وضعیت را به‌روز می‌کند.", show_alert=True)
