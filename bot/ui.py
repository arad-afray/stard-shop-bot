"""کیبوردها، callback dataها و متن‌های مشترک."""
from __future__ import annotations

import contextlib

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .pricing import CATEGORIES, fmt_toman

# ---------- دکمه‌های منوی اصلی ----------
BTN_SHOP = "🛍 فروشگاه"
BTN_ACCOUNT = "👤 حساب کاربری"
BTN_TOPUP = "💳 شارژ کیف پول"
BTN_ORDERS = "📦 سفارش‌های من"
BTN_PRICES = "💹 قیمت لحظه‌ای"
BTN_REFERRAL = "🎁 دعوت دوستان"
BTN_SUPPORT = "🆘 پشتیبانی"
BTN_ADMIN = "⚙️ پنل مدیریت"
BTN_CANCEL = "❌ انصراف"

JOIN_CHECK = "join:check"

STATUS_LABEL = {
    "new": "🕓 در حال ثبت",
    "pending": "⏳ در صف انجام",
    "processing": "⚙️ در حال انجام",
    "manual": "🧑‍💻 در صف انجام توسط پشتیبانی",
    "completed": "✅ انجام شد",
    "cancelled": "↩️ لغو شد (مبلغ برگشت)",
    "refunded": "↩️ مبلغ برگشت داده شد",
    "failed": "❌ ناموفق (مبلغ برگشت)",
}

STARS_PRESETS = (50, 100, 250, 500, 1000, 2500, 5000, 10000)
BOOST_PRESETS = (1, 3, 5, 10, 25, 50, 100, 250)
REACTION_PRESETS = (10, 25, 50, 100, 250, 500, 1000, 2500)


class Nav(CallbackData, prefix="nav"):
    to: str                     # shop | stars | premium | gifts | boost | reaction


class StarsQty(CallbackData, prefix="sq"):
    qty: int                    # 0 = تعداد دلخواه


class PickProduct(CallbackData, prefix="pp"):
    cat: str
    pid: int


class GiftPage(CallbackData, prefix="gp"):
    page: int


class BoostDur(CallbackData, prefix="bd"):
    days: int


class BoostQty(CallbackData, prefix="bq"):
    days: int
    qty: int                    # 0 = تعداد دلخواه


class ReactQty(CallbackData, prefix="rq"):
    qty: int                    # 0 = تعداد دلخواه


class Act(CallbackData, prefix="act"):
    name: str                   # confirm | cancel | self | skipmsg | coupon | nocoupon


class Adm(CallbackData, prefix="adm"):
    name: str
    arg: str = ""


class GPrice(CallbackData, prefix="gpr"):
    what: str                   # all | usd | ton | stars


async def edit_or_send(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """پیام را ویرایش می‌کند؛ اگر نشد (پیام عکس است، قدیمی است، …) پیام تازه می‌فرستد."""
    if not hasattr(message, "edit_text"):  # InaccessibleMessage: پیام قدیمی‌تر از ۴۸ ساعت
        await message.answer(text, reply_markup=markup, disable_web_page_preview=True)
        return
    try:
        await message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def drop_markup(message: Message) -> None:
    if not hasattr(message, "edit_reply_markup"):
        return
    with contextlib.suppress(TelegramBadRequest):
        await message.edit_reply_markup(reply_markup=None)


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SHOP)],
        [KeyboardButton(text=BTN_ACCOUNT), KeyboardButton(text=BTN_TOPUP)],
        [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_PRICES)],
        [KeyboardButton(text=BTN_REFERRAL), KeyboardButton(text=BTN_SUPPORT)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)


def join_menu(channels: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for ch in channels:
        b.button(text=f"📣 {ch.get('title') or ch['chat']}", url=ch["url"])
    b.button(text="✅ عضو شدم", callback_data=JOIN_CHECK)
    b.adjust(1)
    return b.as_markup()


SHOP_NAV = {"stars": "stars", "premium": "premium", "star_gift": "gifts", "boost": "boost", "reaction": "reaction"}


def shop_menu(enabled: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for cat in enabled:
        b.button(text=CATEGORIES[cat], callback_data=Nav(to=SHOP_NAV[cat]))
    b.adjust(1)
    return b.as_markup()


def _qty_menu(presets, make, back: str, unit: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for q in presets:
        b.button(text=f"{unit} {q:,}", callback_data=make(q))
    b.button(text="✍️ تعداد دلخواه", callback_data=make(0))
    b.button(text="🔙 بازگشت", callback_data=Nav(to=back))
    b.adjust(*([2] * ((len(presets) + 1) // 2)), 1, 1)
    return b.as_markup()


def stars_menu() -> InlineKeyboardMarkup:
    return _qty_menu(STARS_PRESETS, lambda q: StarsQty(qty=q), "shop", "⭐")


def reaction_menu() -> InlineKeyboardMarkup:
    return _qty_menu(REACTION_PRESETS, lambda q: ReactQty(qty=q), "shop", "❤️")


def boost_duration_menu(durations: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for d in durations:
        b.button(text=f"🚀 {d.get('label') or str(d['duration']) + ' روزه'} — هر بوست {fmt_toman(d['sell_per_boost'])}",
                 callback_data=BoostDur(days=int(d["duration"])))
    b.button(text="🔙 بازگشت", callback_data=Nav(to="shop"))
    b.adjust(1)
    return b.as_markup()


def boost_qty_menu(days: int, max_qty: int) -> InlineKeyboardMarkup:
    presets = [q for q in BOOST_PRESETS if q <= max_qty]
    return _qty_menu(presets, lambda q: BoostQty(days=days, qty=q), "boost", "🚀")


def premium_menu(plans: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in plans:
        b.button(text=f"{p['name']} — {fmt_toman(p['sell_price'])}",
                 callback_data=PickProduct(cat="premium", pid=p["product_id"]))
    b.button(text="🔙 بازگشت", callback_data=Nav(to="shop"))
    b.adjust(1)
    return b.as_markup()


GIFTS_PER_PAGE = 8


def gifts_menu(gifts: list[dict], page: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    chunk = gifts[page * GIFTS_PER_PAGE:(page + 1) * GIFTS_PER_PAGE]
    for g in chunk:
        stars = f" ({g['stars_price']}⭐)" if g.get("stars_price") else ""
        b.button(text=f"{g['name']}{stars} — {fmt_toman(g['sell_price'])}",
                 callback_data=PickProduct(cat="star_gift", pid=g["id"]))
    b.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️ قبلی", callback_data=GiftPage(page=page - 1))
    if (page + 1) * GIFTS_PER_PAGE < len(gifts):
        nav.button(text="بعدی ▶️", callback_data=GiftPage(page=page + 1))
    b.attach(nav)
    back = InlineKeyboardBuilder()
    back.button(text="🔙 بازگشت", callback_data=Nav(to="shop"))
    b.attach(back)
    return b.as_markup()


def recipient_menu(own_username: str | None) -> InlineKeyboardMarkup | None:
    if not own_username:
        return None
    b = InlineKeyboardBuilder()
    b.button(text=f"🙋 برای خودم (@{own_username})", callback_data=Act(name="self"))
    return b.as_markup()


def skip_message_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⏭ بدون پیام", callback_data=Act(name="skipmsg"))
    return b.as_markup()


def confirm_menu(has_coupon: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ تأیید و پرداخت", callback_data=Act(name="confirm"))
    b.button(text="❌ انصراف", callback_data=Act(name="cancel"))
    if has_coupon:
        b.button(text="🗑 حذف کد تخفیف", callback_data=Act(name="nocoupon"))
    else:
        b.button(text="🎟 کد تخفیف دارم", callback_data=Act(name="coupon"))
    b.adjust(2, 1)
    return b.as_markup()


def topup_review_menu(tid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ تأیید", callback_data=Adm(name="tp_ok", arg=str(tid)))
    b.button(text="❌ رد", callback_data=Adm(name="tp_no", arg=str(tid)))
    b.adjust(2)
    return b.as_markup()


def manual_order_menu(oid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ انجام شد", callback_data=Adm(name="o_done", arg=str(oid)))
    b.button(text="↩️ رد و برگشت پول", callback_data=Adm(name="o_refund", arg=str(oid)))
    b.adjust(2)
    return b.as_markup()


def admin_menu(shop_open: bool, is_test: bool, is_owner: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 آمار", callback_data=Adm(name="stats"))
    b.button(text="💰 درصد سود", callback_data=Adm(name="profit"))
    b.button(text="💳 شارژهای در انتظار", callback_data=Adm(name="topups"))
    b.button(text="👥 مدیریت کاربر", callback_data=Adm(name="user"))
    b.button(text="🧾 سفارش‌ها", callback_data=Adm(name="orders"))
    b.button(text="🗂 بخش‌های فروشگاه", callback_data=Adm(name="cats"))
    b.button(text="📣 جوین اجباری", callback_data=Adm(name="join"))
    b.button(text="🎟 کد تخفیف", callback_data=Adm(name="coupons"))
    b.button(text="🎁 زیرمجموعه‌گیری", callback_data=Adm(name="ref"))
    b.button(text="💹 قیمت در گروه", callback_data=Adm(name="group"))
    b.button(text="🏦 کیف پول Stard", callback_data=Adm(name="wallet"))
    b.button(text="📢 پیام همگانی", callback_data=Adm(name="broadcast"))
    b.button(text="🛠 تنظیمات", callback_data=Adm(name="settings"))
    b.button(text="💾 پشتیبان", callback_data=Adm(name="backup"))
    if is_owner:
        b.button(text="👮 مدیرها", callback_data=Adm(name="admins"))
    b.button(text="🔴 بستن فروشگاه" if shop_open else "🟢 باز کردن فروشگاه", callback_data=Adm(name="toggle"))
    if is_test:
        b.button(text="🧪 شبیه‌سازی سفارش (test)", callback_data=Adm(name="sim"))
    b.adjust(2)
    return b.as_markup()


def back_admin(extra: InlineKeyboardBuilder | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    if extra is not None:
        extra.attach(b)
        return extra.as_markup()
    return b.as_markup()


def price_group_menu(bot_username: str | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 به‌روزرسانی", callback_data=GPrice(what="all"))
    if bot_username:
        b.button(text="🛒 خرید از ربات", url=f"https://t.me/{bot_username}")
    b.adjust(2)
    return b.as_markup()
