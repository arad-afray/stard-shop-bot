"""مانیتورینگ، لاگ، هشدار، سرور HTTP و وب‌هوک."""
import asyncio
import hashlib
import hmac
import json
import logging
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot import apikeys
from bot.config import Settings
from bot.db import ago_ms
from bot.http_server import build_app, verify_signature
from bot.locks import DistributedLock, RateLimiter
from bot.logging_setup import new_correlation_id, read_logs, redact, set_secrets, setup_logging
from bot.metrics import Metrics, prometheus
from bot.monitor import AlertManager, beat, business_metrics, evaluate_alerts, run_health_check, services_status
from bot.queue import JobQueue
from bot.runtime import Services
from bot.shop import Shop
from bot.stard_api import StardClient
from bot.worker import Context
from tests.conftest import new_db
from tests.fake_stard import FakeStard
from tests.helpers import make_bot

ADMIN = 100
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawZ"
SECRET = "whsec_testsecret123"


@pytest.fixture
async def env(tmp_path):
    db = await new_db()
    fake = FakeStard()
    api = StardClient("sk_test_ok", transport=fake.transport(), max_retries=0)
    metrics = Metrics()
    api.observer = metrics.observe_api
    q, locks = JobQueue(db, "i1"), DistributedLock(db, "i1")
    shop = Shop(db, api, queue=q, locks=locks)
    bot, session = make_bot()
    settings = Settings(bot_token=TOKEN, stard_api_key="sk_test_ok", admin_ids=[ADMIN],
                        stard_webhook_secret=SECRET, log_dir=str(tmp_path / "logs"),
                        backup_dir=str(tmp_path / "bk"), instance_id="i1")
    from bot.admins import Admins
    admins = Admins(db, [ADMIN])
    ctx = Context(bot=bot, shop=shop, db=db, queue=q, locks=locks, admins=admins, settings=settings, metrics=metrics)
    services = Services(settings=settings, db=db, api=api, shop=shop, queue=q, locks=locks, limiter=RateLimiter(db),
                        extra={"metrics": metrics})
    yield ctx, services, fake, session
    await api.close()
    await db.close()


# ---------- secrets و لاگ ----------
def test_redact_removes_all_secret_shapes():
    set_secrets(["my-db-password-xyz"])
    s = (f"token {TOKEN} key sk_live_AbCdEf123456 hook whsec_abcdefgh1234 "
         "url postgresql://user:my-db-password-xyz@host/db gh ghp_abcdefghijklmnopqrstuvwxyz12 "
         "Authorization: Bearer abcdefghijklmnop sbk_1234abcd_secretsecret")
    out = redact(s)
    for leaked in (TOKEN, "sk_live_AbCdEf123456", "whsec_abcdefgh1234", "my-db-password-xyz",
                   "ghp_abcdefghijklmnopqrstuvwxyz12", "abcdefghijklmnop", "secretsecret"):
        assert leaked not in out, leaked
    assert "user:***@host" in out


def test_json_logs_have_correlation_and_no_secrets(tmp_path):
    setup_logging("DEBUG", str(tmp_path), [TOKEN, "sk_test_supersecret"])
    log = logging.getLogger("bot.test")
    cid = new_correlation_id("t")
    log.info("calling with %s", "sk_test_supersecret")
    try:
        raise RuntimeError(f"failed for {TOKEN}")
    except RuntimeError:
        log.exception("boom")
    for h in logging.getLogger().handlers:
        h.flush()
    raw = (tmp_path / "bot.jsonl").read_text(encoding="utf-8")
    assert TOKEN not in raw and "sk_test_supersecret" not in raw
    entries = [json.loads(line) for line in raw.splitlines()]
    assert all(e["cid"] == cid for e in entries if e["logger"] == "bot.test")
    assert read_logs(str(tmp_path), level="ERROR")[0]["msg"] == "boom"
    assert read_logs(str(tmp_path), search="calling")[0]["level"] == "INFO"
    assert read_logs(str(tmp_path), service="nope") == []
    setup_logging("INFO", None, [])


# ---------- متریک ----------
async def test_api_metrics_and_prometheus(env):
    ctx, services, fake, _ = env
    await ctx.shop.api.ping()
    fake.fail_next = [__import__("tests.fake_stard", fromlist=["err"]).err(503, "api_error")]
    with pytest.raises(Exception):
        await ctx.shop.api.ping()
    m = ctx.metrics
    assert m.counters["api_requests_total"] == 2 and m.counters["api_errors_total"] == 1
    assert m.api_latency.pct(95) is not None and m.api_errors[-1][1].startswith("GET /ping → 503")
    text = prometheus(m, {"queue_length": 3})
    assert "stard_api_latency_p95_seconds" in text and "stard_queue_length 3.0" in text


async def test_business_metrics(env):
    ctx, *_ = env
    await ctx.db.upsert_user(1, "a", "A")
    await ctx.db.credit(1, 1_000_000, "topup")
    oid = await ctx.db.create_order_and_debit(user_id=1, type_="stars", category="stars", product_id=None, title="t",
                                              quantity=50, recipient="@a", gift_message=None, quote_id=None,
                                              base_amount=1000, price=2000)
    await ctx.db.refund_order(oid, "failed")
    bm = await business_metrics(ctx.db)
    assert bm["orders_1h"] == 1 and bm["refund_rate_1h"] == 100.0


# ---------- heartbeat و Health ----------
async def test_heartbeats_and_health_check(env):
    ctx, *_ = env
    await beat(ctx.db, "worker", "i1", "2026-01-01T00:00:00.000Z", {"processed": 3})
    await beat(ctx.db, "bot", "i1", "2026-01-01T00:00:00.000Z")
    st = await services_status(ctx.db)
    assert st["worker"][0]["alive"] and st["worker"][0]["info"]["processed"] == 3
    checks = {c.name: c for c in await run_health_check(ctx)}
    assert set(checks) == {"Bot", "API", "Database", "Worker", "Redis", "Queue", "Storage", "External Services"}
    assert checks["Database"].status == "ok" and checks["API"].status == "ok" and checks["Worker"].status == "ok"
    assert checks["Redis"].status == "off"
    assert await ctx.db.get_json("health:last")  # برای پنل ذخیره شد


async def test_health_reports_dead_worker_and_api_down(env):
    ctx, _, fake, _ = env
    await ctx.db.write("INSERT INTO heartbeats(service, instance, last_seen, started_at) VALUES('worker', 'old', :t, :t)",
                       {"t": ago_ms(600)})
    fake.fail_next = [__import__("httpx").ConnectError("down")] * 5
    checks = {c.name: c for c in await run_health_check(ctx)}
    assert checks["Worker"].status == "fail"
    assert checks["API"].status == "fail" and "network_error" in checks["API"].error


# ---------- هشدار ----------
async def test_alert_fires_once_reminds_and_resolves(env):
    ctx, _, _, session = env
    am = AlertManager(ctx.db, ctx.bot, ctx.admins)
    assert await am.fire("stuck_orders", True, "#1")
    assert not await am.fire("stuck_orders", True, "#1")       # تکراری نیست
    await ctx.db.write("UPDATE alert_state SET last_sent = '2000-01-01T00:00:00Z'")
    assert await am.fire("stuck_orders", True, "#1")           # یادآوری
    assert await am.fire("stuck_orders", False)                # برطرف شد
    assert not await am.fire("stuck_orders", False)
    texts = [m.text for m in session.sent if type(m).__name__ == "SendMessage"]
    assert sum("هشدار" in t for t in texts) == 2 and sum("برطرف شد" in t for t in texts) == 1


async def test_two_monitors_send_one_alert(env):
    ctx, _, _, session = env
    a, b = AlertManager(ctx.db, ctx.bot, ctx.admins), AlertManager(ctx.db, ctx.bot, ctx.admins)
    res = await asyncio.gather(a.fire("api_down", True, "x"), b.fire("api_down", True, "x"))
    assert sorted(res) == [False, True]


async def test_alerts_can_be_disabled(env):
    ctx, _, _, session = env
    await ctx.db.set_json("alerts_off", ["disk_high"])
    am = AlertManager(ctx.db, ctx.bot, ctx.admins)
    assert not await am.fire("disk_high", True, "x")


async def test_evaluate_alerts_detects_stuck_order_and_dead_worker(env):
    ctx, *_ = env
    await ctx.db.upsert_user(1, "a", "A")
    await ctx.db.credit(1, 1_000_000, "topup")
    oid = await ctx.db.create_order_and_debit(user_id=1, type_="stars", category="stars", product_id=None, title="t",
                                              quantity=50, recipient="@a", gift_message=None, quote_id=None,
                                              base_amount=1000, price=2000)
    await ctx.db.write("UPDATE orders SET updated_at = '2020-01-01T00:00:00Z' WHERE id = :i", {"i": oid})
    await ctx.db.write("INSERT INTO heartbeats(service, instance, last_seen, started_at) VALUES('worker', 'w', :t, :t)",
                       {"t": ago_ms(600)})
    res = await evaluate_alerts(ctx, AlertManager(ctx.db, ctx.bot, ctx.admins))
    assert res["stuck_orders"] and res["worker_down"] and not res["api_down"]


# ---------- HTTP ----------
@pytest.fixture
async def client(env):
    ctx, services, fake, session = env
    c = TestClient(TestServer(build_app(services)))
    await c.start_server()
    yield c, ctx, services, fake
    await c.close()


def _sign(body: bytes, t: int | None = None, secret: str = SECRET) -> str:
    t = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


async def test_healthz_and_metrics_local(client):
    c, ctx, services, _ = client
    r = await c.get("/healthz")
    assert r.status == 200 and (await r.json())["status"] == "ok"
    r = await c.get("/metrics")  # از 127.0.0.1 بدون کلید مجاز است
    assert r.status == 200 and "stard_queue_length" in await r.text()


async def test_api_key_auth_scopes_and_revocation(client):
    c, ctx, services, _ = client
    assert (await c.get("/api/v1/stats")).status == 401
    kid, key = await apikeys.create_key(ctx.db, "grafana", ["health:read"], admin_id=ADMIN)
    assert (await c.get("/api/v1/stats", headers={"Authorization": f"Bearer {key}"})).status == 401  # scope ندارد
    assert (await c.get("/api/v1/health", headers={"Authorization": f"Bearer {key}"})).status == 200
    rows = await apikeys.list_keys(ctx.db)
    assert rows[0]["last_used_at"] and "hash" not in rows[0]  # secret یا hash نمایش داده نمی‌شود
    new_id, new_key = await apikeys.rotate_key(ctx.db, kid, admin_id=ADMIN)
    assert (await c.get("/api/v1/health", headers={"Authorization": f"Bearer {key}"})).status == 401
    assert (await c.get("/api/v1/health", headers={"Authorization": f"Bearer {new_key}"})).status == 200
    assert await ctx.db.audit_entries(action="api_key_create")


async def test_webhook_signature_dedupe_and_sync(client):
    c, ctx, services, fake = client
    await ctx.db.upsert_user(1, "a", "A")
    await ctx.db.credit(1, 5_000_000, "topup")
    oid = await ctx.shop.place_order(1, await ctx.shop.stars_offer(50), "@bob")
    body = json.dumps({"id": "evt_1", "type": "order.completed",
                       "data": {"object": {"id": "ord_test_1", "status": "completed"}}}).encode()
    bad = await c.post("/webhooks/stard", data=body, headers={"Stard-Signature": _sign(body, secret="wrong"),
                                                                "Stard-Event-Id": "evt_x"})
    assert bad.status == 400
    old = await c.post("/webhooks/stard", data=body, headers={"Stard-Signature": _sign(body, int(time.time()) - 3600),
                                                                "Stard-Event-Id": "evt_y"})
    assert old.status == 400  # ضد replay
    h = {"Stard-Signature": _sign(body), "Stard-Event-Id": "evt_1"}
    r1 = await c.post("/webhooks/stard", data=body, headers=h)
    assert r1.status == 200 and "sync queued" in (await r1.json())["result"]
    r2 = await c.post("/webhooks/stard", data=body, headers=h)
    assert (await r2.json())["status"] == "duplicate"
    ev = await ctx.db.one("SELECT * FROM webhook_events WHERE id = 'evt_1'")
    assert ev["status"] == "processed" and ev["attempts"] == 2
    job = await ctx.db.one("SELECT * FROM jobs WHERE dedupe_key = :k", {"k": f"order:{oid}"})
    assert job is not None and job["run_at"] <= __import__("bot.db", fromlist=["now_ms"]).now_ms()


def test_verify_signature_edge_cases():
    body = b"{}"
    assert verify_signature(body, _sign(body), SECRET)
    assert not verify_signature(body, None, SECRET)
    assert not verify_signature(body, "garbage", SECRET)
    assert not verify_signature(body + b" ", _sign(body), SECRET)
