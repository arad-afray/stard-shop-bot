"""قفل توزیع‌شده و محدودیت نرخ — روی پایگاه داده (همیشه) یا Redis (اگر REDIS_URL باشد).

- DistributedLock: قفل با TTL؛ اگر نمونه‌ی صاحب قفل کرش کند، قفل بعد از TTL آزاد می‌شود.
  برای: اجرای یک‌باره‌ی کارهای زمان‌بندی‌شده، ارسال سفارش، به‌روزرسانی، پشتیبان‌گیری، مهاجرت.
- RateLimiter: پنجره‌ی ثابت؛ بین همه‌ی نمونه‌ها مشترک است.
"""
from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from sqlalchemy import text

from .db import Database, later_ms as later, now_ms as now

log = logging.getLogger(__name__)


class LockNotAcquired(Exception):
    pass


class DistributedLock:
    def __init__(self, db: Database, owner: str):
        self.db = db
        self.owner = owner[:64]

    async def acquire(self, name: str, ttl: float) -> bool:
        """قفل را می‌گیرد اگر آزاد یا منقضی یا مال خودمان باشد (تمدید)."""
        async with self.db.tx() as c:
            r = await c.execute(text(
                "INSERT INTO locks(name, owner, expires_at) VALUES(:n, :o, :e) "
                "ON CONFLICT(name) DO UPDATE SET owner = excluded.owner, expires_at = excluded.expires_at "
                "WHERE locks.expires_at < :t OR locks.owner = :o"),
                {"n": name, "o": self.owner, "e": later(ttl), "t": now()})
            return r.rowcount == 1

    async def release(self, name: str) -> None:
        await self.db.write("DELETE FROM locks WHERE name = :n AND owner = :o", {"n": name, "o": self.owner})

    async def holder(self, name: str) -> dict | None:
        return await self.db.one("SELECT owner, expires_at FROM locks WHERE name = :n AND expires_at >= :t",
                                 {"n": name, "t": now()})

    @contextlib.asynccontextmanager
    async def hold(self, name: str, ttl: float):
        if not await self.acquire(name, ttl):
            raise LockNotAcquired(name)
        try:
            yield
        finally:
            await self.release(name)


class RateLimiter:
    """hit(key, limit, window) → True اگر مجاز. با Redis سریع‌تر؛ بدون آن روی پایگاه داده."""

    def __init__(self, db: Database, redis: Any = None):
        self.db = db
        self.redis = redis

    async def hit(self, key: str, limit: int, window: int) -> bool:
        return (await self.count(key, window)) <= limit

    async def count(self, key: str, window: int) -> int:
        win = int(time.time() // window) * window  # شروع پنجره (epoch)
        if self.redis is not None:
            try:
                rk = f"rl:{key}:{win}"
                pipe = self.redis.pipeline()
                pipe.incr(rk)
                pipe.expire(rk, window + 5)
                n, _ = await pipe.execute()
                return int(n)
            except Exception as e:  # Redis در دسترس نیست → پایگاه داده
                log.warning("redis rate limit failed, falling back to DB: %s", e)
        async with self.db.tx() as c:
            r = await c.execute(text(
                'INSERT INTO rate_limits(key, "window", count) VALUES(:k, :w, 1) '
                'ON CONFLICT(key) DO UPDATE SET count = CASE WHEN rate_limits."window" = excluded."window" '
                'THEN rate_limits.count + 1 ELSE 1 END, "window" = excluded."window" RETURNING count'),
                {"k": key[:128], "w": win})
            return int(r.scalar_one())

    async def purge(self, older_than: int = 2 * 86400) -> None:
        """ردیف‌هایی که پنجره‌شان بیش از older_than ثانیه پیش شروع شده پاک می‌شوند."""
        await self.db.write('DELETE FROM rate_limits WHERE "window" < :w', {"w": int(time.time()) - older_than})


# سیاست‌های محدودیت نرخ: (حداکثر، پنجره بر حسب ثانیه)
LIMITS = {
    "purchase": (10, 60),          # تأیید خرید
    "topup": (5, 3600),            # ثبت رسید شارژ
    "coupon": (10, 600),           # امتحان کد تخفیف
    "admin": (120, 60),            # کارهای مدیر
    "admin_refund": (30, 60),
    "broadcast": (3, 3600),
    "reward": (5, 60),
    "api": (120, 60),              # API خود ربات، برای هر کلید
    "api_auth_fail": (20, 60),     # تلاش ناموفق احراز هویت API، برای هر IP
}


async def allow(limiter: RateLimiter | None, action: str, subject: Any) -> bool:
    if limiter is None:
        return True
    limit, window = LIMITS[action]
    return await limiter.hit(f"{action}:{subject}", limit, window)
