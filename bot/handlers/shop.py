"""فروشگاه: انتخاب محصول ← گیرنده ← (پیام گیفت) ← تأیید ← پرداخت و ثبت خودکار."""
from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import asdict
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..db import Database, InsufficientBalance, User
from ..pricing import fmt_toman
from ..shop import Offer, Shop, ShopError, normalize_username
from ..stard_api import StardError
from ..ui import (BTN_SHOP, Act, GiftPage, Nav, PickProduct, StarsQty, confirm_menu, gifts_menu, main_menu,
                  premium_menu, recipient_menu, shop_menu, skip_message_menu, stars_menu)

log = logging.getLogger(__name__)
router = Router(name="shop")

# quote استارد ۲ دقیقه اعتبار دارد؛ کمی زودتر تازه‌اش می‌کنیم
QUOTE_TTL = 100

# کاربرانی که پرداختشان در جریان است. aiogram آپدیت‌ها را هم‌زمان پردازش می‌کند،
# پس بدون این، دو بار زدن سریع «تأیید» می‌تواند دو سفارش بسازد.
_paying: set[int] = set()


class Buy(StatesGroup):
    stars_qty = State()
    recipient = State()
    gift_message = State()
    confirm = State()


SHOP_TEXT = "🛍 <b>فروشگاه</b>\n\nدسته‌ی مورد نظر را انتخاب کنید:"
API_DOWN = "⚠️ ارتباط با سرور فروشگاه برقرار نشد. چند لحظه دیگر دوباره تلاش کنید."


async def _closed(shop: Shop, is_admin: bool) -> bool:
    return not is_admin and not await shop.is_open()


@router.message(F.text == BTN_SHOP)
async def open_shop(message: Message, state: FSMContext, shop: Shop, is_admin: bool):
    await state.clear()
    if await _closed(shop, is_admin):
        await message.answer("🔴 فروشگاه موقتاً بسته است. کمی بعد سر بزنید.")
        return
    await message.answer(SHOP_TEXT, reply_markup=shop_menu())


@router.callback_query(Nav.filter())
async def navigate(cb: CallbackQuery, callback_data: Nav, state: FSMContext, shop: Shop, is_admin: bool):
    await state.clear()
    if await _closed(shop, is_admin):
        await cb.answer("فروشگاه موقتاً بسته است.", show_alert=True)
        return
    try:
        if callback_data.to == "stars":
            await cb.message.edit_text(
                "⭐ <b>استارز تلگرام</b>\n\nتعداد را انتخاب کنید (حداقل ۵۰).\n"
                "استارز مستقیم به یوزرنیم گیرنده فرستاده می‌شود.", reply_markup=stars_menu())
        elif callback_data.to == "premium":
            plans = await shop.premium_plans()
            if not plans:
                await cb.answer("فعلاً پلن پریمیومی موجود نیست.", show_alert=True)
                return
            await cb.message.edit_text("💎 <b>تلگرام پریمیوم</b>\n\nپلن را انتخاب کنید:",
                                       reply_markup=premium_menu(plans))
        elif callback_data.to == "gifts":
            await _show_gifts(cb, shop, 0)
            return
        else:
            await cb.message.edit_text(SHOP_TEXT, reply_markup=shop_menu())
    except StardError as e:
        log.error("catalog error: %s", e)
        await cb.answer(API_DOWN, show_alert=True)
        return
    await cb.answer()


async def _show_gifts(cb: CallbackQuery, shop: Shop, page: int):
    try:
        gifts = await shop.gifts()
    except StardError as e:
        log.error("gifts error: %s", e)
        await cb.answer(API_DOWN, show_alert=True)
        return
    if not gifts:
        await cb.answer("فعلاً گیفتی موجود نیست.", show_alert=True)
        return
    await cb.message.edit_text("🎁 <b>گیفت استارزی</b>\n\nگیفت را انتخاب کنید؛ مستقیم برای گیرنده ارسال می‌شود:",
                               reply_markup=gifts_menu(gifts, page))
    await cb.answer()


@router.callback_query(GiftPage.filter())
async def gifts_page(cb: CallbackQuery, callback_data: GiftPage, shop: Shop):
    await _show_gifts(cb, shop, max(0, callback_data.page))


# ---------- انتخاب محصول ----------
@router.callback_query(StarsQty.filter())
async def stars_pick(cb: CallbackQuery, callback_data: StarsQty, state: FSMContext, shop: Shop, user: User):
    if callback_data.qty == 0:
        await state.set_state(Buy.stars_qty)
        await cb.message.answer("✍️ تعداد استارز را به عدد بفرستید (۵۰ تا ۱٬۰۰۰٬۰۰۰):")
        await cb.answer()
        return
    await cb.answer("⏳ در حال گرفتن قیمت…")
    await _start_offer(cb.message, state, shop, user, lambda: shop.stars_offer(callback_data.qty))


@router.message(Buy.stars_qty)
async def stars_custom(message: Message, state: FSMContext, shop: Shop, user: User):
    text = (message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    text = text.replace(",", "").replace("٬", "")
    if not text.isdigit():
        await message.answer("فقط عدد بفرستید. مثلاً: 750")
        return
    await _start_offer(message, state, shop, user, lambda: shop.stars_offer(int(text)))


@router.callback_query(PickProduct.filter())
async def product_pick(cb: CallbackQuery, callback_data: PickProduct, state: FSMContext, shop: Shop, user: User):
    if callback_data.cat not in ("premium", "star_gift"):
        await cb.answer()
        return
    await cb.answer("⏳ در حال گرفتن قیمت…")
    await _start_offer(cb.message, state, shop, user,
                       lambda: shop.product_offer(callback_data.pid, callback_data.cat))


async def _start_offer(message: Message, state: FSMContext, shop: Shop, user: User, make):
    try:
        offer: Offer = await make()
    except ShopError as e:
        await message.answer(f"⚠️ {e}")
        return
    except StardError as e:
        log.error("offer error: %s", e)
        await message.answer(API_DOWN if e.status in (0, 429) or e.status >= 500 else f"⚠️ {escape(e.message)}")
        return
    await state.set_state(Buy.recipient)
    await state.update_data(offer=asdict(offer), quoted_at=time.time())
    who = {"stars": "استارز", "premium": "پریمیوم", "star_gift": "گیفت"}[offer.category]
    await message.answer(
        f"🛒 <b>{escape(offer.title)}</b>\n💵 قیمت: <b>{fmt_toman(offer.price)}</b>\n\n"
        f"👤 یوزرنیم تلگرام گیرنده‌ی {who} را بفرستید (مثلاً @username):",
        reply_markup=recipient_menu(user.username))


# ---------- گیرنده ----------
@router.callback_query(Buy.recipient, Act.filter(F.name == "self"))
async def recipient_self(cb: CallbackQuery, state: FSMContext, user: User):
    await cb.answer()
    if not user.username:
        await cb.message.answer("شما یوزرنیم ندارید؛ یوزرنیم گیرنده را تایپ کنید.")
        return
    await _set_recipient(cb.message, state, "@" + user.username, user)


@router.message(Buy.recipient)
async def recipient_text(message: Message, state: FSMContext, user: User):
    username = normalize_username(message.text or "")
    if not username:
        await message.answer("❗️ یوزرنیم معتبر نیست. مثل @username بفرستید (۵ تا ۳۲ کاراکتر، فقط حروف انگلیسی، عدد و _).")
        return
    await _set_recipient(message, state, username, user)


async def _set_recipient(message: Message, state: FSMContext, username: str, user: User):
    await state.update_data(recipient=username)
    data = await state.get_data()
    if data["offer"]["category"] == "star_gift":
        await state.set_state(Buy.gift_message)
        await message.answer("💌 پیامی که روی گیفت نوشته شود را بفرستید (حداکثر ۲۰۰ کاراکتر):",
                             reply_markup=skip_message_menu())
        return
    await _show_confirm(message, state, user)


# ---------- پیام گیفت ----------
@router.callback_query(Buy.gift_message, Act.filter(F.name == "skipmsg"))
async def gift_skip(cb: CallbackQuery, state: FSMContext, user: User):
    await cb.answer()
    await state.update_data(gift_message=None)
    await _show_confirm(cb.message, state, user)


@router.message(Buy.gift_message)
async def gift_message(message: Message, state: FSMContext, user: User):
    text = (message.text or "").strip()
    if not text or len(text) > 200:
        await message.answer("پیام باید متنی و حداکثر ۲۰۰ کاراکتر باشد.")
        return
    await state.update_data(gift_message=text)
    await _show_confirm(message, state, user)


# ---------- تأیید و پرداخت ----------
async def _show_confirm(message: Message, state: FSMContext, user: User, note: str = ""):
    data = await state.get_data()
    o = data["offer"]
    await state.set_state(Buy.confirm)
    msg = f"\n💌 پیام: {escape(data['gift_message'])}" if data.get("gift_message") else ""
    enough = user.balance >= o["price"]
    warn = "" if enough else f"\n\n⚠️ موجودی کافی نیست؛ <b>{fmt_toman(o['price'] - user.balance)}</b> کم دارید. از «💳 شارژ کیف پول» شارژ کنید."
    await message.answer(
        f"{note}🧾 <b>پیش‌فاکتور</b>\n\n"
        f"📦 محصول: {escape(o['title'])}\n"
        f"👤 گیرنده: {escape(data['recipient'])}{msg}\n"
        f"💵 مبلغ: <b>{fmt_toman(o['price'])}</b>\n"
        f"💰 موجودی شما: {fmt_toman(user.balance)}{warn}\n\n"
        "در صورت تأیید، مبلغ از کیف پول کسر و سفارش خودکار انجام می‌شود.",
        reply_markup=confirm_menu())


@router.callback_query(Act.filter(F.name == "cancel"))
async def confirm_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    with contextlib.suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("❌ خرید لغو شد.")
    await cb.answer()


@router.callback_query(Buy.confirm, Act.filter(F.name == "confirm"))
async def confirm_pay(cb: CallbackQuery, state: FSMContext, shop: Shop, db: Database, user: User, is_admin: bool):
    # بررسی و افزودن بدون await در میان، پس اتمیک است
    if user.id in _paying:
        await cb.answer("⏳ در حال پردازش…")
        return
    _paying.add(user.id)
    try:
        await _confirm_pay(cb, state, shop, db, user, is_admin)
    finally:
        _paying.discard(user.id)


async def _confirm_pay(cb: CallbackQuery, state: FSMContext, shop: Shop, db: Database, user: User, is_admin: bool):
    if await state.get_state() != Buy.confirm.state:
        await cb.answer()
        return
    data = await state.get_data()
    offer = Offer(**data["offer"])
    if await _closed(shop, is_admin):
        await state.clear()
        await cb.answer("فروشگاه موقتاً بسته است.", show_alert=True)
        return
    await cb.answer("⏳ در حال پردازش…")
    with contextlib.suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=None)

    # قیمت قدیمی شده؟ پیش‌قیمت تازه بگیر و اگر گران‌تر شد دوباره تأیید بگیر
    if time.time() - data.get("quoted_at", 0) > QUOTE_TTL:
        try:
            fresh = await (shop.stars_offer(offer.quantity) if offer.type == "stars"
                           else shop.product_offer(offer.product_id, offer.category))
        except (ShopError, StardError) as e:
            await state.clear()
            await cb.message.answer(f"⚠️ {escape(str(e)) if isinstance(e, ShopError) else API_DOWN}")
            return
        await state.update_data(offer=asdict(fresh), quoted_at=time.time())
        if fresh.price > offer.price:
            await _show_confirm(cb.message, state, await db.get_user(user.id),
                                note=f"🔄 قیمت به‌روز شد: {fmt_toman(offer.price)} ← <b>{fmt_toman(fresh.price)}</b>\n\n")
            return
        offer = fresh

    await state.clear()
    try:
        oid = await shop.place_order(user.id, offer, data["recipient"], data.get("gift_message"))
    except InsufficientBalance:
        u = await db.get_user(user.id)
        await cb.message.answer(
            f"❌ موجودی کافی نیست.\n💵 مبلغ: {fmt_toman(offer.price)}\n💰 موجودی: {fmt_toman(u.balance)}\n\n"
            "از «💳 شارژ کیف پول» موجودی را افزایش دهید.", reply_markup=main_menu(is_admin))
        return
    except ShopError as e:
        await cb.message.answer(f"⚠️ {escape(str(e))}", reply_markup=main_menu(is_admin))
        return
    o = await db.get_order(oid)
    u = await db.get_user(user.id)
    await cb.message.answer(
        f"✅ <b>سفارش #{oid} ثبت شد</b>\n\n"
        f"📦 {escape(o['title'])} → {escape(o['recipient'] or '')}\n"
        f"💵 {fmt_toman(o['price'])} کسر شد | 💰 موجودی: {fmt_toman(u.balance)}\n\n"
        "سفارش خودکار انجام می‌شود و نتیجه را همین‌جا اطلاع می‌دهیم.",
        reply_markup=main_menu(is_admin))
