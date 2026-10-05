"""منطق فروشگاه: کاتالوگ، قیمت‌گذاری، ثبت سفارش و همگام‌سازی وضعیت با Stard.

جریان هر سفارش خودکار:
  1. پیش‌قیمت (POST /orders/quote) → قیمت خرید + quote_id قفل‌شده برای ۲ دقیقه
  2. قیمت فروش = سود بخش + قانون‌های قیمت + VIP (bot/commerce.py)؛ هرگز کمتر از قیمت خرید
  3. بررسی‌های خرید (نمایش، زبان، سقف روزانه، موجودی انبار)
  4. کسر موجودی کاربر + ساخت سفارش + کار ارسال در صف (یک تراکنش؛ outbox)
  5. POST /orders یا POST /boosts/orders با Idempotency-Key = "bot-<id سفارش>"
  6. worker وضعیت را دنبال می‌کند؛ اگر failed/cancelled/refunded شد پول کاربر برمی‌گردد.

ریکشن استارزی در Stard API وجود ندارد؛ سفارش آن «دستی» است و مدیر انجامش می‌دهد.
Test Mode: سفارش‌های test با SimulatedStardClient انجام می‌شوند (بدون API واقعی و بدون پول واقعی).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .commerce import Commerce, PurchaseBlocked
from .db import CouponInvalid, Database, OutOfStock, User, now
from .features import Features
from .pricing import apply_discount, apply_profit
from .stard_api import FINAL_STATUSES, REFUND_STATUSES, StardClient, StardError

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")
# لینک پست کانال عمومی (t.me/channel/123) یا خصوصی (t.me/c/123456/789)
POST_LINK_RE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:(c/\d{5,})|([A-Za-z][A-Za-z0-9_]{3,31}))/(\d{1,10})/?(?:\?.*)?$",
                          re.IGNORECASE)
STARS_MIN, STARS_MAX = 50, 1_000_000
REACTION_MIN, REACTION_MAX = 1, 10_000
COUPON_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
# بخش‌هایی که از فهرست عمومی محصولات Stard فروخته می‌شوند (بخش ربات → slug دسته در Stard)
CATALOG_CATEGORIES = {"nft": "nft", "username": "username", "number": "number"}

# خطاهایی که یعنی سفارش قطعاً ثبت نشده و پول باید برگردد
DEFINITE_REJECT = {400, 402, 403, 404, 409, 422}

CACHE_TTL = 60  # ثانیه؛ کاتالوگ و نرخ‌ها. پیش‌قیمت سفارش هیچ‌وقت کش نمی‌شود.
SUBMIT_GRACE = 30  # ثانیه؛ worker بعد از این مدت سراغ ارسال سفارش می‌رود اگر ارسال درون‌خطی کامل نشده باشد
ACTIVE = ("new", "pending", "processing")


@dataclass
class Offer:
    """یک پیشنهاد قیمت آماده‌ی خرید."""
    category: str            # stars | premium | star_gift | boost | reaction | nft | username | number
    type: str                # stars | product | boost | reaction
    title: str
    base_amount: int         # قیمت Stard
    price: int               # قیمت فروش (بعد از قانون‌های قیمت و VIP)
    quantity: int = 1
    product_id: int | None = None
    quote_id: str | None = None
    duration: int | None = None
    needs_recipient: bool = True
    notes: list = field(default_factory=list)  # توضیح قانون‌های قیمت اعمال‌شده
    is_test: bool = False


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
                 pay_currency: str | None = None, queue=None, locks=None, features: Features | None = None,
                 commerce: Commerce | None = None, simulator: Any = None):
        self.db = db
        self.api = api
        self.default_profit = default_profit
        self.pay_currency = pay_currency
        self.queue = queue      # JobQueue؛ اگر None باشد سفارش فقط درون‌خطی ارسال می‌شود
        self.locks = locks      # DistributedLock
        self.features = features or Features(db)
        self.commerce = commerce or Commerce(db)
        if simulator is None:
            from .simulator import SimulatedStardClient
            simulator = SimulatedStardClient(db)
        self.simulator = simulator
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

    # ---------- Test Mode ----------
    async def test_mode(self) -> str:
        """off | admins | all"""
        v = await self.db.get_setting("test_mode", "off")
        return v if v in ("off", "admins", "all") else "off"

    async def is_test_for(self, is_admin: bool) -> bool:
        mode = await self.test_mode()
        return mode == "all" or (mode == "admins" and is_admin)

    def api_for(self, test: bool):
        return self.simulator if test else self.api

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

    async def maintenance(self) -> tuple[bool, str]:
        on = (await self.db.get_setting("maintenance", "0")) == "1"
        msg = await self.db.get_setting("maintenance_text") or \
            "🧹 ربات در حال به‌روزرسانی و نگهداری است. لطفاً کمی بعد دوباره سر بزنید."
        return on, msg

    async def category_enabled(self, category: str, user_id: int | None = None) -> bool:
        return await self.features.category_state(category, user_id) == "ok"

    async def enabled_categories(self, user_id: int | None = None) -> list[str]:
        return [c for c, ok in await self.features.shop_buttons(user_id) if ok]

    async def round_to(self) -> int:
        try:
            return max(1, int(await self.db.get_setting("round_to", 1000)))
        except ValueError:
            return 1000

    async def sell(self, category: str, base: int) -> int:
        """قیمت با سود بخش (بدون قانون‌ها و VIP)؛ همان فرمول نسخه‌ی ۲."""
        return apply_profit(base, await self.get_profit(category), await self.round_to())

    async def price(self, category: str, base: int, *, user: User | None = None,
                    product_id: int | None = None) -> tuple[int, list[str]]:
        """قیمت نهایی برای این کاربر: سود + قانون‌های قیمت + VIP، هرگز کمتر از قیمت خرید."""
        return await self.commerce.adjust(await self.sell(category, base), base, category, user=user,
                                          product_id=product_id, round_to=await self.round_to())

    async def _ensure_enabled(self, category: str, user: User | None = None) -> None:
        state = await self.features.category_state(category, user.id if user else None)
        if state != "ok":
            raise ShopError("این بخش فعلاً غیرفعال است.")

    # ---------- نرخ‌ها ----------
    async def rates(self, test: bool = False) -> dict:
        """نرخ لحظه‌ای استارز، TON و دلار از GET /prices (کش ۶۰ ثانیه‌ای)."""
        api = self.api_for(test)
        return await self._cached(f"prices:{test}", api.prices)

    async def star_unit_sell(self) -> int:
        """قیمت فروش یک استارز با سود دسته‌ی stars (بدون گرد کردن)."""
        unit = int((await self.rates())["star"]["amount"])
        return apply_profit(unit, await self.get_profit("stars"), round_to=1)

    # ---------- کاتالوگ ----------
    async def stars_offer(self, quantity: int, *, user: User | None = None, test: bool = False) -> Offer:
        await self._ensure_enabled("stars", user)
        if not STARS_MIN <= quantity <= STARS_MAX:
            raise ShopError(f"تعداد استارز باید بین {STARS_MIN:,} تا {STARS_MAX:,} باشد.")
        q = await self.api_for(test).quote("stars", quantity=quantity)
        base = int(q["amount"])
        price, notes = await self.price("stars", base, user=user)
        return Offer(category="stars", type="stars", title=f"⭐ {quantity:,} استارز تلگرام", quantity=quantity,
                     base_amount=base, price=price, quote_id=q["id"], notes=notes, is_test=test)

    async def premium_plans(self, *, user: User | None = None, test: bool = False) -> list[dict]:
        api = self.api_for(test)
        plans = await self._cached(f"premium:{test}", api.premium_prices)
        hidden = await self.commerce.hidden_products("premium")
        out = []
        for p in plans:
            if p.get("available") and int(p["product_id"]) not in hidden:
                price, _ = await self.price("premium", int(p["price"]["amount"]), user=user,
                                            product_id=int(p["product_id"]))
                out.append(dict(p, sell_price=price))
        return out

    async def gifts(self, *, user: User | None = None, test: bool = False) -> list[dict]:
        api = self.api_for(test)
        gifts = await self._cached(f"gifts:{test}", api.gifts)
        hidden = await self.commerce.hidden_products("star_gift")
        out = []
        for g in gifts:
            if g.get("available") and g.get("purchasable_via_api") and int(g["id"]) not in hidden:
                price, _ = await self.price("star_gift", int(g["price"]["amount"]), user=user, product_id=int(g["id"]))
                out.append(dict(g, sell_price=price))
        return out

    async def catalog(self, category: str, *, user: User | None = None, test: bool = False) -> list[dict]:
        """محصولات NFT / یوزرنیم / شماره که از API قابل خریدند (قیمت ثابت)."""
        slug = await self.db.get_setting(f"slug:{category}") or CATALOG_CATEGORIES[category]
        api = self.api_for(test)

        async def fetch():
            res = await api.products(category=slug, purchasable=True, sort="price_asc", limit=50)
            return res.get("data") or []
        items = await self._cached(f"catalog:{category}:{test}", fetch)
        hidden = await self.commerce.hidden_products(category)
        out = []
        for p in items:
            if p.get("available") and p.get("purchasable_via_api") and int(p["id"]) not in hidden \
                    and (p.get("price") or {}).get("amount"):
                price, _ = await self.price(category, int(p["price"]["amount"]), user=user, product_id=int(p["id"]))
                out.append(dict(p, sell_price=price))
        return out

    async def product_offer(self, product_id: int, category: str, *, user: User | None = None,
                            test: bool = False) -> Offer:
        await self._ensure_enabled(category, user)
        api = self.api_for(test)
        p = await api.product(product_id)
        slug = await self.db.get_setting(f"slug:{category}") or CATALOG_CATEGORIES.get(category, category)
        if p.get("category") not in (category, slug):
            raise ShopError("این محصول در این بخش نیست.")
        if not p.get("available") or not p.get("purchasable_via_api"):
            raise ShopError("این محصول الان قابل خرید نیست.")
        if int(product_id) in await self.commerce.hidden_products(category):
            raise ShopError("این محصول فعلاً در دسترس نیست.")
        q = await api.quote("product", product_id=product_id)
        if not q.get("product_available", True):
            raise ShopError("این محصول همین الان ناموجود شد.")
        base = int(q["amount"])
        price, notes = await self.price(category, base, user=user, product_id=int(product_id))
        return Offer(category=category, type="product", title=p["name"], product_id=product_id, base_amount=base,
                     price=price, quote_id=q["id"], notes=notes, is_test=test)

    async def boost_catalog(self, *, user: User | None = None, test: bool = False) -> dict:
        """{"enabled", "max_quantity", "durations": [... با sell_per_boost]}"""
        api = self.api_for(test)
        cat = await self._cached(f"boosts:{test}", api.boosts)
        durations = []
        for d in cat.get("durations") or []:
            if d.get("available") and d.get("price_per_boost"):
                per, _ = await self.price("boost", int(d["price_per_boost"]), user=user)
                durations.append(dict(d, sell_per_boost=per))
        return {"enabled": bool(cat.get("enabled", True)) and bool(durations),
                "max_quantity": int(cat.get("max_quantity") or 1000), "durations": durations}

    async def boost_offer(self, quantity: int, duration: int, *, user: User | None = None,
                          test: bool = False) -> Offer:
        await self._ensure_enabled("boost", user)
        cat = await self.boost_catalog(user=user, test=test)
        if not cat["enabled"]:
            raise ShopError("فروش بوست فعلاً غیرفعال است.")
        d = next((x for x in cat["durations"] if int(x["duration"]) == duration), None)
        if d is None:
            raise ShopError("این مدت بوست فعلاً موجود نیست.")
        if not 1 <= quantity <= cat["max_quantity"]:
            raise ShopError(f"تعداد بوست باید بین ۱ تا {cat['max_quantity']:,} باشد.")
        try:
            q = await self.api_for(test).quote("boost", quantity=quantity, duration=duration)
        except StardError as e:
            if e.code in ("boost_unavailable", "boost_disabled"):
                raise ShopError("این تعداد/مدت بوست فعلاً موجود نیست.") from e
            raise
        base = int(q["amount"])
        price, notes = await self.price("boost", base, user=user)
        return Offer(category="boost", type="boost", title=f"🚀 {quantity:,} بوست {d.get('label') or f'{duration} روزه'}",
                     quantity=quantity, duration=duration, base_amount=base, price=price, quote_id=q["id"],
                     notes=notes, is_test=test)

    async def reaction_offer(self, quantity: int, *, user: User | None = None, test: bool = False) -> Offer:
        await self._ensure_enabled("reaction", user)
        if not REACTION_MIN <= quantity <= REACTION_MAX:
            raise ShopError(f"تعداد ریکشن استارزی باید بین {REACTION_MIN:,} تا {REACTION_MAX:,} باشد.")
        unit = int((await self.rates(test))["star"]["amount"])
        base = unit * quantity
        price, notes = await self.price("reaction", base, user=user)
        return Offer(category="reaction", type="reaction", title=f"❤️ {quantity:,} ریکشن استارزی",
                     quantity=quantity, base_amount=base, price=price, notes=notes, is_test=test)

    # ---------- کد تخفیف / پیشنهاد اختصاصی ----------
    async def check_coupon(self, code: str, user_id: int, offer: Offer) -> tuple[int, int]:
        """(قیمت نهایی، مبلغ تخفیف) یا ShopError."""
        code = code.strip().upper()
        c = await self.db.get_coupon(code) if COUPON_RE.match(code) else None
        if c is None or not c["active"] or (c["user_id"] and c["user_id"] != user_id):
            raise ShopError("کد تخفیف معتبر نیست.")
        if c["expires_at"] and c["expires_at"] <= now():
            raise ShopError("مهلت این کد تخفیف تمام شده است.")
        if c["category"] and c["category"] != offer.category:
            raise ShopError("این کد برای این بخش نیست.")
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
                          gift_message: str | None = None, coupon: str | None = None,
                          checkout_id: str | None = None, *, user: User | None = None,
                          is_admin: bool = False) -> int:
        """کسر موجودی و ثبت سفارش. شناسه‌ی سفارش محلی را برمی‌گرداند.

        InsufficientBalance اگر موجودی کم باشد؛ ShopError اگر خرید مجاز نباشد، Stard سفارش را رد کند
        (پول برگشته) یا کد تخفیف دیگر معتبر نباشد (پولی کسر نشده).
        """
        if user is not None:
            try:
                await self.commerce.check_purchase(user, offer.category, offer.product_id, offer.quantity,
                                                   is_admin=is_admin)
            except PurchaseBlocked as e:
                raise ShopError(str(e)) from e
        price, discount = offer.price, 0
        if coupon:
            price, discount = await self.check_coupon(coupon, user_id, offer)
            coupon = coupon.strip().upper()
        manual = offer.type == "reaction"
        stock_key = await self.commerce.stock_key(offer.category, offer.product_id)

        async def outbox(c, oid: int) -> None:
            # الگوی outbox: کار ارسال در همان تراکنش کسر پول ثبت می‌شود. اگر ربات بلافاصله بعد از پرداخت
            # کرش کند، worker سفارش را با همان Idempotency-Key می‌فرستد؛ سفارش هرگز گم نمی‌شود.
            if self.queue is not None and not manual:
                await self.queue.enqueue("order.submit", {"oid": oid}, dedupe_key=f"order:{oid}",
                                         delay=SUBMIT_GRACE, c=c)
        try:
            oid = await self.db.create_order_and_debit(
                user_id=user_id, type_=offer.type, category=offer.category, product_id=offer.product_id,
                title=offer.title, quantity=offer.quantity, recipient=recipient, gift_message=gift_message,
                quote_id=offer.quote_id, base_amount=offer.base_amount, price=price, duration=offer.duration,
                status="manual" if manual else "new", coupon=coupon, discount=discount,
                checkout_id=checkout_id, is_test=offer.is_test, stock_key=stock_key, after_insert=outbox)
        except CouponInvalid as e:
            raise ShopError("کد تخفیف دیگر معتبر نیست. دوباره بدون کد یا با کد دیگر تلاش کنید.") from e
        except OutOfStock as e:
            raise ShopError("موجودی این محصول همین الان تمام شد؛ پولی کسر نشد.") from e
        if manual:
            return oid
        await self.submit(oid)
        row = await self.db.get_order(oid)
        if row["refunded"]:
            raise ShopError(_reject_text(row["failure_reason"]))
        return oid

    async def submit(self, oid: int) -> None:
        """ارسال سفارش محلی به Stard. با Idempotency-Key تکرارش امن است.

        قفل توزیع‌شده‌ی order:<id> نمی‌گذارد دو نمونه (ربات و worker) هم‌زمان یک سفارش را بفرستند.
        """
        if self.locks is not None:
            if not await self.locks.acquire(f"order:{oid}", 90):
                return  # نمونه‌ی دیگری همین الان در حال ارسال است
            try:
                await self._submit(oid)
            finally:
                await self.locks.release(f"order:{oid}")
        else:
            await self._submit(oid)

    async def _submit(self, oid: int) -> None:
        o = await self.db.get_order(oid)
        if o is None or o["status"] != "new":
            return
        api = self.api_for(bool(o["is_test"]))
        meta = {"bot_order_id": oid, "user_id": o["user_id"]}
        try:
            if o["type"] == "boost":
                res = await api.create_boost_order(
                    idempotency_key=f"bot-{oid}", recipient=o["recipient"], quantity=o["quantity"],
                    duration=o["duration"], quote_id=o["quote_id"], pay_currency=self.pay_currency, metadata=meta)
            else:
                res = await api.create_order(
                    idempotency_key=f"bot-{oid}", type_=o["type"], product_id=o["product_id"],
                    quantity=o["quantity"], recipient=o["recipient"], gift_message=o["gift_message"],
                    quote_id=o["quote_id"], pay_currency=self.pay_currency, metadata=meta)
        except StardError as e:
            if e.status in DEFINITE_REJECT and e.code != "request_in_progress":
                log.warning("order %s rejected by Stard: %s %s", oid, e.status, e.code)
                await self.db.refund_order(oid, "cancelled", e.code)
            else:
                # نامعلوم (شبکه/5xx): سفارش 'new' می‌ماند و worker با همان کلید دوباره می‌فرستد
                log.error("order %s submit failed, will retry: %s %s", oid, e.status, e.code)
                await self.db.update_order(oid, failure_reason=f"retrying: {e.code}"[:250])
            return
        status = res.get("status") or "pending"
        if status in REFUND_STATUSES:
            await self.db.update_order(oid, stard_ref=res["id"])
            await self.db.refund_order(oid, status, res.get("failure_reason"))
        else:
            await self.db.update_order(oid, stard_ref=res["id"], status=status, failure_reason=None)

    async def fetch_remote(self, o) -> dict:
        api = self.api_for(bool(o["is_test"]))
        if o["type"] == "boost":
            return await api.get_boost_order(o["stard_ref"])
        return await api.get_order(o["stard_ref"])

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
    async def complete_manual(self, oid: int, admin_id: int | None = None) -> bool:
        return await self.db.set_order_status(oid, "completed", only_from=("manual",), admin_id=admin_id)

    async def admin_refund(self, oid: int, reason: str = "admin", admin_id: int | None = None) -> bool:
        """برگشت پول دستی. فقط برای سفارش دستی یا سفارشی که هنوز در Stard ثبت نشده (یا در Stard لغو شده).

        هر درخواست در مرکز بازپرداخت (جدول refunds) ثبت می‌شود: pending → completed یا failed.
        """
        o = await self.db.get_order(oid)
        if o is None or o["refunded"] or o["status"] == "completed":
            return False
        await self._refund_state(o, "pending", reason, admin_id, None)
        try:
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
                    await self.api_for(bool(o["is_test"])).cancel_order(o["stard_ref"])
                except StardError as e:
                    raise ShopError(f"لغو در Stard ممکن نشد ({e.code}). سفارش احتمالاً شروع شده است.") from e
        except ShopError as e:
            await self._refund_state(o, "failed", reason, admin_id, str(e))
            raise
        return await self.db.refund_order(oid, "cancelled", reason, admin_id=admin_id)

    async def _refund_state(self, o, status: str, reason: str, admin_id: int | None, error: str | None) -> None:
        t = now()
        await self.db.write(
            "INSERT INTO refunds(order_id, user_id, amount, status, reason, admin_id, error, created_at, updated_at) "
            "VALUES(:o, :u, :a, :s, :r, :adm, :e, :t, :t) ON CONFLICT(order_id) DO UPDATE SET status = excluded.status, "
            "error = excluded.error, admin_id = excluded.admin_id, updated_at = excluded.updated_at "
            "WHERE refunds.status != 'completed'",
            {"o": o["id"], "u": o["user_id"], "a": o["price"], "s": status, "r": reason[:250], "adm": admin_id,
             "e": (error or None) and error[:250], "t": t})

    async def referral_percent(self) -> float:
        if not await self.features.enabled("referral"):
            return 0.0
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
