"""منطق فروشگاه: کاتالوگ، قیمت با سود، ثبت سفارش و همگام‌سازی وضعیت با Stard.

جریان هر سفارش:
  1. پیش‌قیمت (POST /orders/quote) → قیمت خرید + quote_id قفل‌شده برای ۲ دقیقه
  2. کسر موجودی کاربر + ساخت سفارش محلی (اتمیک)
  3. POST /orders با Idempotency-Key = "bot-<id سفارش>"
  4. worker وضعیت را با GET /orders/{id} دنبال می‌کند؛ اگر failed/cancelled/refunded شد پول کاربر برمی‌گردد.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from .db import Database
from .pricing import apply_profit
from .stard_api import FINAL_STATUSES, REFUND_STATUSES, StardClient, StardError

log = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{3,31}$")
STARS_MIN, STARS_MAX = 50, 1_000_000

# خطاهایی که یعنی سفارش قطعاً ثبت نشده و پول باید برگردد
DEFINITE_REJECT = {400, 402, 403, 404, 409, 422}


@dataclass
class Offer:
    """یک پیشنهاد قیمت آماده‌ی خرید."""
    category: str            # stars | premium | star_gift
    type: str                # stars | product
    title: str
    base_amount: int         # قیمت Stard
    price: int               # قیمت فروش
    quantity: int = 1
    product_id: int | None = None
    quote_id: str | None = None
    needs_recipient: bool = True


class ShopError(Exception):
    """خطایی که پیامش برای نمایش به کاربر مناسب است."""


def normalize_username(text: str) -> str | None:
    text = text.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    if not USERNAME_RE.match(text):
        return None
    return "@" + text.lstrip("@")


class Shop:
    def __init__(self, db: Database, api: StardClient, *, default_profit: float = 10.0,
                 pay_currency: str | None = None):
        self.db = db
        self.api = api
        self.default_profit = default_profit
        self.pay_currency = pay_currency

    # ---------- تنظیمات سود ----------
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

    async def sell(self, category: str, base: int) -> int:
        return apply_profit(base, await self.get_profit(category))

    # ---------- کاتالوگ ----------
    async def stars_offer(self, quantity: int) -> Offer:
        if not STARS_MIN <= quantity <= STARS_MAX:
            raise ShopError(f"تعداد استارز باید بین {STARS_MIN:,} تا {STARS_MAX:,} باشد.")
        q = await self.api.quote("stars", quantity=quantity)
        return Offer(category="stars", type="stars", title=f"⭐ {quantity:,} استارز تلگرام",
                     quantity=quantity, base_amount=int(q["amount"]),
                     price=await self.sell("stars", int(q["amount"])), quote_id=q["id"])

    async def premium_plans(self) -> list[dict]:
        plans = [p for p in await self.api.premium_prices() if p.get("available")]
        for p in plans:
            p["sell_price"] = await self.sell("premium", int(p["price"]["amount"]))
        return plans

    async def gifts(self) -> list[dict]:
        gifts = [g for g in await self.api.gifts() if g.get("available") and g.get("purchasable_via_api")]
        for g in gifts:
            g["sell_price"] = await self.sell("star_gift", int(g["price"]["amount"]))
        return gifts

    async def product_offer(self, product_id: int, category: str) -> Offer:
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

    # ---------- سفارش ----------
    async def place_order(self, user_id: int, offer: Offer, recipient: str | None,
                          gift_message: str | None = None) -> int:
        """کسر موجودی، ثبت سفارش در Stard. شناسه‌ی سفارش محلی را برمی‌گرداند.

        InsufficientBalance اگر موجودی کم باشد؛ ShopError اگر Stard سفارش را رد کند (پول برگشته).
        """
        oid = await self.db.create_order_and_debit(
            user_id=user_id, type_=offer.type, category=offer.category, product_id=offer.product_id,
            title=offer.title, quantity=offer.quantity, recipient=recipient, gift_message=gift_message,
            quote_id=offer.quote_id, base_amount=offer.base_amount, price=offer.price)
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
        try:
            res = await self.api.create_order(
                idempotency_key=f"bot-{oid}", type_=o["type"], product_id=o["product_id"],
                quantity=o["quantity"], recipient=o["recipient"], gift_message=o["gift_message"],
                quote_id=o["quote_id"], pay_currency=self.pay_currency,
                metadata={"bot_order_id": oid, "user_id": o["user_id"]})
        except StardError as e:
            if e.status in DEFINITE_REJECT and e.code not in ("request_in_progress",):
                log.warning("order %s rejected by Stard: %s", oid, e)
                await self.db.refund_order(oid, "cancelled", e.code)
            else:
                # نامعلوم (شبکه/5xx): سفارش 'new' می‌ماند و worker با همان کلید دوباره می‌فرستد
                log.error("order %s submit failed, will retry: %s", oid, e)
            return
        await self.db.update_order(oid, stard_ref=res["id"], status=res.get("status") or "pending")
        if res.get("status") in REFUND_STATUSES:
            await self.db.refund_order(oid, res["status"], res.get("failure_reason"))

    async def sync_order(self, oid: int) -> tuple[str, str] | None:
        """وضعیت یک سفارش را از Stard می‌گیرد. اگر عوض شد (قبلی، جدید) را برمی‌گرداند."""
        o = await self.db.get_order(oid)
        if o is None or o["status"] in FINAL_STATUSES:
            return None
        old = o["status"]
        if old == "new":
            await self.submit(oid)
        else:
            res = await self.api.get_order(o["stard_ref"])
            new = res.get("status") or old
            if new in REFUND_STATUSES:
                await self.db.refund_order(oid, new, res.get("failure_reason"))
            elif new != old:
                await self.db.update_order(oid, status=new)
        cur = (await self.db.get_order(oid))["status"]
        return (old, cur) if cur != old else None


def _reject_text(code: str | None) -> str:
    return {
        "insufficient_funds": "موجودی فروشگاه موقتاً کافی نیست. مبلغ به کیف پول شما برگشت.",
        "price_changed": "قیمت همین الان تغییر کرد. مبلغ برگشت؛ لطفاً دوباره تلاش کنید.",
        "recipient_invalid": "یوزرنیم گیرنده معتبر نیست. مبلغ به کیف پول شما برگشت.",
        "product_unavailable": "محصول همین الان ناموجود شد. مبلغ به کیف پول شما برگشت.",
        "product_not_found": "محصول پیدا نشد. مبلغ به کیف پول شما برگشت.",
    }.get(code or "", "سفارش ثبت نشد و مبلغ به کیف پول شما برگشت.")


Notifier = Callable[[int, str], Awaitable[None]]
