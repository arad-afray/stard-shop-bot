"""مانیتورینگ: heartbeat سرویس‌ها، Health Check، متریک‌های کسب‌وکار و هشدار.

- هر نمونه هر ۱۵ ثانیه برای سرویس‌هایش (bot / worker / scheduler) heartbeat می‌نویسد؛ پس وضعیت همه‌ی
  نمونه‌ها از هر جایی (پنل، /readyz) دیده می‌شود.
- هشدارها «لبه‌ای»اند: وقتی مشکلی شروع می‌شود یک پیام، تا وقتی ادامه دارد هر ۳۰ دقیقه یادآوری، و وقتی
  برطرف شد پیام «برطرف شد». وضعیت هشدار در پایگاه داده است تا چند نمونه هشدار تکراری نفرستند.
- اگر خود پایگاه داده از دسترس خارج شود، هشدار «Database» مستقیم (بدون پایگاه داده) فرستاده می‌شود.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot

from . import __version__, notify
from .db import Database, ago, ago_ms, now, now_ms
from .metrics import Metrics, human_bytes, human_duration, system_snapshot
from .worker import Context, periodic, stuck_orders

log = logging.getLogger(__name__)

HEARTBEAT_EVERY = 15
HEARTBEAT_STALE = 90     # ثانیه؛ بعد از این مدت سرویس «از کار افتاده» حساب می‌شود
REMIND_EVERY = 1800      # یادآوری هشدار فعال

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"
ICON = {OK: "🟢", WARN: "🟡", FAIL: "🔴", OFF: "⚪️"}


# ---------- heartbeat ----------
async def beat(db: Database, service: str, instance: str, started_at: str, info: dict | None = None) -> None:
    await db.write(
        "INSERT INTO heartbeats(service, instance, last_seen, started_at, info) VALUES(:s, :i, :t, :st, :info) "
        "ON CONFLICT(service, instance) DO UPDATE SET last_seen = excluded.last_seen, info = excluded.info",
        {"s": service, "i": instance[:64], "t": now_ms(), "st": started_at,
         "info": json.dumps(info or {}, ensure_ascii=False, default=str)})


async def services_status(db: Database) -> dict[str, list[dict]]:
    """سرویس → فهرست نمونه‌ها با سن heartbeat."""
    rows = await db.all("SELECT * FROM heartbeats WHERE last_seen >= :t ORDER BY service, instance",
                        {"t": ago_ms(86400)})
    out: dict[str, list[dict]] = {}
    for r in rows:
        age = _age_ms(r["last_seen"])
        try:
            info = json.loads(r["info"] or "{}")
        except ValueError:
            info = {}
        out.setdefault(r["service"], []).append({"instance": r["instance"], "age": age, "alive": age < HEARTBEAT_STALE,
                                                 "started_at": r["started_at"], "info": info})
    return out


def _age_ms(v: str) -> float:
    try:
        dt = datetime.strptime(v.rstrip("Z")[:23], "%Y-%m-%dT%H:%M:%S.%f" if "." in v else "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 1e9
    return time.time() - dt.replace(tzinfo=timezone.utc).timestamp()


async def heartbeat_loop(ctx: Context, roles: list[str], started_at: str) -> None:
    while True:
        info = {"version": __version__, "pid": os.getpid()}
        w = ctx.extra.get("worker")
        if w is not None:
            info.update(processed=w.processed, failed=w.failed, running=len(w._running))
        m: Metrics | None = ctx.metrics
        if m is not None:
            info.update(rss=system_snapshot().get("proc_rss"), errors=m.counters.get("errors_total", 0))
        for role in roles:
            try:
                await beat(ctx.db, role, ctx.settings.instance_id if ctx.settings else "local", started_at, info)
            except Exception as e:
                log.warning("heartbeat failed: %s", type(e).__name__)
        await asyncio.sleep(HEARTBEAT_EVERY)


# ---------- متریک‌های کسب‌وکار (از پایگاه داده؛ درست برای چند نمونه) ----------
async def business_metrics(db: Database) -> dict[str, Any]:
    r = await db.one(
        """SELECT
             (SELECT COUNT(*) FROM orders WHERE created_at >= :m5) AS orders_5m,
             (SELECT COUNT(*) FROM orders WHERE created_at >= :h1) AS orders_1h,
             (SELECT COUNT(*) FROM orders WHERE created_at >= :h1 AND refunded = 1) AS refunded_1h,
             (SELECT COUNT(*) FROM orders WHERE created_at >= :d1 AND refunded = 1) AS failed_24h,
             (SELECT COUNT(*) FROM orders WHERE created_at >= :d1) AS orders_24h,
             (SELECT COUNT(*) FROM orders WHERE status IN ('new', 'pending', 'processing', 'manual')) AS open_orders""",
        {"m5": ago(seconds=300), "h1": ago(seconds=3600), "d1": ago(1)})
    r["orders_per_min"] = round(r["orders_5m"] / 5, 2)
    r["refund_rate_1h"] = round(r["refunded_1h"] / r["orders_1h"] * 100, 1) if r["orders_1h"] else 0.0
    return r


# ---------- Health Check ----------
@dataclass
class Check:
    name: str
    status: str
    latency_ms: float | None = None
    detail: str = ""
    error: str = ""
    checked_at: str = field(default_factory=now)


async def _timed(coro, timeout: float = 10):
    t = time.perf_counter()
    res = await asyncio.wait_for(coro, timeout)
    return res, round((time.perf_counter() - t) * 1000, 1)


async def run_health_check(ctx: Context) -> list[Check]:
    checks: list[Check] = []
    s = ctx.settings

    async def check(name: str, fn):
        try:
            checks.append(await fn())
        except Exception as e:
            checks.append(Check(name, FAIL, error=_short(e)))

    async def bot_check():
        me, ms = await _timed(ctx.bot.get_me())
        st = await services_status(ctx.db)
        alive = [i for i in st.get("bot", []) if i["alive"]]
        status = OK if (alive or (s and s.role == "worker")) else WARN
        return Check("Bot", status, ms, f"@{me.username} | نمونه‌های فعال: {len(alive)}")

    async def api_check():
        ping, ms = await _timed(ctx.shop.api.ping())
        status = OK if ms < 3000 else WARN
        return Check("API", status, ms, f"Stard {ping.get('environment', '')}")

    async def db_check():
        _, ms = await _timed(ctx.db.ping())
        return Check("Database", OK if ms < 500 else WARN, ms, ctx.db.dialect)

    async def worker_check():
        st = await services_status(ctx.db)
        alive = [i for i in st.get("worker", []) if i["alive"]]
        if not alive:
            return Check("Worker", FAIL, None, error="هیچ worker فعالی heartbeat نفرستاده است")
        return Check("Worker", OK, None, f"{len(alive)} نمونه | آخرین: {min(i['age'] for i in alive):.0f}s پیش")

    async def redis_check():
        r = ctx.extra.get("redis")
        if r is None:
            return Check("Redis", OFF, None, "تنظیم نشده (فقط برای چند نمونه لازم است)")
        _, ms = await _timed(r.ping())
        return Check("Redis", OK, ms)

    async def queue_check():
        q, ms = await _timed(ctx.queue.stats())
        status = OK
        if q["lag_seconds"] > 120 or q["stale"]:
            status = WARN
        if q["lag_seconds"] > 600:
            status = FAIL
        return Check("Queue", status, ms, f"صف: {q['queued']} | در حال اجرا: {q['running']} | dead: {q['dead']} | "
                                          f"تأخیر: {q['lag_seconds']:.0f}s")

    async def storage_check():
        snap = system_snapshot(os.path.dirname(os.path.abspath(ctx.db.path)) if ctx.db.is_sqlite else ".")
        free_pct = 100 - snap.get("disk_percent", 0)
        dirs = [d for d in (getattr(s, "log_dir", "logs"), getattr(s, "backup_dir", "backups")) if d]
        not_writable = [d for d in dirs if os.path.exists(d) and not os.access(d, os.W_OK)]
        status = OK if free_pct > 15 and not not_writable else (WARN if free_pct > 5 and not not_writable else FAIL)
        return Check("Storage", status, None, f"آزاد: {human_bytes(snap.get('disk_free'))} ({free_pct:.0f}%)",
                     error=("غیرقابل نوشتن: " + ", ".join(not_writable)) if not_writable else "")

    async def external_check():
        st, ms = await _timed(ctx.shop.api.status())
        bad = [k for k, v in (st.get("services") or {}).items() if v != "operational"]
        return Check("External Services", OK if not bad else WARN, ms,
                     "Stard: همه سالم" if not bad else "Stard: " + ", ".join(bad))

    await asyncio.gather(*(check(n, f) for n, f in (
        ("Bot", bot_check), ("API", api_check), ("Database", db_check), ("Worker", worker_check),
        ("Redis", redis_check), ("Queue", queue_check), ("Storage", storage_check),
        ("External Services", external_check))))
    order = ["Bot", "API", "Database", "Worker", "Redis", "Queue", "Storage", "External Services"]
    checks.sort(key=lambda c: order.index(c.name) if c.name in order else 99)
    try:
        await ctx.db.set_json("health:last", [asdict(c) for c in checks])
    except Exception:
        pass
    return checks


def _short(e: BaseException) -> str:
    from .logging_setup import redact
    msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    return redact(msg)[:200]


CHECK_LABELS = {"Bot": "ربات", "API": "API استارد", "Database": "پایگاه داده", "Worker": "پردازشگر صف",
                "Redis": "Redis", "Queue": "صف کارها", "Storage": "فضای ذخیره", "External Services": "سرویس‌های بیرونی"}


def check_label(name: str) -> str:
    return CHECK_LABELS.get(name, name)


def format_checks(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lat = f" | {c.latency_ms:.0f}ms" if c.latency_ms is not None else ""
        lines.append(f"{ICON.get(c.status, '⚪️')} <b>{check_label(c.name)}</b>{lat}"
                     + (f"\n   {c.detail}" if c.detail else "")
                     + (f"\n   ⚠️ {c.error}" if c.error else "")
                     + f"\n   🕒 {c.checked_at[11:19]} UTC")
    return "\n".join(lines)


# ---------- هشدار ----------
ALERT_TEXT = {
    "bot_down": "🤖 ربات (دریافت پیام) پاسخ نمی‌دهد",
    "worker_down": "⚙️ worker فعال نیست؛ سفارش‌ها پیگیری نمی‌شوند",
    "api_down": "🌐 Stard API در دسترس نیست",
    "db_error": "🗄 خطای پایگاه داده",
    "disk_high": "💽 دیسک نزدیک پر شدن است",
    "memory_high": "🧠 مصرف حافظه بالاست",
    "error_spike": "📈 افزایش شدید خطاها",
    "stuck_orders": "⏳ سفارش‌های گیرکرده",
    "refund_spike": "↩️ افزایش نرخ برگشت پول",
    "api_latency": "🐢 تأخیر بالای Stard API",
    "queue_lag": "📬 صف کارها عقب افتاده است",
    "dead_jobs": "☠️ کارهای متوقف‌شده زیاد شده‌اند",
    "wallet_low": "🏦 موجودی کیف پول Stard کم است",
}

DEFAULT_THRESHOLDS = {
    "disk_percent": 90, "memory_percent": 90, "errors_5m": 20, "refund_rate": 30, "refund_min_orders": 5,
    "api_p95_ms": 3000, "queue_lag": 300, "dead_jobs": 10, "wallet_min": 0,
}


class AlertManager:
    def __init__(self, db: Database, bot: Bot, admins: Any):
        self.db = db
        self.bot = bot
        self.admins = admins
        self._mem: dict[str, float] = {}   # وقتی پایگاه داده در دسترس نیست

    async def thresholds(self) -> dict[str, float]:
        return {**DEFAULT_THRESHOLDS, **(await self.db.get_json("alert_thresholds", {}) or {})}

    async def enabled(self, key: str) -> bool:
        off = await self.db.get_json("alerts_off", []) or []
        return key not in off

    async def fire(self, key: str, active: bool, detail: str = "") -> bool:
        """وضعیت یک هشدار را اعمال می‌کند. True اگر پیامی فرستاده شد."""
        title = ALERT_TEXT.get(key, key)
        if key == "db_error":
            return await self._fire_without_db(key, active, title, detail)
        row = await self.db.one("SELECT * FROM alert_state WHERE key = :k", {"k": key})
        was = bool(row and row["active"])
        t = now()
        if active:
            due = not was or (row["last_sent"] or "") < ago(seconds=REMIND_EVERY)
            if not due:
                return False
            # فقط یک نمونه پیام بفرستد: به‌روزرسانی شرطی روی last_sent
            if row is None:
                n = await self.db.write("INSERT INTO alert_state(key, active, last_sent, count) VALUES(:k, 1, :t, 1) "
                                        "ON CONFLICT(key) DO NOTHING", {"k": key, "t": t})
            else:
                n = await self.db.write("UPDATE alert_state SET active = 1, last_sent = :t, count = count + 1 "
                                        "WHERE key = :k AND (active = 0 OR last_sent IS NULL OR last_sent = :old)",
                                        {"k": key, "t": t, "old": row["last_sent"]})
            if n != 1 or not await self.enabled(key):
                return False
            prefix = "🚨 <b>هشدار</b>" if not was else "🔁 <b>هشدار ادامه دارد</b>"
            await notify.to_admins(self.bot, self.admins, f"{prefix}\n{title}\n{detail}".strip())
            log.warning("ALERT %s: %s", key, detail)
            return True
        if was:
            n = await self.db.write("UPDATE alert_state SET active = 0 WHERE key = :k AND active = 1", {"k": key})
            if n == 1 and await self.enabled(key):
                await notify.to_admins(self.bot, self.admins, f"✅ <b>برطرف شد</b>\n{title}")
                log.info("RESOLVED %s", key)
                return True
        return False

    async def _fire_without_db(self, key: str, active: bool, title: str, detail: str) -> bool:
        last = self._mem.get(key)
        if active and (last is None or time.time() - last > REMIND_EVERY):
            self._mem[key] = time.time()
            await notify.to_admins(self.bot, self.admins, f"🚨 <b>هشدار</b>\n{title}\n{detail}".strip())
            log.error("ALERT %s: %s", key, detail)
            return True
        if not active and last is not None:
            self._mem.pop(key, None)
            await notify.to_admins(self.bot, self.admins, f"✅ <b>برطرف شد</b>\n{title}")
            return True
        return False

    async def active(self) -> list[str]:
        return [r["key"] for r in await self.db.all("SELECT key FROM alert_state WHERE active = 1")] + list(self._mem)


async def evaluate_alerts(ctx: Context, am: AlertManager) -> dict[str, bool]:
    """همه‌ی قوانین هشدار را یک بار ارزیابی می‌کند. خروجی: کلید → فعال؟"""
    results: dict[str, tuple[bool, str]] = {}
    try:
        await ctx.db.ping()
    except Exception as e:
        await am.fire("db_error", True, _short(e))
        return {"db_error": True}
    await am.fire("db_error", False)
    th = await am.thresholds()

    st = await services_status(ctx.db)
    for svc, key in (("bot", "bot_down"), ("worker", "worker_down")):
        seen = st.get(svc, [])
        # فقط اگر این سرویس قبلاً جایی اجرا شده باشد (نصب ROLE=worker تنها، bot ندارد)
        results[key] = (bool(seen) and not any(i["alive"] for i in seen),
                        f"آخرین heartbeat: {min((i['age'] for i in seen), default=0):.0f} ثانیه پیش" if seen else "")

    m: Metrics | None = ctx.metrics
    try:
        _, ms = await _timed(ctx.shop.api.ping(), 15)
        results["api_down"] = (False, "")
        p95 = (m.api_latency.pct(95) * 1000) if m is not None and m.api_latency.pct(95) is not None else ms
        results["api_latency"] = (p95 > th["api_p95_ms"], f"p95: {p95:.0f}ms")
    except Exception as e:
        fails = ctx.extra.get("_api_fail", 0) + 1
        ctx.extra["_api_fail"] = fails
        results["api_down"] = (fails >= 2, _short(e))  # دو شکست پشت سر هم
    else:
        ctx.extra["_api_fail"] = 0

    snap = system_snapshot(".")
    results["disk_high"] = (snap.get("disk_percent", 0) >= th["disk_percent"],
                            f"مصرف: {snap.get('disk_percent')}% | آزاد: {human_bytes(snap.get('disk_free'))}")
    if "mem_percent" in snap:
        results["memory_high"] = (snap["mem_percent"] >= th["memory_percent"], f"RAM: {snap['mem_percent']}%")
    if m is not None:
        errs = m.rate("errors_total") + m.rate("api_errors_total")
        results["error_spike"] = (errs >= th["errors_5m"], f"{errs} خطا در ۵ دقیقه‌ی اخیر")

    stuck = await stuck_orders(ctx.db)
    results["stuck_orders"] = (bool(stuck), f"{len(stuck)} سفارش: " + ", ".join(f"#{o['id']}" for o in stuck[:10]))
    bm = await business_metrics(ctx.db)
    results["refund_spike"] = (bm["orders_1h"] >= th["refund_min_orders"] and bm["refund_rate_1h"] >= th["refund_rate"],
                               f"{bm['refund_rate_1h']}% از {bm['orders_1h']} سفارش در یک ساعت اخیر")
    q = await ctx.queue.stats()
    results["queue_lag"] = (q["lag_seconds"] >= th["queue_lag"], f"تأخیر: {human_duration(q['lag_seconds'])}")
    results["dead_jobs"] = (q["dead"] >= th["dead_jobs"], f"{q['dead']} کار dead")
    if th.get("wallet_min", 0) > 0:
        try:
            w = await ctx.shop.api.wallet()
            irt = next((b["amount"] for b in w.get("balances", []) if b.get("currency") == "IRT"), None)
            if isinstance(irt, (int, float)):
                results["wallet_low"] = (irt < th["wallet_min"], f"موجودی: {int(irt):,} تومان")
        except Exception:
            pass

    for key, (active, detail) in results.items():
        try:
            await am.fire(key, active, detail)
        except Exception:
            log.exception("alert %s failed", key)
    return {k: v[0] for k, v in results.items()}


@periodic("monitor", 60)
async def monitor_task(ctx: Context) -> None:
    am = ctx.extra.get("alerts")
    if am is None:
        am = ctx.extra["alerts"] = AlertManager(ctx.db, ctx.bot, ctx.admins)
    await evaluate_alerts(ctx, am)
