"""منطق فروشگاه: کاتالوگ، قیمت با سود، ثبت سفارش و همگام‌سازی وضعیت با Stard.

جریان هر سفارش خودکار:
  1. پیش‌قیمت (POST /orders/quote) → قیمت خرید + quote_id قفل‌شده برای ۲ دقیقه
  2. کسر موجودی کاربر + ساخت سفارش محلی (اتمیک)
  3. POST /orders یا POST /boosts/orders با Idempotency-Key = "bot-<id سفارش>"
  4. worker وضعیت را دنبال می‌کند؛ اگر failed/cancelled/refunded شد پول کاربر برمی‌گردد.

ریکشن استارزی در Stard API وجود ندارد؛ پس سفارش آن «دستی» است: پول کسر می‌شود،
مدیر خبردار می‌شود و با یک دکمه آن را «انجام شد» یا «رد و برگشت پول» می‌کند.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .db import CouponInvalid, Database
from .pricing import CATEGORIES, apply_discount, apply_profit
from .stard_api import FINAL_STATUSES, REFUND_STATUSES, StardClient, StardError

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")
# لینک پست کانال عمومی (t.me/channel/123) یا خصوصی (t.me/c/123456/789)
POST_LINK_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:(c/\d{5,})|([A-Za-z][A-Za-z0-9_]{3,31}))/(\d{1,10})/?(?:\?.*)?$",
                          re.IGNORECASE)
STARS_MIN, STARS_MAX = 50, 1_000_000
REACTION_MIN, REACTION_MAX = 1, 10_000
COUPON_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")

# خطاهایی که یعنی سفارش قطعاً ثبت نشده و پول باید برگردد
DEFINITE_REJECT = {400, 402, 403, 404, 409, 422}

CACHE_TTL = 60  # ثانیه؛ کاتالوگ و نرخ‌ها. پیش‌قیمت سفارش هیچ‌وقت کش نمی‌شود.


@dataclass
class Offer:
    """یک پیشنهاد قیمت آماده‌ی خرید."""
    category: str            # stars | premium | star_gift | boost | reaction
    type: str                # stars | product | boost | reaction
    title: str
    base_amount: int         # قیمت Stard
    price: int               # قیمت فروش
    quantity: int = 1
    product_id: int | None = None
    quote_id: str | None = None
    duration: int | None = None
    needs_recipient: bool = True


class ShopError(Exception):
    """خطایی که پیامش برای نمایش به کاربر مناسب است."""


def normalize_username(text: str) -> str | None:
    text = text.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    text = text.rstrip("/")
    if not USERNAME_RE.match(text):
        return None
    return "@" + text.lstrip("@")


def normalize_post_link(text: str) -> str | None:
    m = POST_LINK_RE.match(text.strip())
    if not m:
        return None
    private, public, msg = m.groups()
    return f"https://t.me/{private or public}/{msg}"


class Shop:
    def __init__(self, db: Database, api: StardClient, *, default_profit: float = 10.0,
                 pay_currency: str | None = None):
        self.db = db
        self.api = api
        self.default_profit = default_profit
        self.pay_currency = pay_currency
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _cached(self, key: str, fetch: Callable[[], Awaitable[Any]], ttl: float = CACHE_TTL) -> Any:
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        value = await fetch()
        self._cache[key] = (time.monotonic() + ttl, value)
        return value

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---------- تنظیمات ----------
    async def get_profit(self, category: str | None = None) -> float:
        if category:
            v = await self.db.get_setting(f"profit:{category}")
            if v is not None:
                return float(v)
        v = await self.db.get_setting("profit:global")
        return float(v) if v is not None else self.default_profit

    async def set_profit(self, percent: float, category: str | None = None) -> None:
        if not 0 <= percent <= 1000:
            raise ShopError("درصد سود باید بین ۰ تا ۱۰۰۰ باشد.")
        await self.db.set_setting(f"profit:{category or 'global'}", percent)

    async def clear_category_profit(self, category: str) -> None:
        await self.db.del_setting(f"profit:{category}")

    async def is_open(self) -> bool:
        return (await self.db.get_setting("shop_open", "1")) == "1"

    async def category_enabled(self, category: str) -> bool:
        return (await self.db.get_setting(f"cat:{category}", "1")) == "1"

    async def enabled_categories(self) -> list[str]:
        return [c for c in CATEGORIES if await self.category_enabled(c)]

    async def round_to(self) -> int:
        try:
            return max(1, int(await self.db.get_setting("round_to", 1000)))
        except ValueError:
            return 1000

    async def sell(self, category: str, base: int) -> int:
        return apply_profit(base, await self.get_profit(category), await self.round_to())

    async def _ensure_enabled(self, category: str) -> None:
        if not await self.category_enabled(category):
            raise ShopError("این بخش فعلاً غیرفعال است.")

    # ---------- نرخ‌ها ----------
    async def rates(self) -> dict:
        """نرخ لحظه‌ای استارز، TON و دلار از GET /prices (کش ۶۰ ثانیه‌ای)."""
        return await self._cached("prices", self.api.prices)

    async def star_unit_sell(self) -> int:
        """قیمت فروش یک استارز با سود دسته‌ی stars (بدون گرد کردن)."""
        unit = int((await self.rates())["star"]["amount"])
        return apply_profit(unit, await self.get_profit("stars"), round_to=1)

    # ---------- کاتالوگ ----------
    async def stars_offer(self, quantity: int) -> Offer:
        await self._ensure_enabled("stars")
        if not STARS_MIN <= quantity <= STARS_MAX:
            raise ShopError(f"تعداد استارز باید بین {STARS_MIN:,} تا {STARS_MAX:,} باشد.")
        q = await self.api.quote("stars", quantity=quantity)
        return Offer(category="stars", type="stars", title=f"⭐ {quantity:,} استارز تلگرام",
                     quantity=quantity, base_amount=int(q["amount"]),
                     price=await self.sell("stars", int(q["amount"])), quote_id=q["id"])

    async def premium_plans(self) -> list[dict]:
        plans = await self._cached("premium", self.api.premium_prices)
        return [dict(p, sell_price=await self.sell("premium", int(p["price"]["amount"])))
                for p in plans if p.get("available")]

    async def gifts(self) -> list[dict]:
        gifts = await self._cached("gifts", self.api.gifts)
        return [dict(g, sell_price=await self.sell("star_gift", int(g["price"]["amount"])))
                for g in gifts if g.get("available") and g.get("purchasable_via_api")]

    async def product_offer(self, product_id: int, category: str) -> Offer:
        await self._ensure_enabled(category)
        p = await self.api.product(product_id)
        if p.get("category") != category:
            raise ShopError("این محصول در این بخش نیست.")
        if not p.get("available") or not p.get("purchasable_via_api"):
            raise ShopError("این محصول الان قابل خرید نیست.")
        q = await self.api.quote("product", product_id=product_id)
        if not q.get("product_available", True):
            raise ShopError("این محصول همین الان ناموجود شد.")
        base = int(q["amount"])
        return Offer(category=category, type="product", title=p["name"], product_id=product_id,
                     base_amount=base, price=await self.sell(category, base), quote_id=q["id"])

    async def boost_catalog(self) -> dict:
        """{"enabled", "max_quantity", "durations": [... با sell_per_boost]}"""
        cat = await self._cached("boosts", self.api.boosts)
        durations = []
        for d in cat.get("durations") or []:
            if d.get("available") and d.get("price_per_boost"):
                durations.append(dict(d, sell_per_boost=await self.sell("boost", int(d["price_per_boost"]))))
        return {"enabled": bool(cat.get("enabled", True)) and bool(durations),
                "max_quantity": int(cat.get("max_quantity") or 1000), "durations": durations}

    async def boost_offer(self, quantity: int, duration: int) -> Offer:
        await self._ensure_enabled("boost")
        cat = await self.boost_catalog()
        if not cat["enabled"]:
            raise ShopError("فروش بوست فعلاً غیرفعال است.")
        d = next((x for x in cat["durations"] if int(x["duration"]) == duration), None)
        if d is None:
            raise ShopError("این مدت بوست فعلاً موجود نیست.")
        if not 1 <= quantity <= cat["max_quantity"]:
            raise ShopError(f"تعداد بوست باید بین ۱ تا {cat['max_quantity']:,} باشد.")
        try:
            q = await self.api.quote("boost", quantity=quantity, duration=duration)
        except StardError as e:
            if e.code in ("boost_unavailable", "boost_disabled"):
                raise ShopError("این تعداد/مدت بوست فعلاً موجود نیست.") from e
            raise
        base = int(q["amount"])
        return Offer(category="boost", type="boost", title=f"🚀 {quantity:,} بوست {d.get('label') or f'{duration} روزه'}",
                     quantity=quantity, duration=duration, base_amount=base,
                     price=await self.sell("boost", base), quote_id=q["id"])

    async def reaction_offer(self, quantity: int) -> Offer:
        await self._ensure_enabled("reaction")
        if not REACTION_MIN <= quantity <= REACTION_MAX:
            raise ShopError(f"تعداد ریکشن استارزی باید بین {REACTION_MIN:,} تا {REACTION_MAX:,} باشد.")
        unit = int((await self.rates())["star"]["amount"])
        base = unit * quantity
        return Offer(category="reaction", type="reaction", title=f"❤️ {quantity:,} ریکشن استارزی",
                     quantity=quantity, base_amount=base, price=await self.sell("reaction", base))

    # ---------- کد تخفیف ----------
    async def check_coupon(self, code: str, user_id: int, offer: Offer) -> tuple[int, int]:
        """(قیمت نهایی، مبلغ تخفیف) یا ShopError."""
        code = code.strip().upper()
        c = await self.db.get_coupon(code) if COUPON_RE.match(code) else None
        if c is None or not c["active"]:
            raise ShopError("کد تخفیف معتبر نیست.")
        if c["max_uses"] and c["used"] >= c["max_uses"]:
            raise ShopError("ظرفیت این کد تخفیف تمام شده است.")
        if await self.db.coupon_used_by(code, user_id):
            raise ShopError("شما قبلاً از این کد تخفیف استفاده کرده‌اید.")
        final, off = apply_discount(offer.price, offer.base_amount, float(c["percent"]))
        if off <= 0:
            raise ShopError("این کد روی این محصول تخفیفی ندارد.")
        return final, off

    # ---------- سفارش ----------
    async def place_order(self, user_id: int, offer: Offer, recipient: str | None,
                          gift_message: str | None = None, coupon: str | None = None) -> int:
        """کسر موجودی و ثبت سفارش. شناسه‌ی سفارش محلی را برمی‌گرداند.

        InsufficientBalance اگر موجودی کم باشد؛ ShopError اگر Stard سفارش را رد کند (پول برگشته)
        یا کد تخفیف دیگر معتبر نباشد (پولی کسر نشده).
        """
        price, discount = offer.price, 0
        if coupon:
            price, discount = await self.check_coupon(coupon, user_id, offer)
            coupon = coupon.strip().upper()
        manual = offer.type == "reaction"
        try:
            oid = await self.db.create_order_and_debit(
                user_id=user_id, type_=offer.type, category=offer.category, product_id=offer.product_id,
                title=offer.title, quantity=offer.quantity, recipient=recipient, gift_message=gift_message,
                quote_id=offer.quote_id, base_amount=offer.base_amount, price=price, duration=offer.duration,
                status="manual" if manual else "new", coupon=coupon, discount=discount)
        except CouponInvalid as e:
            raise ShopError("کد تخفیف دیگر معتبر نیست. دوباره بدون کد یا با کد دیگر تلاش کنید.") from e
        if manual:
            return oid
        await self.submit(oid)
        row = await self.db.get_order(oid)
        if row["refunded"]:
            raise ShopError(_reject_text(row["failure_reason"]))
        return oid

    async def submit(self, oid: int) -> None:
        """ارسال سفارش محلی به Stard. با Idempotency-Key تکرارش امن است."""
        o = await self.db.get_order(oid)
        if o is None or o["status"] != "new":
            return
        meta = {"bot_order_id": oid, "user_id": o["user_id"]}
        try:
            if o["type"] == "boost":
                res = await self.api.create_boost_order(
                    idempotency_key=f"bot-{oid}", recipient=o["recipient"], quantity=o["quantity"],
                    duration=o["duration"], quote_id=o["quote_id"], pay_currency=self.pay_currency, metadata=meta)
            else:
                res = await self.api.create_order(
                    idempotency_key=f"bot-{oid}", type_=o["type"], product_id=o["product_id"],
                    quantity=o["quantity"], recipient=o["recipient"], gift_message=o["gift_message"],
                    quote_id=o["quote_id"], pay_currency=self.pay_currency, metadata=meta)
        except StardError as e:
            if e.status in DEFINITE_REJECT and e.code != "request_in_progress":
                log.warning("order %s rejected by Stard: %s", oid, e)
                await self.db.refund_order(oid, "cancelled", e.code)
            else:
                # نامعلوم (شبکه/5xx): سفارش 'new' می‌ماند و worker با همان کلید دوباره می‌فرستد
                log.error("order %s submit failed, will retry: %s", oid, e)
            return
        status = res.get("status") or "pending"
        if status in REFUND_STATUSES:
            await self.db.update_order(oid, stard_ref=res["id"])
            await self.db.refund_order(oid, status, res.get("failure_reason"))
        else:
            await self.db.update_order(oid, stard_ref=res["id"], status=status)

    async def fetch_remote(self, o) -> dict:
        if o["type"] == "boost":
            return await self.api.get_boost_order(o["stard_ref"])
        return await self.api.get_order(o["stard_ref"])

    async def sync_order(self, oid: int) -> tuple[str, str] | None:
        """وضعیت یک سفارش را از Stard می‌گیرد. اگر عوض شد (قبلی، جدید) را برمی‌گرداند."""
        o = await self.db.get_order(oid)
        if o is None or o["status"] in FINAL_STATUSES or o["status"] == "manual":
            return None
        old = o["status"]
        if old == "new" or not o["stard_ref"]:
            await self.submit(oid)
        else:
            res = await self.fetch_remote(o)
            new = res.get("status") or old
            if new in REFUND_STATUSES:
                await self.db.refund_order(oid, new, res.get("failure_reason"))
            elif new != old:
                await self.db.set_order_status(oid, new, only_from=(old,))
        cur = (await self.db.get_order(oid))["status"]
        return (old, cur) if cur != old else None

    # ---------- عملیات مدیر ----------
    async def complete_manual(self, oid: int) -> bool:
        return await self.db.set_order_status(oid, "completed", only_from=("manual",))

    async def admin_refund(self, oid: int, reason: str = "admin") -> bool:
        """برگشت پول دستی. فقط برای سفارش دستی یا سفارشی که هنوز در Stard ثبت نشده (یا در Stard لغو شده)."""
        o = await self.db.get_order(oid)
        if o is None or o["refunded"] or o["status"] == "completed":
            return False
        if o["status"] == "new":
            # شاید سفارش در Stard ثبت شده ولی پاسخش در شبکه گم شده باشد؛ با همان Idempotency-Key
            # دوباره می‌فرستیم تا وضعیت واقعی معلوم شود، وگرنه ممکن است هم محصول برسد هم پول برگردد.
            await self.submit(oid)
            o = await self.db.get_order(oid)
            if o["refunded"]:
                return True
            if o["status"] == "new":
                raise ShopError("ارتباط با Stard برقرار نیست؛ وضعیت سفارش معلوم نیست. کمی بعد دوباره تلاش کنید.")
        if o["stard_ref"] and o["status"] in ACTIVE:
            # در Stard ثبت شده؛ اول باید آنجا لغو شود تا پول دو بار خرج نشود
            try:
                await self.api.cancel_order(o["stard_ref"])
            except StardError as e:
                raise ShopError(f"لغو در Stard ممکن نشد ({e.code}). سفارش احتمالاً شروع شده است.") from e
        return await self.db.refund_order(oid, "cancelled", reason)

    async def referral_percent(self) -> float:
        try:
            return float(await self.db.get_setting("referral_percent", 0))
        except ValueError:
            return 0.0

    async def after_complete(self, oid: int) -> tuple[int, int] | None:
        """پاداش معرف بعد از انجام سفارش (فقط یک بار)."""
        pct = await self.referral_percent()
        if pct <= 0:
            return None
        return await self.db.pay_referral(oid, pct)


ACTIVE = ("new", "pending", "processing")


def _reject_text(code: str | None) -> str:
    return {
        "insufficient_funds": "موجودی فروشگاه موقتاً کافی نیست. مبلغ به کیف پول شما برگشت.",
        "price_changed": "قیمت همین الان تغییر کرد. مبلغ برگشت؛ لطفاً دوباره تلاش کنید.",
        "quote_expired": "مهلت قیمت تمام شد. مبلغ برگشت؛ لطفاً دوباره تلاش کنید.",
        "recipient_invalid": "یوزرنیم گیرنده (یا کانال) معتبر نیست. مبلغ به کیف پول شما برگشت.",
        "product_unavailable": "محصول همین الان ناموجود شد. مبلغ به کیف پول شما برگشت.",
        "product_not_found": "محصول پیدا نشد. مبلغ به کیف پول شما برگشت.",
        "boost_unavailable": "این بوست همین الان ناموجود شد. مبلغ به کیف پول شما برگشت.",
        "boost_disabled": "فروش بوست موقتاً غیرفعال است. مبلغ به کیف پول شما برگشت.",
    }.get(code or "", "سفارش ثبت نشد و مبلغ به کیف پول شما برگشت.")
