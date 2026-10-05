"""سرور HTTP داخلی (aiohttp).

مسیرها:
  GET  /healthz            زنده بودن پردازه (بدون احراز هویت؛ برای Docker/Kubernetes)
  GET  /readyz             آماده بودن: پایگاه داده + (برای worker) heartbeat تازه
  GET  /metrics            Prometheus — کلید با دسترسی metrics:read، یا از 127.0.0.1
  GET  /api/v1/health      نتیجه‌ی Health Check — health:read
  GET  /api/v1/stats       آمار فروش — stats:read
  POST /webhooks/stard     وب‌هوک Stard با امضای HMAC-SHA256 (STARD_WEBHOOK_SECRET)

پیش‌فرض فقط روی 127.0.0.1 گوش می‌دهد. برای وب‌هوک، HTTP_HOST=0.0.0.0 و یک reverse proxy با HTTPS بگذارید.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

from aiohttp import web

from . import __version__, apikeys
from .db import now
from .locks import allow
from .logging_setup import correlation_id, new_correlation_id
from .metrics import prometheus, system_snapshot
from .monitor import business_metrics, services_status

log = logging.getLogger(__name__)

WEBHOOK_TOLERANCE = 300  # ثانیه؛ درخواست قدیمی‌تر رد می‌شود (ضد replay)


def verify_signature(raw: bytes, header: str | None, secret: str, *, now_ts: float | None = None) -> bool:
    """Stard-Signature: t=<unix>,v1=<hex(HMAC-SHA256(secret, f"{t}.{raw}"))>"""
    if not header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        t = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs((now_ts or time.time()) - t) > WEBHOOK_TOLERANCE:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


def _client_ip(request: web.Request) -> str:
    return request.remote or "unknown"


def _is_local(request: web.Request) -> bool:
    return _client_ip(request) in ("127.0.0.1", "::1")


def build_app(services: Any) -> web.Application:
    app = web.Application(client_max_size=256 * 1024)

    @web.middleware
    async def mw(request: web.Request, handler):
        token = correlation_id.set(request.headers.get("X-Request-Id", "")[:32] or new_correlation_id("h"))
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            log.exception("http %s %s failed", request.method, request.path)
            return web.json_response({"error": "internal_error"}, status=500)  # بدون جزئیات (افشای اطلاعات)
        finally:
            correlation_id.reset(token)

    app.middlewares.append(mw)

    async def auth(request: web.Request, scope: str, *, allow_local: bool = False) -> web.Response | None:
        if allow_local and _is_local(request):
            return None
        key = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or None
        row = await apikeys.verify(services.db, key, scope)
        if row is None:
            if not await allow(services.limiter, "api_auth_fail", _client_ip(request)):
                return web.json_response({"error": "too_many_requests"}, status=429)
            return web.json_response({"error": "unauthorized"}, status=401)
        if not await allow(services.limiter, "api", row["id"]):
            return web.json_response({"error": "rate_limited"}, status=429)
        return None

    async def healthz(_):
        return web.json_response({"status": "ok", "version": __version__, "uptime": time.time() - services.started_at})

    async def readyz(_):
        try:
            await services.db.ping()
        except Exception:
            return web.json_response({"status": "db_unavailable"}, status=503)
        role = services.settings.role
        if role in ("all", "worker"):
            st = await services_status(services.db)
            if not any(i["alive"] for i in st.get("worker", [])):
                return web.json_response({"status": "worker_not_ready"}, status=503)
        return web.json_response({"status": "ready", "role": role})

    async def metrics(request):
        if (err := await auth(request, "metrics:read", allow_local=True)) is not None:
            return err
        m = services.extra.get("metrics")
        bm = await business_metrics(services.db)
        q = await services.queue.stats()
        snap = system_snapshot(".")
        extra = {
            "orders_per_min": bm["orders_per_min"], "refund_rate_1h_percent": bm["refund_rate_1h"],
            "orders_failed_24h": bm["failed_24h"], "orders_open": bm["open_orders"],
            "queue_length": q["queued"], "queue_running": q["running"], "queue_dead": q["dead"],
            "queue_lag_seconds": q["lag_seconds"],
            "cpu_percent": snap.get("cpu_percent"), "memory_percent": snap.get("mem_percent"),
            "disk_percent": snap.get("disk_percent"), "process_rss_bytes": snap.get("proc_rss"),
            "net_sent_bytes": snap.get("net_sent"), "net_recv_bytes": snap.get("net_recv"),
        }
        st = await services_status(services.db)
        for svc in ("bot", "worker", "scheduler"):
            extra[f"service_up_{svc}"] = 1 if any(i["alive"] for i in st.get(svc, [])) else 0
        body = prometheus(m, {k: v for k, v in extra.items() if v is not None}) if m else ""
        return web.Response(text=body, content_type="text/plain")

    async def api_health(request):
        if (err := await auth(request, "health:read")) is not None:
            return err
        return web.json_response({"checks": await services.db.get_json("health:last", []),
                                  "services": await services_status(services.db)})

    async def api_stats(request):
        if (err := await auth(request, "stats:read")) is not None:
            return err
        return web.json_response({**await services.db.stats(), **await business_metrics(services.db)})

    async def stard_webhook(request: web.Request):
        secret = services.settings.stard_webhook_secret
        raw = await request.read()
        event_id = request.headers.get("Stard-Event-Id", "")[:64]
        if secret is None:
            return web.json_response({"error": "webhooks_disabled"}, status=404)
        if not verify_signature(raw, request.headers.get("Stard-Signature"), secret.get_secret_value()):
            log.warning("webhook rejected: bad signature from %s", _client_ip(request))
            await _record(services.db, event_id or f"bad-{time.time_ns()}", "?", "rejected", "bad signature", None)
            return web.json_response({"error": "invalid_signature"}, status=400)
        try:
            event = json.loads(raw)
        except ValueError:
            return web.json_response({"error": "invalid_json"}, status=400)
        event_id = event_id or str(event.get("id", ""))[:64]
        etype = str(event.get("type", ""))[:64]
        # ثبت با شناسه‌ی رویداد: تحویل تکراری (Stard دوباره تلاش می‌کند) فقط یک بار پردازش می‌شود
        fresh = await _record(services.db, event_id, etype, "received", None, raw.decode("utf-8", "replace")[:8000])
        if not fresh:
            await services.db.write("UPDATE webhook_events SET attempts = attempts + 1 WHERE id = :id", {"id": event_id})
            return web.json_response({"status": "duplicate"})
        result = await _process_event(services, etype, event)
        await services.db.write("UPDATE webhook_events SET status = :s, response = :r WHERE id = :id",
                                {"s": "processed" if result != "error" else "failed", "r": result[:250],
                                 "id": event_id})
        return web.json_response({"status": "ok", "result": result})

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/api/v1/health", api_health)
    app.router.add_get("/api/v1/stats", api_stats)
    app.router.add_post("/webhooks/stard", stard_webhook)
    return app


async def _record(db, event_id: str, etype: str, status: str, response: str | None, payload: str | None) -> bool:
    n = await db.write("INSERT INTO webhook_events(id, type, status, attempts, response, payload, received_at) "
                       "VALUES(:id, :t, :s, 1, :r, :p, :at) ON CONFLICT(id) DO NOTHING",
                       {"id": event_id, "t": etype, "s": status, "r": response, "p": payload, "at": now()})
    return n == 1


async def _process_event(services: Any, etype: str, event: dict) -> str:
    """رویداد سفارش ← پیگیری فوری همان سفارش در صف (وضعیت از خود API خوانده می‌شود، نه از بدنه‌ی وب‌هوک)."""
    if etype == "webhook.test":
        return "test ok"
    if not etype.startswith("order."):
        return "ignored"
    obj = ((event.get("data") or {}).get("object") or {})
    ref = obj.get("id")
    if not ref:
        return "no order id"
    o = await services.db.get_order_by_ref(str(ref))
    if o is None:
        return "unknown order"
    if o["status"] not in ("new", "pending", "processing"):
        return "already final"
    key = f"order:{o['id']}"
    if not await services.queue.enqueue("order.sync", {"oid": o["id"]}, dedupe_key=key):
        # کار فعال هست؛ زمانش را «الان» کن
        from .db import now_ms
        await services.db.write("UPDATE jobs SET run_at = :t WHERE dedupe_key = :k AND status = 'queued'",
                                {"t": now_ms(), "k": key})
    return f"sync queued for order {o['id']}"


async def start_http(services: Any) -> web.AppRunner | None:
    s = services.settings
    if not s.http_enabled:
        return None
    runner = web.AppRunner(build_app(services), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, s.http_host, s.http_port)
    try:
        await site.start()
    except OSError as e:
        log.error("HTTP server could not bind %s:%s (%s) — continuing without it", s.http_host, s.http_port, e)
        await runner.cleanup()
        return None
    log.info("HTTP server on http://%s:%s (healthz, readyz, metrics, webhooks)", s.http_host, s.http_port)
    return runner
