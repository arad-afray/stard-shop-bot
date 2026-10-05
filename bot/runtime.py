"""ساخت و نگه‌داری همه‌ی سرویس‌های یک پردازه (پایگاه داده، Redis، صف، قفل، API، فروشگاه، worker، …)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .config import Settings
from .db import Database
from .locks import DistributedLock, RateLimiter
from .queue import JobQueue
from .shop import Shop
from .stard_api import StardClient

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    api: StardClient
    shop: Shop
    queue: JobQueue
    locks: DistributedLock
    limiter: RateLimiter
    redis: Any = None
    started_at: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)

    async def close(self) -> None:
        await self.api.close()
        if self.redis is not None:
            try:
                await self.redis.aclose()
            except Exception:
                pass
        await self.db.close()


async def build_services(settings: Settings, *, api: StardClient | None = None, db: Database | None = None) -> Services:
    if db is None:
        db = Database(settings.database_path,
                      url=settings.database_url.get_secret_value() if settings.database_url else None,
                      pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow)
        await db.connect()
    redis = None
    if settings.redis_url:
        import redis.asyncio as aioredis
        redis = aioredis.from_url(settings.redis_url.get_secret_value(), socket_timeout=5,
                                  socket_connect_timeout=5, health_check_interval=30)
        try:
            await redis.ping()
        except Exception as e:
            log.error("Redis unreachable (%s) — falling back to database for locks/rate limits", type(e).__name__)
            redis = None
    if api is None:
        api = StardClient(settings.stard_api_key.get_secret_value(), settings.stard_base_url,
                          timeout=settings.stard_timeout)
    queue = JobQueue(db, settings.instance_id)
    locks = DistributedLock(db, settings.instance_id)
    limiter = RateLimiter(db, redis)
    shop = Shop(db, api, default_profit=settings.default_profit_percent, pay_currency=settings.stard_pay_currency,
                queue=queue, locks=locks)
    return Services(settings=settings, db=db, api=api, shop=shop, queue=queue, locks=locks, limiter=limiter,
                    redis=redis)


class Supervisor:
    """اجرای کارهای پس‌زمینه با Restart خودکار.

    اگر یک کار کرش کند: خطا ثبت می‌شود، با فاصله‌ی افزایشی (۱، ۲، ۴ … حداکثر ۶۰ ثانیه) دوباره اجرا
    می‌شود و اگر در ۱۰ دقیقه بیش از ۵ بار کرش کند به مدیرها هشدار داده می‌شود (Restart Loop متوقف
    نمی‌شود ولی کند می‌شود تا منابع هدر نروند).
    """

    MAX_BACKOFF = 60
    ALERT_AFTER = 5
    WINDOW = 600

    def __init__(self, on_alert: Callable[[str, str], Awaitable[None]] | None = None):
        self.on_alert = on_alert
        self.tasks: dict[str, asyncio.Task] = {}
        self.factories: dict[str, Callable[[], Awaitable[None]]] = {}
        self.crashes: dict[str, list[float]] = {}
        self.restarts: dict[str, int] = {}
        self.status: dict[str, str] = {}

    def start(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        self.factories[name] = factory
        self.tasks[name] = asyncio.create_task(self._loop(name, factory), name=f"sup:{name}")

    def restart(self, name: str) -> None:
        """Restart یک سرویس گیرکرده (watchdog یا دکمه‌ی پنل)."""
        task = self.tasks.get(name)
        if task is not None:
            task.cancel()
        self.restarts[name] = self.restarts.get(name, 0) + 1
        self.tasks[name] = asyncio.create_task(self._loop(name, self.factories[name]), name=f"sup:{name}")

    async def _loop(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        delay = 1.0
        while True:
            self.status[name] = "running"
            started = time.monotonic()
            try:
                await factory()
                self.status[name] = "stopped"
                return
            except asyncio.CancelledError:
                self.status[name] = "stopped"
                raise
            except Exception as e:
                self.status[name] = "crashed"
                log.exception("service %s crashed — restarting in %.0fs", name, delay)
                t = time.monotonic()
                recent = [c for c in self.crashes.get(name, []) if t - c < self.WINDOW] + [t]
                self.crashes[name] = recent
                self.restarts[name] = self.restarts.get(name, 0) + 1
                if len(recent) >= self.ALERT_AFTER and self.on_alert is not None:
                    try:
                        await self.on_alert(f"service:{name}",
                                            f"سرویس {name} در ۱۰ دقیقه {len(recent)} بار کرش کرد: "
                                            f"{type(e).__name__}")
                    except Exception:
                        log.exception("alert failed")
            if time.monotonic() - started > 300:
                delay = 1.0  # مدتی سالم کار کرده بود؛ backoff از اول
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.MAX_BACKOFF)

    async def stop(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        for t in self.tasks.values():
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
