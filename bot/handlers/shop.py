"""فروشگاه: انتخاب محصول ← گیرنده ← (پیام گیفت) ← تأیید (با کد تخفیف) ← پرداخت و ثبت."""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import notify
from ..admins import Admins
from ..db import Database, InsufficientBalance, User
from ..locks import RateLimiter, allow
from ..pricing import fmt_toman, to_int
from ..shop import (REACTION_MAX, REACTION_MIN, STARS_MAX, STARS_MIN, Offer, Shop, ShopError, normalize_post_link,
                    normalize_username)
from ..stard_api import StardError
from ..ui import (BTN_SHOP, Act, BoostDur, BoostQty, GiftPage, Nav, PickProduct, ReactQty, StarsQty,
                  boost_duration_menu, boost_qty_menu, confirm_menu, drop_markup, edit_or_send, gifts_menu,
                  main_menu, premium_menu, reaction_menu, recipient_menu, shop_menu, skip_message_menu, stars_menu)

log = logging.getLogger(__name__)
router = Router(name="shop")

# quote استارد ۲ دقیقه اعتبار دارد؛ کمی زودتر تازه‌اش می‌کنیم
QUOTE_TTL = 100

# مسیر سریع داخل پردازه برای کلیک‌های پشت سر هم. محافظ اصلی در برابر خرید تکراری (حتی با چند نمونه‌ی
# ربات یا بعد از ری‌استارت) checkout_id یکتای هر پیش‌فاکتور است که در پایگاه داده UNIQUE است.
_paying: set[int] = set()


class Buy(StatesGroup):
    custom_qty = State()
    recipient = State()
    gift_message = State()
    confirm = State()
    coupon = State()


SHOP_TEXT = "🛍 <b>فروشگاه</b>\n\nدسته‌ی مورد نظر را انتخاب کنید:"
API_DOWN = "⚠️ ارتباط با سرور فروشگاه برقرار نشد. چند لحظه دیگر دوباره تلاش کنید."

RECIPIENT_PROMPT = {
    "stars": "👤 یوزرنیم تلگرام گیرنده‌ی استارز را بفرستید (مثلاً @username):",
    "premium": "👤 یوزرنیم تلگرام گیرنده‌ی پریمیوم را بفرستید (مثلاً @username):",
    "star_gift": "👤 یوزرنیم تلگرام گیرنده‌ی گیفت را بفرستید (مثلاً @username):",
    "boost": "📣 یوزرنیم یا لینک <b>کانال/گروه عمومی</b> را بفرستید (مثلاً @mychannel):",
    "reaction": "🔗 لینک <b>پستی</b> که ریکشن استارزی روی آن زده شود را بفرستید\n(مثلاً https://t.me/mychannel/123):",
}


async def _closed(shop: Shop, is_admin: bool) -> bool:
    return not is_admin and not await shop.is_open()


@router.message(F.text == BTN_SHOP)
@router.message(F.text == "/shop")
async def open_shop(message: Message, state: FSMContext, shop: Shop, is_admin: bool):
    await state.clear()
    if await _closed(shop, is_admin):
        await message.answer("🔴 فروشگاه موقتاً بسته است. کمی بعد سر بزنید.")
        return
    enabled = await shop.enabled_categories()
    if not enabled:
        await message.answer("فعلاً محصولی برای فروش فعال نیست.")
        return
    await message.answer(SHOP_TEXT, reply_markup=shop_menu(enabled))


@router.callback_query(Nav.filter())
async def navigate(cb: CallbackQuery, callback_data: Nav, state: FSMContext, shop: Shop, is_admin: bool):
    await state.clear()
    if await _closed(shop, is_admin):
        await cb.answer("فروشگاه موقتاً بسته است.", show_alert=True)
        return
    to = callback_data.to
    cat = {"gifts": "star_gift"}.get(to, to)
    if to != "shop" and not await shop.category_enabled(cat):
        await cb.answer("این بخش فعلاً غیرفعال است.", show_alert=True)
        return
    try:
        if to == "stars":
            await edit_or_send(cb.message,
                               "⭐ <b>استارز تلگرام</b>\n\nتعداد را انتخاب کنید (حداقل ۵۰).\n"
                               "استارز مستقیم و خودکار به یوزرنیم گیرنده فرستاده می‌شود.", stars_menu())
        elif to == "premium":
            plans = await shop.premium_plans()
            if not plans:
                await cb.answer("فعلاً پلن پریمیومی موجود نیست.", show_alert=True)
                return
            await edit_or_send(cb.message, "💎 <b>تلگرام پریمیوم</b>\n\nپلن را انتخاب کنید:", premium_menu(plans))
        elif to == "gifts":
            await _show_gifts(cb, shop, 0)
            return
        elif to == "boost":
            cat_info = await shop.boost_catalog()
            if not cat_info["enabled"]:
                await cb.answer("فعلاً بوستی موجود نیست.", show_alert=True)
                return
            await edit_or_send(cb.message,
                               "🚀 <b>بوست کانال و گروه</b>\n\nبوست سطح کانال/گروه را بالا می‌برد "
                               "(استوری، ری‌اکشن سفارشی، رنگ و …).\nمدت بوست را انتخاب کنید:",
                               boost_duration_menu(cat_info["durations"]))
        elif to == "reaction":
            unit = await shop.sell("reaction", int((await shop.rates())["star"]["amount"]) * 100)
            await edit_or_send(cb.message,
                               "❤️ <b>ریکشن استارزی (Paid Reaction)</b>\n\n"
                               "ریکشن ⭐ روی پست کانال شما زده می‌شود.\n"
                               f"💵 هر ۱۰۰ ریکشن حدود {fmt_toman(unit)}\n\nتعداد را انتخاب کنید:", reaction_menu())
        else:
            await edit_or_send(cb.message, SHOP_TEXT, shop_menu(await shop.enabled_categories()))
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
    page = min(page, (len(gifts) - 1) // 8)
    await edit_or_send(cb.message, "🎁 <b>گیفت استارزی</b>\n\nگیفت را انتخاب کنید؛ مستقیم برای گیرنده ارسال می‌شود:",
                       gifts_menu(gifts, page))
    await cb.answer()


@router.callback_query(GiftPage.filter())
async def gifts_page(cb: CallbackQuery, callback_data: GiftPage, shop: Shop):
    await _show_gifts(cb, shop, max(0, callback_data.page))


# ---------- انتخاب محصول ----------
async def _ask_custom(cb: CallbackQuery, state: FSMContext, kind: str, hint: str, **extra):
    await state.set_state(Buy.custom_qty)
    await state.update_data(kind=kind, **extra)
    await cb.message.answer(f"✍️ تعداد را به عدد بفرستید ({hint}):")
    await cb.answer()


@router.callback_query(StarsQty.filter())
async def stars_pick(cb: CallbackQuery, callback_data: StarsQty, state: FSMContext, shop: Shop, user: User):
    if callback_data.qty == 0:
        await _ask_custom(cb, state, "stars", f"{STARS_MIN:,} تا {STARS_MAX:,}")
        return
    await cb.answer("⏳ در حال گرفتن قیمت…")
    await _start_offer(cb.message, state, shop, user, lambda: shop.stars_offer(callback_data.qty))


@router.callback_query(ReactQty.filter())
async def reaction_pick(cb: CallbackQuery, callback_data: ReactQty, state: FSMContext, shop: Shop, user: User):
    if callback_data.qty == 0:
        await _ask_custom(cb, state, "reaction", f"{REACTION_MIN:,} تا {REACTION_MAX:,}")
        return
    await cb.answer("⏳ در حال گرفتن قیمت…")
    await _start_offer(cb.message, state, shop, user, lambda: shop.reaction_offer(callback_data.qty))


@router.callback_query(BoostDur.filter())
async def boost_duration(cb: CallbackQuery, callback_data: BoostDur, shop: Shop):
    try:
        cat = await shop.boost_catalog()
    except StardError:
        await cb.answer(API_DOWN, show_alert=True)
        return
    d = next((x for x in cat["durations"] if int(x["duration"]) == callback_data.days), None)
    if d is None:
        await cb.answer("این مدت فعلاً موجود نیست.", show_alert=True)
        return
    await edit_or_send(cb.message,
                       f"🚀 <b>بوست {escape(d.get('label') or str(callback_data.days))}</b>\n"
                       f"💵 هر بوست: {fmt_toman(d['sell_per_boost'])}\n\n"
                       f"تعداد بوست را انتخاب کنید (حداکثر {cat['max_quantity']:,}):",
                       boost_qty_menu(callback_data.days, cat["max_quantity"]))
    await cb.answer()


@router.callback_query(BoostQty.filter())
async def boost_pick(cb: CallbackQuery, callback_data: BoostQty, state: FSMContext, shop: Shop, user: User):
    if callback_data.qty == 0:
        await _ask_custom(cb, state, "boost", "مثلاً 20", days=callback_data.days)
        return
    await cb.answer("⏳ در حال گرفتن قیمت…")
    await _start_offer(cb.message, state, shop, user,
                       lambda: shop.boost_offer(callback_data.qty, callback_data.days))


@router.message(Buy.custom_qty)
async def custom_qty(message: Message, state: FSMContext, shop: Shop, user: User):
    qty = to_int(message.text)
    if qty is None or qty <= 0:
        await message.answer("فقط عدد بفرستید. مثلاً: 750")
        return
    data = await state.get_data()
    kind = data.get("kind")
    if kind == "reaction":
        make = lambda: shop.reaction_offer(qty)  # noqa: E731
    elif kind == "boost":
        make = lambda: shop.boost_offer(qty, int(data["days"]))  # noqa: E731
    else:
        make = lambda: shop.stars_offer(qty)  # noqa: E731
    await _start_offer(message, state, shop, user, make)


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
        await message.answer(f"⚠️ {escape(str(e))}")
        return
    except StardError as e:
        log.error("offer error: %s", e)
        await message.answer(API_DOWN if e.status in (0, 429) or e.status >= 500 else f"⚠️ {escape(e.message)}")
        return
    await state.clear()
    await state.set_state(Buy.recipient)
    await state.update_data(offer=asdict(offer), quoted_at=time.time())
    self_btn = recipient_menu(user.username) if offer.category in ("stars", "premium", "star_gift") else None
    await message.answer(
        f"🛒 <b>{escape(offer.title)}</b>\n💵 قیمت: <b>{fmt_toman(offer.price)}</b>\n\n"
        + RECIPIENT_PROMPT[offer.category], reply_markup=self_btn)


# ---------- گیرنده ----------
@router.callback_query(Buy.recipient, Act.filter(F.name == "self"))
async def recipient_self(cb: CallbackQuery, state: FSMContext, user: User):
    await cb.answer()
    data = await state.get_data()
    if not user.username or data["offer"]["category"] not in ("stars", "premium", "star_gift"):
        await cb.message.answer("گیرنده را تایپ کنید.")
        return
    await _set_recipient(cb.message, state, "@" + user.username, user)


@router.message(Buy.recipient)
async def recipient_text(message: Message, state: FSMContext, user: User):
    cat = (await state.get_data())["offer"]["category"]
    text = message.text or ""
    if cat == "reaction":
        value = normalize_post_link(text)
        error = "❗️ لینک پست معتبر نیست. مثل https://t.me/mychannel/123 بفرستید."
    else:
        value = normalize_username(text)
        error = ("❗️ کانال/گروه معتبر نیست. یوزرنیم کانال یا گروه <b>عمومی</b> را مثل @mychannel بفرستید."
                 if cat == "boost" else
                 "❗️ یوزرنیم معتبر نیست. مثل @username بفرستید (۵ تا ۳۲ کاراکتر، فقط حروف انگلیسی، عدد و _).")
    if not value:
        await message.answer(error)
        return
    await _set_recipient(message, state, value, user)


async def _set_recipient(message: Message, state: FSMContext, recipient: str, user: User):
    await state.update_data(recipient=recipient)
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
    if not data.get("checkout_id"):
        await state.update_data(checkout_id=uuid.uuid4().hex)
    await state.set_state(Buy.confirm)
    price = data.get("final_price") or o["price"]
    msg = f"\n💌 پیام: {escape(data['gift_message'])}" if data.get("gift_message") else ""
    disc = ""
    if data.get("coupon"):
        disc = (f"\n🎟 کد تخفیف <code>{escape(data['coupon'])}</code>: "
                f"<s>{fmt_toman(o['price'])}</s> ← −{fmt_toman(data['discount'])}")
    enough = user.balance >= price
    warn = "" if enough else (f"\n\n⚠️ موجودی کافی نیست؛ <b>{fmt_toman(price - user.balance)}</b> کم دارید. "
                              "از «💳 شارژ کیف پول» شارژ کنید.")
    manual = "\n🧑‍💻 این سفارش توسط پشتیبانی انجام می‌شود." if o["category"] == "reaction" else \
        "\nدر صورت تأیید، مبلغ از کیف پول کسر و سفارش خودکار انجام می‌شود."
    await message.answer(
        f"{note}🧾 <b>پیش‌فاکتور</b>\n\n"
        f"📦 محصول: {escape(o['title'])}\n"
        f"👤 گیرنده: {escape(data['recipient'])}{msg}{disc}\n"
        f"💵 مبلغ قابل پرداخت: <b>{fmt_toman(price)}</b>\n"
        f"💰 موجودی شما: {fmt_toman(user.balance)}{warn}\n{manual}",
        reply_markup=confirm_menu(bool(data.get("coupon"))), disable_web_page_preview=True)


@router.callback_query(Act.filter(F.name == "cancel"))
async def confirm_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await drop_markup(cb.message)
    await cb.message.answer("❌ خرید لغو شد.")
    await cb.answer()


@router.callback_query(Buy.confirm, Act.filter(F.name == "coupon"))
async def coupon_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Buy.coupon)
    await cb.message.answer("🎟 کد تخفیف را بفرستید:")
    await cb.answer()


@router.callback_query(Buy.confirm, Act.filter(F.name == "nocoupon"))
async def coupon_remove(cb: CallbackQuery, state: FSMContext, db: Database, user: User):
    await state.update_data(coupon=None, discount=0, final_price=None)
    await drop_markup(cb.message)
    await cb.answer("کد تخفیف حذف شد")
    await _show_confirm(cb.message, state, await db.get_user(user.id))


@router.message(Buy.coupon)
async def coupon_value(message: Message, state: FSMContext, shop: Shop, db: Database, user: User,
                       limiter: RateLimiter | None = None):
    data = await state.get_data()
    if not await allow(limiter, "coupon", user.id):
        await state.set_state(Buy.confirm)
        await message.answer("⏳ تلاش‌های کد تخفیف زیاد بود؛ چند دقیقه‌ی دیگر امتحان کنید.")
        return
    offer = Offer(**data["offer"])
    code = (message.text or "").strip().upper()
    try:
        final, off = await shop.check_coupon(code, user.id, offer)
    except ShopError as e:
        await state.set_state(Buy.confirm)
        await message.answer(f"❗️ {escape(str(e))}")
        await _show_confirm(message, state, await db.get_user(user.id))
        return
    await state.update_data(coupon=code, discount=off, final_price=final)
    await _show_confirm(message, state, await db.get_user(user.id), note="✅ کد تخفیف اعمال شد.\n\n")


@router.callback_query(Buy.confirm, Act.filter(F.name == "confirm"))
async def confirm_pay(cb: CallbackQuery, state: FSMContext, shop: Shop, db: Database, user: User, is_admin: bool,
                      bot: Bot, admins: Admins, limiter: RateLimiter | None = None):
    # بررسی و افزودن بدون await در میان، پس اتمیک است
    if user.id in _paying:
        await cb.answer("⏳ در حال پردازش…")
        return
    if not await allow(limiter, "purchase", user.id):
        await cb.answer("⏳ تعداد خریدها در این دقیقه زیاد است؛ کمی صبر کنید.", show_alert=True)
        return
    _paying.add(user.id)
    try:
        await _confirm_pay(cb, state, shop, db, user, is_admin, bot, admins)
    finally:
        _paying.discard(user.id)


async def _refresh_offer(shop: Shop, offer: Offer) -> Offer:
    if offer.type == "stars":
        return await shop.stars_offer(offer.quantity)
    if offer.type == "boost":
        return await shop.boost_offer(offer.quantity, offer.duration)
    if offer.type == "reaction":
        return await shop.reaction_offer(offer.quantity)
    return await shop.product_offer(offer.product_id, offer.category)


async def _confirm_pay(cb: CallbackQuery, state: FSMContext, shop: Shop, db: Database, user: User, is_admin: bool,
                       bot: Bot, admins: Admins):
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
    await drop_markup(cb.message)

    # قیمت قدیمی شده؟ پیش‌قیمت تازه بگیر و اگر گران‌تر شد دوباره تأیید بگیر
    if time.time() - data.get("quoted_at", 0) > QUOTE_TTL:
        try:
            fresh = await _refresh_offer(shop, offer)
        except (ShopError, StardError) as e:
            await state.clear()
            await cb.message.answer(f"⚠️ {escape(str(e)) if isinstance(e, ShopError) else API_DOWN}",
                                    reply_markup=main_menu(is_admin))
            return
        await state.update_data(offer=asdict(fresh), quoted_at=time.time())
        if fresh.price > offer.price:
            if data.get("coupon"):
                try:
                    final, off = await shop.check_coupon(data["coupon"], user.id, fresh)
                    await state.update_data(discount=off, final_price=final)
                except ShopError:
                    await state.update_data(coupon=None, discount=0, final_price=None)
            await _show_confirm(cb.message, state, await db.get_user(user.id),
                                note=f"🔄 قیمت به‌روز شد: {fmt_toman(offer.price)} ← <b>{fmt_toman(fresh.price)}</b>\n\n")
            return
        offer = fresh

    await state.clear()
    try:
        oid = await shop.place_order(user.id, offer, data["recipient"], data.get("gift_message"), data.get("coupon"),
                                     checkout_id=data.get("checkout_id"))
    except InsufficientBalance:
        u = await db.get_user(user.id)
        await cb.message.answer(
            f"❌ موجودی کافی نیست.\n💵 مبلغ: {fmt_toman(data.get('final_price') or offer.price)}\n"
            f"💰 موجودی: {fmt_toman(u.balance)}\n\nاز «💳 شارژ کیف پول» موجودی را افزایش دهید.",
            reply_markup=main_menu(is_admin))
        return
    except ShopError as e:
        await cb.message.answer(f"⚠️ {escape(str(e))}", reply_markup=main_menu(is_admin))
        return
    except StardError as e:  # مثلاً گرفتن قیمت ریکشن هنگام بررسی کد
        log.error("place order error: %s", e)
        await cb.message.answer(API_DOWN, reply_markup=main_menu(is_admin))
        return
    o = await db.get_order(oid)
    u = await db.get_user(user.id)
    tail = ("پشتیبانی به‌زودی سفارش را انجام می‌دهد و نتیجه را همین‌جا اطلاع می‌دهیم."
            if o["status"] == "manual" else "سفارش خودکار انجام می‌شود و نتیجه را همین‌جا اطلاع می‌دهیم.")
    await cb.message.answer(
        f"✅ <b>سفارش #{oid} ثبت شد</b>\n\n"
        f"📦 {escape(o['title'])} → {escape(o['recipient'] or '')}\n"
        f"💵 {fmt_toman(o['price'])} کسر شد | 💰 موجودی: {fmt_toman(u.balance)}\n\n{tail}",
        reply_markup=main_menu(is_admin), disable_web_page_preview=True)
    await notify.order_placed(bot, db, admins, oid)
