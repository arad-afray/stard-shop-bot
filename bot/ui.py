"""کیبوردها، callback dataها و متن‌های مشترک."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .pricing import fmt_toman

# ---------- دکمه‌های منوی اصلی ----------
BTN_SHOP = "🛍 فروشگاه"
BTN_ACCOUNT = "👤 حساب کاربری"
BTN_TOPUP = "💳 شارژ کیف پول"
BTN_ORDERS = "📦 سفارش‌های من"
BTN_SUPPORT = "🆘 پشتیبانی"
BTN_ADMIN = "⚙️ پنل مدیریت"
BTN_CANCEL = "❌ انصراف"

STATUS_LABEL = {
    "new": "🕓 در حال ثبت",
    "pending": "⏳ در صف انجام",
    "processing": "⚙️ در حال انجام",
    "completed": "✅ انجام شد",
    "cancelled": "↩️ لغو شد (مبلغ برگشت)",
    "refunded": "↩️ مبلغ برگشت داده شد",
    "failed": "❌ ناموفق (مبلغ برگشت)",
}

STARS_PRESETS = (50, 100, 250, 500, 1000, 2500, 5000, 10000)


class Nav(CallbackData, prefix="nav"):
    to: str                     # shop | stars | premium | gifts | home


class StarsQty(CallbackData, prefix="sq"):
    qty: int                    # 0 = تعداد دلخواه


class PickProduct(CallbackData, prefix="pp"):
    cat: str
    pid: int


class GiftPage(CallbackData, prefix="gp"):
    page: int


class Act(CallbackData, prefix="act"):
    name: str                   # confirm | cancel | self | skipmsg


class Adm(CallbackData, prefix="adm"):
    name: str
    arg: str = ""


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_SHOP)],
        [KeyboardButton(text=BTN_ACCOUNT), KeyboardButton(text=BTN_TOPUP)],
        [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_SUPPORT)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)


def shop_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⭐ استارز تلگرام", callback_data=Nav(to="stars"))
    b.button(text="💎 تلگرام پریمیوم", callback_data=Nav(to="premium"))
    b.button(text="🎁 گیفت استارزی", callback_data=Nav(to="gifts"))
    b.adjust(1)
    return b.as_markup()


def stars_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for q in STARS_PRESETS:
        b.button(text=f"⭐ {q:,}", callback_data=StarsQty(qty=q))
    b.button(text="✍️ تعداد دلخواه", callback_data=StarsQty(qty=0))
    b.button(text="🔙 بازگشت", callback_data=Nav(to="shop"))
    b.adjust(2, 2, 2, 2, 1, 1)
    return b.as_markup()


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


def confirm_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ تأیید و پرداخت", callback_data=Act(name="confirm"))
    b.button(text="❌ انصراف", callback_data=Act(name="cancel"))
    b.adjust(2)
    return b.as_markup()


def topup_review_menu(tid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ تأیید", callback_data=Adm(name="tp_ok", arg=str(tid)))
    b.button(text="❌ رد", callback_data=Adm(name="tp_no", arg=str(tid)))
    b.adjust(2)
    return b.as_markup()


def admin_menu(shop_open: bool, is_test: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📊 آمار", callback_data=Adm(name="stats"))
    b.button(text="💰 درصد سود", callback_data=Adm(name="profit"))
    b.button(text="💳 شارژهای در انتظار", callback_data=Adm(name="topups"))
    b.button(text="👥 مدیریت کاربر", callback_data=Adm(name="user"))
    b.button(text="🧾 سفارش‌های اخیر", callback_data=Adm(name="orders"))
    b.button(text="🏦 کیف پول Stard", callback_data=Adm(name="wallet"))
    b.button(text="📢 پیام همگانی", callback_data=Adm(name="broadcast"))
    b.button(text="🛠 تنظیمات", callback_data=Adm(name="settings"))
    b.button(text="🔴 بستن فروشگاه" if shop_open else "🟢 باز کردن فروشگاه", callback_data=Adm(name="toggle"))
    if is_test:
        b.button(text="🧪 شبیه‌سازی سفارش (test)", callback_data=Adm(name="sim"))
    b.adjust(2)
    return b.as_markup()


def back_admin() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔙 پنل مدیریت", callback_data=Adm(name="home"))
    return b.as_markup()
