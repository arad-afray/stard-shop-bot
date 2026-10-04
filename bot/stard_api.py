"""کلاینت async برای Stard Market API v1.

مستندات: https://stard-market.ir/docs
- احراز هویت: هدر Authorization: Bearer sk_test_… / sk_live_…
- مبلغ‌ها عدد صحیح تومان (IRT) هستند.
- خطاها همیشه به شکل {"error": {"type", "code", "message", "param", "request_id"}} برمی‌گردند.
- ثبت سفارش با Idempotency-Key امن تکرار می‌شود.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# وضعیت‌های نهایی سفارش طبق مستندات /docs/orders
FINAL_STATUSES = {"completed", "cancelled", "refunded", "failed"}
# وضعیت‌هایی که یعنی پول به کیف پول API برگشته و باید به کاربر هم برگردد
REFUND_STATUSES = {"cancelled", "refunded", "failed"}


class StardError(Exception):
    """خطای برگشتی از API. روی code تصمیم بگیر، message برای نمایش است."""

    def __init__(self, status: int, code: str, message: str, *, type_: str = "",
                 param: str | None = None, request_id: str | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.type = type_
        self.param = param
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500 or self.code == "request_in_progress"


class StardClient:
    def __init__(self, api_key: str, base_url: str = "https://stard-market.ir/api/v1", *,
                 timeout: float = 20.0, max_retries: int = 4,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "stard-shop-bot/1.0"},
            timeout=timeout,
            transport=transport,
        )
        self.max_retries = max_retries

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       json: dict | None = None, idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        # POST فقط با Idempotency-Key دوباره فرستاده می‌شود (طبق /docs/errors)
        can_retry = method == "GET" or idempotency_key is not None
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            try:
                r = await self._client.request(method, path, params=params, json=json, headers=headers)
            except httpx.TransportError as e:
                if not can_retry or attempt == self.max_retries:
                    raise StardError(0, "network_error", f"خطای شبکه: {e}") from e
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if r.status_code < 400:
                return r.json() if r.content else None

            err = _parse_error(r)
            if can_retry and err.retryable and attempt < self.max_retries:
                wait = delay
                if r.status_code == 429:
                    try:
                        wait = float(r.headers.get("Retry-After", delay))
                    except ValueError:
                        pass
                log.warning("Stard %s %s -> %s, retry in %.1fs", method, path, err.code, wait)
                await asyncio.sleep(wait)
                delay *= 2
                continue
            raise err
        raise AssertionError("unreachable")

    # ---------- عمومی ----------
    async def ping(self) -> dict:
        return await self._request("GET", "/ping")

    async def status(self) -> dict:
        return await self._request("GET", "/status")

    # ---------- محصولات و قیمت ----------
    async def categories(self) -> list[dict]:
        return (await self._request("GET", "/categories"))["data"]

    async def products(self, *, category: str | None = None, search: str | None = None,
                       purchasable: bool = True, sort: str = "price_asc",
                       limit: int = 20, offset: int = 0) -> dict:
        return await self._request("GET", "/products", params={
            "category": category, "search": search, "purchasable": str(purchasable).lower(),
            "sort": sort, "limit": limit, "offset": offset,
        })

    async def product(self, product_id: int) -> dict:
        return await self._request("GET", f"/products/{int(product_id)}")

    async def product_price(self, product_id: int) -> dict:
        return await self._request("GET", f"/products/{int(product_id)}/price")

    async def prices(self) -> dict:
        return await self._request("GET", "/prices")

    async def stars_price(self, quantity: int) -> dict:
        return await self._request("GET", "/stars/price", params={"quantity": quantity})

    async def premium_prices(self) -> list[dict]:
        return (await self._request("GET", "/premium/prices"))["data"]

    async def gifts(self) -> list[dict]:
        return (await self._request("GET", "/gifts"))["data"]

    async def quote(self, type_: str, *, quantity: int = 1, product_id: int | None = None) -> dict:
        body: dict[str, Any] = {"type": type_, "quantity": quantity}
        if product_id is not None:
            body["product_id"] = product_id
        return await self._request("POST", "/orders/quote", json=body)

    # ---------- سفارش ----------
    async def create_order(self, *, idempotency_key: str, type_: str = "product",
                           product_id: int | None = None, quantity: int = 1,
                           recipient: str | None = None, gift_message: str | None = None,
                           quote_id: str | None = None, pay_currency: str | None = None,
                           metadata: dict | None = None) -> dict:
        body: dict[str, Any] = {"type": type_, "quantity": quantity}
        for k, v in (("product_id", product_id), ("recipient", recipient),
                     ("gift_message", gift_message), ("quote_id", quote_id),
                     ("pay_currency", pay_currency), ("metadata", metadata)):
            if v is not None:
                body[k] = v
        return await self._request("POST", "/orders", json=body, idempotency_key=idempotency_key)

    async def get_order(self, ref: str) -> dict:
        return await self._request("GET", f"/orders/{ref}")

    async def list_orders(self, *, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        return await self._request("GET", "/orders", params={"status": status, "limit": limit, "offset": offset})

    async def cancel_order(self, ref: str) -> dict:
        return await self._request("POST", f"/orders/{ref}/cancel", idempotency_key=f"cancel-{ref}")

    async def simulate(self, ref: str, outcome: str) -> dict:
        """فقط با کلید sk_test_: processing | completed | failed | refunded"""
        return await self._request("POST", f"/test/orders/{ref}/simulate", json={"outcome": outcome},
                                   idempotency_key=f"sim-{ref}-{outcome}")

    # ---------- کیف پول ----------
    async def wallet(self) -> dict:
        return await self._request("GET", "/wallet")

    async def transactions(self, *, limit: int = 20, offset: int = 0) -> dict:
        return await self._request("GET", "/transactions", params={"limit": limit, "offset": offset})


def _parse_error(r: httpx.Response) -> StardError:
    try:
        body = r.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        e = body["error"]
        return StardError(r.status_code, e.get("code") or "unknown", e.get("message") or "",
                          type_=e.get("type") or "", param=e.get("param"), request_id=e.get("request_id"))
    if isinstance(body, dict) and "detail" in body:  # خطای اعتبارسنجی 422
        return StardError(r.status_code, "validation_error", str(body["detail"]))
    return StardError(r.status_code, "http_error", r.text[:200])
