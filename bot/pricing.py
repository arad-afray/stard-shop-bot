"""محاسبه‌ی قیمت فروش: قیمت API + درصد سود، گرد شده به بالا."""
from __future__ import annotations

# دسته‌هایی که ربات می‌فروشد؛ کلید همان category در API است
CATEGORIES = {
    "stars": "⭐ استارز تلگرام",
    "premium": "💎 تلگرام پریمیوم",
    "star_gift": "🎁 گیفت استارزی",
}


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


def fmt_toman(amount: int) -> str:
    return f"{amount:,} تومان"
