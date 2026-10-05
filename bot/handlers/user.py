"""شروع، حساب کاربری، سفارش‌ها، قیمت لحظه‌ای، دعوت دوستان، پشتیبانی و انصراف."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..db import Database, User, now
from ..middlewares import JoinChecker
from ..pricing import CATEGORIES, fmt_toman
from ..shop import Shop
from ..stard_api import StardError
from ..ui import (BTN_ACCOUNT, BTN_CANCEL, BTN_ORDERS, BTN_PRICES, BTN_REFERRAL, BTN_SUPPORT, JOIN_CHECK,
                  STATUS_LABEL, edit_or_send, join_menu, main_menu)
from .prices import prices_text

log = logging.getLogger(__name__)
router = Router(name="user")

DEFAULT_SUPPORT = "برای پشتیبانی به مدیر ربات پیام دهید."
DEFAULT_WELCOME = ("به فروشگاه خوش آمدید!\n\n"
                   "⭐ استارز، 💎 پریمیوم، 🎁 گیفت، 🚀 بوست و ❤️ ریکشن استارزی تلگرام را با تحویل سریع بخرید.\n"
                   "اول کیف پول را شارژ کنید، بعد از بخش فروشگاه خرید کنید.")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, user: User, is_admin: bool, db: Database):
    await state.clear()
    name = escape(user.first_name or "دوست")
    welcome = await db.get_setting("welcome_text") or DEFAULT_WELCOME
    await message.answer(f"سلام {name} 👋\n{escape(welcome)}",
                         reply_markup=main_menu(is_admin))


@router.callback_query(F.data == JOIN_CHECK)
async def join_check(cb: CallbackQuery, bot: Bot, user: User, is_admin: bool, joins: JoinChecker):
    missing = [] if is_admin else await joins.missing(bot, user.id, use_cache=False)
    if missing:
        await cb.answer("❗️ هنوز در همه‌ی کانال‌ها عضو نشده‌اید.", show_alert=True)
        if cb.message:
            await edit_or_send(cb.message, "📣 برای استفاده از ربات، اول در این کانال‌ها عضو شوید:", join_menu(missing))
        return
    await cb.answer("✅ عضویت تأیید شد")
    if cb.message:
        await cb.message.answer("✅ عضویت شما تأیید شد. خوش آمدید!", reply_markup=main_menu(is_admin))


@router.message(F.text == BTN_CANCEL)
@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, is_admin: bool):
    await state.clear()
    await message.answer("لغو شد. به منوی اصلی برگشتید.", reply_markup=main_menu(is_admin))


@router.message(F.text == BTN_ACCOUNT)
@router.message(Command("me"))
async def account(message: Message, state: FSMContext, user: User, db: Database, shop: Shop):
    await state.clear()
    orders = await db.count_user_orders(user.id)
    spent = await db.user_spent(user.id)
    uname = f"@{escape(user.username)}" if user.username else "—"
    level = await shop.commerce.user_level(user.id)
    vip = (f"\n👑 سطح VIP: <b>{escape(level['name'])}</b> ({level['discount']:g}% تخفیف روی همه‌ی خریدها)"
           + (f" تا {level['ends_at'][:10]}" if level.get("ends_at") else "")) if level else ""
    offers = [c for c in await db.list_coupons(user_id=user.id)
              if c["active"] and (not c["expires_at"] or c["expires_at"] > now())
              and (not c["max_uses"] or c["used"] < c["max_uses"])]
    offer_text = ("\n\n🎁 <b>پیشنهادهای اختصاصی شما</b>\n" + "\n".join(
        f"• <code>{c['code']}</code> — {c['percent']:g}% تخفیف" + (f" ({CATEGORIES.get(c['category'], '')})" if c["category"] else "")
        + (f" تا {c['expires_at'][:10]}" if c["expires_at"] else "") for c in offers)) if offers else ""
    b = InlineKeyboardBuilder()
    if await shop.features.enabled("daily_reward", user.id):
        b.button(text="🎁 پاداش روزانه", callback_data=REWARD_DAILY)
    if await shop.features.enabled("spin", user.id):
        b.button(text="🎡 گردونه‌ی شانس", callback_data=REWARD_SPIN)
    b.adjust(2)
    await message.answer(
        "👤 <b>حساب کاربری</b>\n\n"
        f"🆔 شناسه: <code>{user.id}</code>\n"
        f"👤 یوزرنیم: {uname}\n"
        f"💰 موجودی: <b>{fmt_toman(user.balance)}</b>\n"
        f"📦 تعداد سفارش‌ها: {orders:,}\n"
        f"🛒 مجموع خرید: {fmt_toman(spent)}\n"
        f"👥 زیرمجموعه‌ها: {await db.count_referrals(user.id):,}\n"
        f"📅 عضویت: {user.created_at[:10]}{vip}{offer_text}",
        reply_markup=b.as_markup() if b.buttons else None)


REWARD_DAILY, REWARD_SPIN = "rw:daily", "rw:spin"


@router.callback_query(F.data.in_({REWARD_DAILY, REWARD_SPIN}))
async def claim(cb: CallbackQuery, db: Database, shop: Shop, user: User, limiter=None):
    from ..features import RewardError, claim_reward
    from ..locks import allow
    kind = cb.data.split(":", 1)[1]
    if not await shop.features.enabled("daily_reward" if kind == "daily" else "spin", user.id):
        await cb.answer("این بخش فعلاً فعال نیست.", show_alert=True)
        return
    if not await allow(limiter, "reward", user.id):
        await cb.answer("⏳ کمی صبر کنید.", show_alert=True)
        return
    try:
        amount = await claim_reward(db, user.id, kind)
    except RewardError as e:
        await cb.answer(str(e), show_alert=True)
        return
    bal = (await db.get_user(user.id)).balance
    if kind == "spin":
        text = f"🎡 گردونه چرخید… {'🎉 برنده‌ی ' + fmt_toman(amount) + ' شدید!' if amount else '😅 این بار پوچ بود؛ فردا دوباره!'}"
    else:
        text = f"🎁 پاداش روزانه: +{fmt_toman(amount)}"
    await cb.answer()
    await cb.message.answer(f"{text}\n💰 موجودی: <b>{fmt_toman(bal)}</b>")


@router.message(F.text == BTN_ORDERS)
@router.message(Command("orders"))
async def my_orders(message: Message, state: FSMContext, user: User, db: Database):
    await state.clear()
    rows = await db.user_orders(user.id, limit=10)
    if not rows:
        await message.answer("هنوز سفارشی ثبت نکرده‌اید. از «🛍 فروشگاه» شروع کنید.")
        return
    lines = ["📦 <b>۱۰ سفارش آخر شما</b>\n"]
    for o in rows:
        to = f" → {escape(o['recipient'])}" if o["recipient"] else ""
        lines.append(
            f"<b>#{o['id']}</b> {escape(o['title'])}{to}\n"
            f"   {STATUS_LABEL.get(o['status'], o['status'])} | {fmt_toman(o['price'])} | "
            f"{o['created_at'][:16].replace('T', ' ')}")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(F.text == BTN_PRICES)
@router.message(Command("price", "prices"))
async def prices(message: Message, state: FSMContext, shop: Shop):
    await state.clear()
    try:
        await message.answer(await prices_text(shop, "all"))
    except StardError as e:
        log.warning("prices failed: %s", e)
        await message.answer("⚠️ گرفتن قیمت ممکن نشد. چند لحظه دیگر دوباره تلاش کنید.")


@router.message(F.text == BTN_REFERRAL)
@router.message(Command("invite"))
async def referral(message: Message, state: FSMContext, user: User, db: Database, shop: Shop, bot: Bot):
    await state.clear()
    me = await bot.me()
    link = f"https://t.me/{me.username}?start=ref_{user.id}"
    if not await shop.features.enabled("referral", user.id):
        await message.answer("🎁 بخش دعوت دوستان فعلاً فعال نیست.")
        return
    pct = await shop.referral_percent()
    reward = (f"از هر خرید دوستانتان <b>{pct:g}٪</b> به کیف پول شما اضافه می‌شود. 🎁" if pct > 0
              else "فعلاً پاداشی برای دعوت تعیین نشده است.")
    await message.answer(
        "🎁 <b>دعوت دوستان</b>\n\n"
        f"{reward}\n\n"
        f"🔗 لینک دعوت شما:\n<code>{link}</code>\n\n"
        f"👥 زیرمجموعه‌ها: {await db.count_referrals(user.id):,}\n"
        f"💰 مجموع پاداش: {fmt_toman(await db.referral_earnings(user.id))}",
        disable_web_page_preview=True)


@router.message(F.text == BTN_SUPPORT)
@router.message(Command("support"))
async def support(message: Message, state: FSMContext, db: Database):
    await state.clear()
    text = await db.get_setting("support_text", DEFAULT_SUPPORT)
    await message.answer(f"🆘 <b>پشتیبانی</b>\n\n{escape(text)}")
