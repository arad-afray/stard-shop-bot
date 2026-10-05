"""شبیه‌ساز Stard API برای Test Mode: هیچ درخواستی به Stard نمی‌رود و هیچ پول واقعی خرج نمی‌شود.

- همان رابط StardClient را دارد؛ فروشگاه برای سفارش‌های test (orders.is_test=1) به جای API واقعی از این استفاده می‌کند.
- بدون حالت: شناسه‌ی سفارش از Idempotency-Key ساخته می‌شود (تکرار امن) و وضعیت از زمان ساخت سفارش در
  پایگاه داده محاسبه می‌شود؛ پس بعد از ری‌استارت و روی چند نمونه هم درست است.
- مثل محیط sk_test_ واقعی، سفارش بعد از ۱۵ ثانیه completed می‌شود؛ مدیر می‌تواند نتیجه را از پنل تعیین کند.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

from .stard_api import StardError

COMPLETE_AFTER = 15
STAR = 4382


class SimulatedStardClient:
    is_simulator = True

    def __init__(self, db: Any):
        self.db = db
        self.observer = None
        self.last_error = None
        self._overrides: dict[str, str] = {}
        self.products_ = {
            9001: {"id": 9001, "name": "پریمیوم ۳ ماهه (آزمایشی)", "category": "premium", "price": 3_788_624},
            9002: {"id": 9002, "name": "پریمیوم ۶ ماهه (آزمایشی)", "category": "premium", "price": 5_050_000},
            9003: {"id": 9003, "name": "پریمیوم ۱۲ ماهه (آزمایشی)", "category": "premium", "price": 9_150_000},
            9101: {"id": 9101, "name": "🧸 خرس", "category": "star_gift", "price": 112_275, "stars_price": 15},
            9102: {"id": 9102, "name": "🌹 گل رز", "category": "star_gift", "price": 187_125, "stars_price": 25},
            9103: {"id": 9103, "name": "💎 الماس", "category": "star_gift", "price": 748_500, "stars_price": 100},
            9201: {"id": 9201, "name": "Plush Pepe #1", "category": "nft", "price": 25_000_000},
            9301: {"id": 9301, "name": "@sample_name", "category": "username", "price": 12_000_000},
            9401: {"id": 9401, "name": "+888 0000 0001", "category": "number", "price": 30_000_000},
        }

    async def close(self) -> None:
        pass

    def _product(self, pid: int) -> dict:
        p = self.products_.get(int(pid))
        if p is None:
            raise StardError(404, "product_not_found", "محصول پیدا نشد")
        return {"object": "product", "id": p["id"], "name": p["name"], "category": p["category"],
                "price": {"amount": p["price"], "currency": "IRT"}, "stars_price": p.get("stars_price"),
                "available": True, "purchasable_via_api": True, "pricing_type": "fixed"}

    # ---------- عمومی ----------
    async def ping(self) -> dict:
        return {"object": "ping", "ok": True, "environment": "simulator",
                "key": {"display": "simulator", "scopes": ["*"]}}

    async def status(self) -> dict:
        return {"object": "status", "status": "operational", "services": {"api": "operational"}}

    async def openapi_version(self) -> str:
        return "simulator"

    async def prices(self) -> dict:
        return {"object": "prices", "currency": "IRT", "star": {"amount": STAR, "unit": "1 star"},
                "ton": {"amount": 385_770, "usd": 1.5}, "usd": {"amount": 256_300},
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    async def premium_prices(self) -> list[dict]:
        return [{"object": "premium_plan", "product_id": p["id"], "name": p["name"],
                 "price": {"amount": p["price"], "currency": "IRT"}, "available": True}
                for p in self.products_.values() if p["category"] == "premium"]

    async def gifts(self) -> list[dict]:
        return [self._product(p["id"]) for p in self.products_.values() if p["category"] == "star_gift"]

    async def products(self, *, category: str | None = None, **_: Any) -> dict:
        data = [self._product(p["id"]) for p in self.products_.values() if category in (None, p["category"])]
        return {"object": "list", "data": data, "has_more": False}

    async def categories(self) -> list[dict]:
        return [{"slug": c, "label": c} for c in sorted({p["category"] for p in self.products_.values()})]

    async def product(self, product_id: int) -> dict:
        return self._product(product_id)

    async def boosts(self) -> dict:
        return {"object": "boost_catalog", "enabled": True, "max_quantity": 1000, "durations": [
            {"duration": 7, "label": "۷ روزه", "price_per_boost": 13_200, "available": True},
            {"duration": 30, "label": "۱ ماهه", "price_per_boost": 32_100, "available": True}]}

    async def quote(self, type_: str, *, quantity: int = 1, product_id: int | None = None,
                    duration: int | None = None) -> dict:
        if type_ == "stars":
            if quantity < 50:
                raise StardError(400, "parameter_invalid", "حداقل ۵۰")
            amount = STAR * quantity
        elif type_ == "boost":
            d = next((x for x in (await self.boosts())["durations"] if x["duration"] == duration), None)
            if d is None:
                raise StardError(409, "boost_unavailable", "ناموجود")
            amount = d["price_per_boost"] * quantity
        else:
            amount = self._product(product_id)["price"]["amount"]
        return {"object": "quote", "id": f"qt_sim_{amount}", "amount": amount, "currency": "IRT",
                "product_available": True}

    # ---------- سفارش ----------
    def _ref(self, key: str) -> str:
        return "ord_sim_" + hashlib.sha256(key.encode()).hexdigest()[:16]

    async def create_order(self, *, idempotency_key: str, recipient: str | None = None, **_: Any) -> dict:
        return {"object": "order", "id": self._ref(idempotency_key), "status": "pending", "failure_reason": None}

    async def create_boost_order(self, *, idempotency_key: str, recipient: str, **_: Any) -> dict:
        if not recipient.startswith("@"):
            raise StardError(400, "recipient_invalid", "کانال نامعتبر")
        return await self.create_order(idempotency_key=idempotency_key)

    async def get_order(self, ref: str) -> dict:
        if ref in self._overrides:
            return {"object": "order", "id": ref, "status": self._overrides[ref], "failure_reason": "simulated"
                    if self._overrides[ref] in ("failed", "refunded") else None}
        row = await self.db.one("SELECT created_at FROM orders WHERE stard_ref = :r", {"r": ref})
        if row is None:
            raise StardError(404, "order_not_found", "پیدا نشد")
        created = datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        status = "completed" if time.time() - created >= COMPLETE_AFTER else "pending"
        return {"object": "order", "id": ref, "status": status, "failure_reason": None}

    get_boost_order = get_order

    async def cancel_order(self, ref: str) -> dict:
        o = await self.get_order(ref)
        if o["status"] != "pending":
            raise StardError(409, "order_not_cancellable", "شروع شده")
        self._overrides[ref] = "cancelled"
        return {**o, "status": "cancelled"}

    async def simulate(self, ref: str, outcome: str) -> dict:
        self._overrides[ref] = outcome
        return await self.get_order(ref)

    async def wallet(self) -> dict:
        return {"object": "wallet", "environment": "simulator",
                "balances": [{"currency": "IRT", "amount": 100_000_000}], "note": "Test Mode — پول واقعی نیست"}

    async def transactions(self, **_: Any) -> dict:
        return {"object": "list", "data": []}
