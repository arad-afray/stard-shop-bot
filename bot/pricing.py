"""محاسبه‌ی قیمت فروش: قیمت API + درصد سود، گرد شده به بالا."""
from __future__ import annotations

# دسته‌هایی که ربات می‌فروشد
CATEGORIES = {
    "stars": "⭐ استارز تلگرام",
    "premium": "💎 تلگرام پریمیوم",
    "star_gift": "🎁 گیفت استارزی",
    "boost": "🚀 بوست کانال و گروه",
    "reaction": "❤️ ریکشن استارزی",
    "nft": "🖼 گیفت NFT",
    "username": "👤 یوزرنیم",
    "number": "📱 شماره مجازی",
}

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def apply_profit(base: int, percent: float, round_to: int = 1000) -> int:
    """قیمت نهایی = base × (1 + percent/100)، به بالا گرد شده به مضرب round_to.

    گرد کردن به بالا تضمین می‌کند قیمت فروش هیچ‌وقت کمتر از قیمت خرید نشود.
    """
    if base < 0:
        raise ValueError("base must be >= 0")
    if percent < 0:
        raise ValueError("percent must be >= 0")
    # عملیات صحیح برای جلوگیری از خطای ممیز شناور: درصد با دقت ۰.۰۱
    raw = base * (10000 + round(percent * 100))
    price = -(-raw // 10000)  # ceil
    if round_to > 1:
        price = -(-price // round_to) * round_to
    return int(price)


def apply_discount(price: int, base: int, percent: float) -> tuple[int, int]:
    """(قیمت بعد از تخفیف، مبلغ تخفیف). قیمت هیچ‌وقت از قیمت خرید (base) کمتر نمی‌شود."""
    if percent <= 0:
        return price, 0
    off = price * round(min(percent, 100) * 100) // 10000
    final = max(price - off, base)
    return final, price - final


def to_int(text: str | None) -> int | None:
    """عدد از ورودی کاربر؛ ارقام فارسی/عربی و جداکننده‌ی هزارگان را می‌پذیرد."""
    t = (text or "").translate(PERSIAN_DIGITS).replace(",", "").replace("٬", "").replace(" ", "").strip()
    return int(t) if t.isdigit() else None


def to_float(text: str | None) -> float | None:
    t = (text or "").translate(PERSIAN_DIGITS).replace("%", "").replace("٪", "").replace("/", ".").strip()
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def fmt_toman(amount: int) -> str:
    return f"{amount:,} تومان"
