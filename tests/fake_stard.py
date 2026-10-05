"""شبیه‌ساز Stard API برای تست، بر اساس نمونه‌پاسخ‌های مستندات."""
from __future__ import annotations

import json

import httpx


def err(status: int, code: str, message: str = "x") -> httpx.Response:
    return httpx.Response(status, json={"error": {"type": "invalid_request_error", "code": code,
                                                  "message": message, "param": None, "request_id": "req_t"}})


class FakeStard:
    def __init__(self):
        self.balance = 100_000_000
        self.unit = 4382
        self.boost_unit = 13200
        self.orders: dict[str, dict] = {}
        self.idem: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_next: list[httpx.Response | Exception] = []
        self.drop_responses = 0  # درخواست انجام شود ولی پاسخ در شبکه گم شود
        self.products = {
            4518: {"object": "product", "id": 4518, "name": "پریمیوم ۳ ماهه", "category": "premium",
                   "price": {"amount": 3788624, "currency": "IRT"}, "available": True, "purchasable_via_api": True},
            12577: {"object": "product", "id": 12577, "name": "خرس", "category": "star_gift", "stars_price": 15,
                    "price": {"amount": 112275, "currency": "IRT"}, "available": True, "purchasable_via_api": True},
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        res = self._handle(req)
        if self.drop_responses:
            self.drop_responses -= 1
            raise httpx.ReadTimeout("lost", request=req)
        return res

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/v1")
        self.calls.append((req.method, path))
        if self.fail_next:
            f = self.fail_next.pop(0)
            if isinstance(f, Exception):
                raise f
            return f
        if req.headers.get("authorization") != "Bearer sk_test_ok":
            return err(401, "invalid_api_key")
        body = json.loads(req.content) if req.content else {}

        if path == "/ping":
            return httpx.Response(200, json={"object": "ping", "ok": True, "environment": "test",
                                             "key": {"display": "sk_test_…", "scopes": ["orders:write"]}})
        if path == "/premium/prices":
            p = self.products[4518]
            return httpx.Response(200, json={"object": "list", "data": [
                {"object": "premium_plan", "product_id": 4518, "name": p["name"], "price": p["price"],
                 "available": True}], "has_more": False, "next_offset": None})
        if path == "/gifts":
            return httpx.Response(200, json={"object": "list", "data": [self.products[12577]]})
        if path.startswith("/products/"):
            pid = int(path.split("/")[2])
            if pid not in self.products:
                return err(404, "product_not_found")
            return httpx.Response(200, json=self.products[pid])
        if path == "/prices":
            return httpx.Response(200, json={"object": "prices", "currency": "IRT",
                                             "star": {"amount": self.unit, "unit": "1 star"},
                                             "ton": {"amount": 385770, "usd": 1.4976}, "usd": {"amount": 256300},
                                             "updated_at": "2026-10-01T10:31:48Z"})
        if path == "/boosts":
            return httpx.Response(200, json={"object": "boost_catalog", "enabled": True, "currency": "IRT",
                                             "max_quantity": 1000, "durations": [
                                                 {"duration": 7, "label": "۷ روزه", "price_per_boost": self.boost_unit,
                                                  "available": True},
                                                 {"duration": 14, "label": "۱۴ روزه", "price_per_boost": None,
                                                  "available": False}]})
        if path == "/boosts/orders" and req.method == "POST":
            key = req.headers.get("idempotency-key")
            if key in self.idem:
                return httpx.Response(201, json=self.idem[key])
            if not str(body.get("recipient", "")).startswith("@"):
                return err(400, "recipient_invalid")
            amount = body["quantity"] * self.boost_unit
            if body.get("quote_id") and body["quote_id"] != f"qt_{amount}":
                return err(409, "price_changed")
            if amount > self.balance:
                return err(402, "insufficient_funds")
            self.balance -= amount
            oid = f"ord_test_{len(self.orders) + 1}"
            order = {"object": "order", "id": oid, "status": "pending", "type": "boost", "quantity": body["quantity"],
                     "duration": body["duration"], "recipient": body["recipient"],
                     "amount": {"amount": amount, "currency": "IRT"}, "failure_reason": None,
                     "metadata": body.get("metadata", {})}
            self.orders[oid] = order
            self.idem[key] = order
            return httpx.Response(201, json=order)
        if path.startswith("/boosts/orders/") and req.method == "GET":
            ref = path.split("/")[3]
            if ref not in self.orders:
                return err(404, "order_not_found")
            return httpx.Response(200, json=self.orders[ref])
        if path.startswith("/orders/") and path.endswith("/cancel"):
            ref = path.split("/")[2]
            o = self.orders.get(ref)
            if o is None:
                return err(404, "order_not_found")
            if o["status"] != "pending":
                return err(409, "order_not_cancellable")
            o["status"] = "cancelled"
            self.balance += o["amount"]["amount"]
            return httpx.Response(200, json=o)
        if path == "/status":
            return httpx.Response(200, json={"object": "status", "status": "operational",
                                             "services": {"api": "operational"}})
        if path == "/transactions":
            return httpx.Response(200, json={"object": "list", "data": []})
        if path == "/orders/quote":
            if body["type"] == "boost":
                if body.get("duration") != 7:
                    return err(409, "boost_unavailable")
                amount = body["quantity"] * self.boost_unit
            elif body["type"] == "stars":
                if body["quantity"] < 50:
                    return err(400, "parameter_invalid")
                amount = body["quantity"] * self.unit
            else:
                amount = self.products[body["product_id"]]["price"]["amount"]
            return httpx.Response(200, json={"object": "quote", "id": f"qt_{amount}", "amount": amount,
                                             "currency": "IRT", "product_available": True})
        if path == "/orders" and req.method == "POST":
            key = req.headers.get("idempotency-key")
            if key in self.idem:
                return httpx.Response(201, json=self.idem[key], headers={"Idempotent-Replayed": "true"})
            if body["type"] == "stars":
                amount = body["quantity"] * self.unit
                title = f"⭐ {body['quantity']} استارز"
            else:
                amount = self.products[body["product_id"]]["price"]["amount"]
                title = self.products[body["product_id"]]["name"]
            if body.get("quote_id") and body["quote_id"] != f"qt_{amount}":
                return err(409, "price_changed")
            if amount > self.balance:
                return err(402, "insufficient_funds")
            self.balance -= amount
            oid = f"ord_test_{len(self.orders) + 1}"
            order = {"object": "order", "id": oid, "status": "pending", "type": body["type"], "title": title,
                     "amount": {"amount": amount, "currency": "IRT"}, "failure_reason": None,
                     "metadata": body.get("metadata", {})}
            self.orders[oid] = order
            self.idem[key] = order
            return httpx.Response(201, json=order)
        if path.startswith("/orders/") and req.method == "GET":
            ref = path.split("/")[2]
            if ref not in self.orders:
                return err(404, "order_not_found")
            return httpx.Response(200, json=self.orders[ref])
        if path == "/wallet":
            return httpx.Response(200, json={"object": "wallet", "environment": "test",
                                             "balances": [{"currency": "IRT", "amount": self.balance}]})
        return err(404, "not_found")
