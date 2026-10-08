"""Worker: اجرای کارهای صف (ارسال/پیگیری سفارش، پیام همگانی، اعلان) و کارهای زمان‌بندی‌شده.

- هر نمونه با ROLE=worker یا ROLE=all یک JobWorker دارد؛ چند نمونه هم‌زمان امن‌اند (lease + SKIP LOCKED).
- هر کار timeout دارد؛ خطا → تلاش دوباره با backoff؛ تلاش‌ها تمام شد → dead (قابل Retry از پنل).
- پیگیری سفارش‌ها دیگر یک حلقه‌ی سریالی روی همه‌ی سفارش‌ها نیست: هر سفارش کار خودش را دارد و
  worker‌ها موازی کار می‌کنند، پس با زیاد شدن سفارش‌ها گلوگاه ایجاد نمی‌شود.
- کارهای دوره‌ای (بازیابی سفارش‌های گیرکرده، پاک‌سازی، مانیتورینگ، پشتیبان خودکار) با قفل توزیع‌شده
  فقط روی یک نمونه اجرا می‌شوند.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from . import notify
from .db import ACTIVE_STATUSES, Database, ago, ago_ms, now
from .locks import DistributedLock
from .logging_setup import correlation_id, new_correlation_id, service_name
from .queue import Job, JobQueue, PermanentError, RetryLater
from .shop import Shop
from .stard_api import FINAL_STATUSES, StardError

log = logging.getLogger(__name__)

# فاصله‌ی پیگیری سفارش بر حسب سن سفارش: اول سریع، بعد کندتر (کم کردن فشار روی API)
def poll_delay(age_seconds: float, base: int = 20) -> float:
    if age_seconds < 120:
        return 10
    if age_seconds < 900:
        return base
    if age_seconds < 3600:
        return 60
    return 300


STUCK_AFTER = {"new": 600, "pending": 3600, "processing": 3 * 3600, "manual": 6 * 3600}  # ثانیه


async def on_status_change(bot: Bot, shop: Shop, oid: int) -> None:
    """بعد از هر تغییر وضعیت: خبر به کاربر و کانال گزارش، و اگر انجام شد پاداش معرف."""
    await notify.order_changed(bot, shop.db, oid)
    o = await shop.db.get_order(oid)
    if o is not None and o["status"] == "completed":
        paid = await shop.after_complete(oid)
        if paid:
            await notify.referral_paid(bot, shop.db, *paid)


@dataclass
class Context:
    bot: Bot
    shop: Shop
    db: Database
    queue: JobQueue
    locks: DistributedLock
    admins: Any = None
    settings: Any = None
    metrics: Any = None
    extra: dict = field(default_factory=dict)


Handler = Callable[[Job, Context], Awaitable[None]]
HANDLERS: dict[str, tuple[Handler, float]] = {}  # kind → (تابع، timeout ثانیه)


def handler(kind: str, timeout: float = 60):
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = (fn, timeout)
        return fn
    return deco


# ---------- سفارش ----------
def _age(o) -> float:
    try:
        created = datetime.strptime(o["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 0.0
    return time.time() - created.timestamp()


@handler("order.submit", timeout=90)
async def h_order_submit(job: Job, ctx: Context) -> None:
    oid = int(job.payload["oid"])
    o = await ctx.db.get_order(oid)
    if o is None:
        return
    if o["status"] == "new":
        await ctx.shop.submit(oid)
        o = await ctx.db.get_order(oid)
        if o["refunded"]:
            await on_status_change(ctx.bot, ctx.shop, oid)
            return
        if o["status"] == "new":
            # پاسخ Stard نیامد (شبکه/5xx/429): دوباره با همان Idempotency-Key؛ دوبار خرید ممکن نیست
            raise RetryLater(min(15 * max(job.attempts, 1), 300), "submit not confirmed")
    if o["status"] in ACTIVE_STATUSES:
        # از اینجا به بعد همین کار پیگیری وضعیت را انجام می‌دهد (یک کار فعال برای هر سفارش)
        raise _Morph("order.sync", poll_delay(_age(o), _poll_base(ctx)))


@handler("order.sync", timeout=60)
async def h_order_sync(job: Job, ctx: Context) -> None:
    oid = int(job.payload["oid"])
    try:
        change = await ctx.shop.sync_order(oid)
    except StardError as e:
        if e.status == 404:
            raise PermanentError(f"order not found at Stard: {e.code}") from e
        raise
    if change:
        log.info("order %s: %s -> %s", oid, *change)
        await on_status_change(ctx.bot, ctx.shop, oid)
    o = await ctx.db.get_order(oid)
    if o is not None and o["status"] in ACTIVE_STATUSES:
        raise RetryLater(poll_delay(_age(o), _poll_base(ctx)), "still active")


def _poll_base(ctx: Context) -> int:
    return int(getattr(ctx.settings, "poll_interval_seconds", 20) or 20)


class _Morph(Exception):
    """تبدیل کار به نوع دیگر با همان dedupe_key (ارسال → پیگیری)."""

    def __init__(self, kind: str, delay: float):
        super().__init__(kind)
        self.kind, self.delay = kind, delay


# ---------- اعلان ----------
@handler("notify", timeout=30)
async def h_notify(job: Job, ctx: Context) -> None:
    try:
        await ctx.bot.send_message(job.payload["chat_id"], job.payload["text"], disable_web_page_preview=True)
    except TelegramRetryAfter as e:
        raise RetryLater(e.retry_after + 1, "telegram flood") from e
    except TelegramForbiddenError:
        await ctx.db.write("UPDATE users SET blocked = 1 WHERE id = :u", {"u": job.payload["chat_id"]})
    except TelegramBadRequest as e:
        raise PermanentError(str(e)) from e


# ---------- پیام همگانی ----------
BROADCAST_CHUNK = 200
BROADCAST_RATE = 20  # پیام در ثانیه؛ زیر سقف ۳۰ پیام در ثانیه‌ی تلگرام


@handler("broadcast", timeout=BROADCAST_CHUNK / BROADCAST_RATE * 6 + 60)
async def h_broadcast(job: Job, ctx: Context) -> None:
    """هر اجرا یک تکه (۲۰۰ کاربر) می‌فرستد و پیشرفت را ذخیره می‌کند؛ کرش وسط کار فقط همان تکه را
    از آخرین نقطه‌ی ذخیره‌شده ادامه می‌دهد."""
    bid = int(job.payload["bid"])
    b = await ctx.db.one("SELECT * FROM broadcasts WHERE id = :id", {"id": bid})
    if b is None or b["status"] in ("done", "cancelled"):
        return
    inactive = None
    if b["segment"].startswith("inactive:"):
        inactive = ago(int(b["segment"].split(":", 1)[1]))
    if b["status"] == "queued":
        total = await ctx.db.count_users(inactive_since=inactive)
        await ctx.db.write("UPDATE broadcasts SET status = 'running', total = :t WHERE id = :id",
                           {"t": total, "id": bid})
    ids = await ctx.db.user_ids_page(int(b["cursor"]), BROADCAST_CHUNK, inactive_since=inactive)
    sent = failed = blocked = 0
    cursor = int(b["cursor"])
    interval = 1 / BROADCAST_RATE
    for i, uid in enumerate(ids, 1):
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                await ctx.bot.copy_message(uid, b["from_chat"], b["message_id"])
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.5)
            except TelegramForbiddenError:
                blocked += 1
                await ctx.db.write("UPDATE users SET blocked = 1 WHERE id = :u", {"u": uid})
                break
            except TelegramBadRequest:
                failed += 1
                break
            except Exception as e:  # شبکه: یک بار دیگر
                if attempt == 2:
                    failed += 1
                    log.warning("broadcast %s to %s failed: %s", bid, uid, e)
                await asyncio.sleep(1)
        cursor = uid
        if i % 10 == 0:  # ذخیره‌ی پیشرفت؛ بعد از کرش حداکثر ۹ پیام تکراری می‌شود
            await _save_progress(ctx.db, bid, cursor, sent, failed, blocked)
            sent = failed = blocked = 0
            await ctx.queue.extend(job)
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))
    await _save_progress(ctx.db, bid, cursor, sent, failed, blocked)
    if len(ids) < BROADCAST_CHUNK:
        await ctx.db.write("UPDATE broadcasts SET status = 'done', finished_at = :t WHERE id = :id",
                           {"t": now(), "id": bid})
        b = await ctx.db.one("SELECT * FROM broadcasts WHERE id = :id", {"id": bid})
        await notify.safe_send(ctx.bot, b["admin_id"],
                               f"📢 پیام همگانی #{bid} تمام شد.\n✅ موفق: {b['sent']:,}\n"
                               f"⛔️ بلاک کرده‌اند: {b['blocked']:,}\n❌ ناموفق: {b['failed']:,}")
        return
    raise RetryLater(0.5, "next chunk")


async def _save_progress(db: Database, bid: int, cursor: int, sent: int, failed: int, blocked: int) -> None:
    await db.write("UPDATE broadcasts SET cursor = :c, sent = sent + :s, failed = failed + :f, "
                   "blocked = blocked + :b WHERE id = :id",
                   {"c": cursor, "s": sent, "f": failed, "b": blocked, "id": bid})


async def start_broadcast(db: Database, queue: JobQueue, *, admin_id: int, from_chat: int, message_id: int,
                          segment: str = "all") -> int:
    async with db.tx() as c:
        from sqlalchemy import text
        bid = (await c.execute(text(
            "INSERT INTO broadcasts(admin_id, from_chat, message_id, segment, status, created_at) "
            "VALUES(:a, :f, :m, :s, 'queued', :t) RETURNING id"),
            {"a": admin_id, "f": from_chat, "m": message_id, "s": segment, "t": now()})).scalar_one()
        await queue.enqueue("broadcast", {"bid": bid}, dedupe_key=f"broadcast:{bid}", max_attempts=20, c=c)
        await db.audit_in(c, admin_id=admin_id, action="broadcast", ref=f"broadcast:{bid}", after={"segment": segment})
    return bid


# ---------- اجراکننده ----------
class JobWorker:
    def __init__(self, ctx: Context, *, concurrency: int = 8, poll: float = 1.0):
        self.ctx = ctx
        self.concurrency = max(1, concurrency)
        self.poll = poll
        self._running: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self.processed = 0
        self.failed = 0
        self.last_loop = time.monotonic()

    async def run_once(self) -> int:
        """یک دور: گرفتن کارهای آماده و اجرای کامل آن‌ها (برای تست‌ها و اجرای دستی)."""
        jobs = await self.ctx.queue.claim(self.concurrency)
        await asyncio.gather(*(self._execute(j) for j in jobs))
        return len(jobs)

    async def run(self) -> None:
        service_name.set("worker")
        log.info("job worker started (concurrency=%s)", self.concurrency)
        while not self._stop.is_set():
            self.last_loop = time.monotonic()
            free = self.concurrency - len(self._running)
            try:
                jobs = await self.ctx.queue.claim(free) if free > 0 else []
            except Exception:
                log.exception("claim failed")
                jobs = []
            for j in jobs:
                t = asyncio.create_task(self._execute(j))
                self._running.add(t)
                t.add_done_callback(self._running.discard)
            if not jobs:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), self.poll * random.uniform(0.8, 1.2))
        if self._running:
            await asyncio.wait(self._running, timeout=30)

    def stop(self) -> None:
        self._stop.set()

    async def _execute(self, job: Job) -> None:
        q = self.ctx.queue
        token = correlation_id.set(f"job-{job.id}")
        started = time.perf_counter()
        try:
            entry = HANDLERS.get(job.kind)
            if entry is None:
                await q.fail(job, f"unknown job kind {job.kind}", permanent=True)
                return
            fn, timeout = entry
            try:
                await asyncio.wait_for(fn(job, self.ctx), timeout)
            except _Morph as m:
                await q.requeue(job, m.delay, kind=m.kind)
            except RetryLater as r:
                await q.requeue(job, r.delay, reset_attempts=True)
            except PermanentError as e:
                self.failed += 1
                await q.fail(job, f"permanent: {e}", permanent=True)
                log.error("job %s %s dead: %s", job.id, job.kind, e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.failed += 1
                err = f"{type(e).__name__}: {e}"
                state = await q.fail(job, err)
                (log.error if state == "dead" else log.warning)(
                    "job %s %s failed (attempt %s/%s) → %s: %s", job.id, job.kind, job.attempts, job.max_attempts,
                    state, err)
            else:
                await q.complete(job)
            self.processed += 1
        except Exception:
            log.exception("job %s bookkeeping failed", job.id)
        finally:
            if self.ctx.metrics is not None:
                self.ctx.metrics.observe_job(job.kind, time.perf_counter() - started)
            correlation_id.reset(token)


# ---------- کارهای دوره‌ای (یک نمونه با قفل) ----------
Periodic = Callable[[Context], Awaitable[None]]
PERIODIC: list[tuple[str, float, Periodic]] = []  # (نام، فاصله ثانیه، تابع)


def periodic(name: str, every: float):
    def deco(fn: Periodic) -> Periodic:
        PERIODIC.append((name, every, fn))
        return fn
    return deco


@periodic("recover_orders", 60)
async def recover_orders(ctx: Context) -> None:
    """سفارش‌های بدون کار فعال (مثلاً از نسخه‌ی قبل، یا بعد از پاک شدن صف) دوباره در صف قرار می‌گیرند."""
    for o in await ctx.db.active_orders():
        kind = "order.submit" if o["status"] == "new" else "order.sync"
        if await ctx.queue.enqueue(kind, {"oid": o["id"]}, dedupe_key=f"order:{o['id']}"):
            log.info("recovered order %s (%s) into queue as %s", o["id"], o["status"], kind)


async def stuck_orders(db: Database) -> list[dict]:
    out = []
    for status, secs in STUCK_AFTER.items():
        out += await db.all("SELECT * FROM orders WHERE status = :s AND updated_at < :t ORDER BY id LIMIT 50",
                            {"s": status, "t": ago(seconds=secs)})
    return out


@periodic("housekeeping", 3600)
async def housekeeping(ctx: Context) -> None:
    n = await ctx.queue.purge(7)
    await ctx.db.write("DELETE FROM locks WHERE expires_at < :t", {"t": ago_ms(86400)})
    await ctx.db.write("DELETE FROM risk_events WHERE created_at < :t", {"t": ago(90)})
    await ctx.db.write("DELETE FROM heartbeats WHERE last_seen < :t", {"t": ago_ms(7 * 86400)})
    from .locks import RateLimiter
    await RateLimiter(ctx.db).purge()
    if n:
        log.info("housekeeping: purged %s done jobs", n)


class Scheduler:
    def __init__(self, ctx: Context, tasks: list[tuple[str, float, Periodic]] | None = None):
        self.ctx = ctx
        self.tasks = tasks if tasks is not None else PERIODIC
        self._last: dict[str, float] = {}
        self._stop = asyncio.Event()
        self.last_loop = time.monotonic()

    async def tick(self) -> None:
        for name, every, fn in self.tasks:
            # اولین tick همیشه اجرا می‌شود (monotonic بعد از روشن شدن سیستم از صفر شروع می‌شود)
            last = self._last.get(name)
            if last is not None and time.monotonic() - last < every:
                continue
            self._last[name] = time.monotonic()
            # فقط یک نمونه: قفل به اندازه‌ی فاصله‌ی اجرا (منهای کمی) نگه داشته می‌شود و آزاد نمی‌شود
            if not await self.ctx.locks.acquire(f"periodic:{name}", max(every * 0.9, 5)):
                continue
            new_correlation_id(f"{name}-")
            try:
                await asyncio.wait_for(fn(self.ctx), timeout=max(every, 120))
            except Exception:
                log.exception("periodic task %s failed", name)

    async def run(self) -> None:
        service_name.set("scheduler")
        while not self._stop.is_set():
            self.last_loop = time.monotonic()
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), 5)

    def stop(self) -> None:
        self._stop.set()


__all__ = ["JobWorker", "Scheduler", "Context", "on_status_change", "start_broadcast", "stuck_orders",
           "poll_delay", "HANDLERS", "PERIODIC", "periodic", "handler", "FINAL_STATUSES"]
