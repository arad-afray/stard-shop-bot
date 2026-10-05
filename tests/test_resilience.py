"""آزمون‌های خرابی و بار: رقابت، تکرار، قطعی/تأخیر API، 429، 5xx، خرابی پایگاه داده، ری‌استارت سرور و بار سنگین.

هدف همه: هیچ سناریویی نباید باعث کسر دوباره، تحویل دوباره، برگشت دوباره یا سفارش تکراری شود.
"""
import asyncio
import os
import time

import httpx
import pytest

from bot.db import Database, InsufficientBalance, ago_ms
from bot.locks import DistributedLock
from bot.queue import JobQueue
from bot.shop import Shop, ShopError
from bot.stard_api import StardClient
from bot.worker import Context, JobWorker
from tests.conftest import TEST_DATABASE_URL, new_db
from tests.fake_stard import FakeStard, err
from tests.helpers import make_bot


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    sleeps = []

    async def fast(d):
        sleeps.append(d)
    monkeypatch.setattr("bot.stard_api._sleep", fast)
    return sleeps


@pytest.fixture
async def env():
    db = await new_db()
    fake = FakeStard()
    api = StardClient("sk_test_ok", transport=fake.transport(), max_retries=3)
    q, locks = JobQueue(db, "w1"), DistributedLock(db, "w1")
    shop = Shop(db, api, default_profit=10, queue=q, locks=locks)
    await db.upsert_user(1, "alice", "Alice")
    await db.credit(1, 50_000_000, "topup")
    yield db, shop, fake, api
    await api.close()
    await db.close()


async def _consistent(db):
    assert not await db.ledger_balance_check(), "balance ≠ ledger"
    dup = await db.all("SELECT ref, COUNT(*) AS n FROM ledger WHERE kind = 'refund' GROUP BY ref HAVING COUNT(*) > 1")
    assert not dup, f"double refunds: {dup}"


# ---------- رقابت و تکرار ----------
async def test_concurrent_refunds_pay_once(env):
    db, shop, fake, _ = env
    oid = await shop.place_order(1, await shop.stars_offer(100), "@bob")
    fake.orders["ord_test_1"]["status"] = "failed"
    before = (await db.get_user(1)).balance
    res = await asyncio.gather(*(db.refund_order(oid, "failed") for _ in range(10)),
                               *(shop.sync_order(oid) for _ in range(5)), return_exceptions=True)
    assert sum(r is True for r in res[:10]) <= 1
    price = (await db.get_order(oid))["price"]
    assert (await db.get_user(1)).balance == before + price
    await _consistent(db)


async def test_concurrent_topup_approvals_credit_once(env):
    db, *_ = env
    tid, _ = await db.create_topup(1, 77_000, "p", "u1")
    before = (await db.get_user(1)).balance
    res = await asyncio.gather(*(db.resolve_topup(tid, 900 + i, True) for i in range(10)))
    assert sum(r is not None for r in res) == 1
    assert (await db.get_user(1)).balance == before + 77_000
    await _consistent(db)


async def test_duplicate_receipt_and_double_click_purchase(env):
    db, shop, fake, _ = env
    a = await asyncio.gather(*(db.create_topup(1, 50_000, "p", "same-file") for _ in range(5)))
    assert len({t for t, _ in a}) == 1 and sum(c for _, c in a) == 1
    offer = await shop.stars_offer(100)
    oids = await asyncio.gather(*(shop.place_order(1, offer, "@bob", checkout_id="click") for _ in range(10)))
    assert len(set(oids)) == 1 and len(fake.orders) == 1
    await _consistent(db)


async def test_admin_refund_racing_worker_completion_never_double_spends(env):
    """مدیر برگشت می‌زند هم‌زمان با تکمیل سفارش در Stard: یا تحویل یا برگشت، هرگز هر دو."""
    db, shop, fake, _ = env
    oid = await shop.place_order(1, await shop.stars_offer(100), "@bob")
    fake.orders["ord_test_1"]["status"] = "completed"
    res = await asyncio.gather(shop.sync_order(oid), shop.admin_refund(oid), return_exceptions=True)
    o = await db.get_order(oid)
    assert not (o["status"] == "completed" and o["refunded"])
    assert res is not None
    await _consistent(db)


# ---------- API: timeout، 429، 5xx ----------
async def test_api_timeout_on_submit_keeps_money_and_retries_safely(env):
    db, shop, fake, _ = env
    offer = await shop.stars_offer(100)
    fake.fail_next = [httpx.ReadTimeout("slow")] * 4   # همه‌ی تلاش‌های درون‌خطی timeout
    oid = await shop.place_order(1, offer, "@bob")
    o = await db.get_order(oid)
    assert o["status"] == "new" and not o["refunded"] and o["failure_reason"].startswith("retrying")
    assert await shop.sync_order(oid) == ("new", "pending")   # بعداً با همان کلید ثبت شد
    assert len(fake.orders) == 1
    await _consistent(db)


async def test_api_429_honours_retry_after(env, fast_retries):
    db, shop, fake, api = env
    offer = await shop.stars_offer(100)
    fake.fail_next = [httpx.Response(429, headers={"Retry-After": "7"},
                                     json={"error": {"code": "rate_limit_exceeded", "message": "slow"}})]
    oid = await shop.place_order(1, offer, "@bob")
    assert 7.0 in fast_retries and (await db.get_order(oid))["status"] == "pending"


async def test_api_5xx_on_submit_never_refunds_or_duplicates(env):
    db, shop, fake, _ = env
    offer = await shop.stars_offer(100)
    fake.fail_next = [err(503, "api_error")] * 4
    oid = await shop.place_order(1, offer, "@bob")
    assert (await db.get_order(oid))["refunded"] == 0
    await shop.sync_order(oid)
    assert len(fake.orders) == 1
    await _consistent(db)


async def test_api_4xx_rejection_refunds_exactly_once(env):
    db, shop, fake, _ = env
    offer = await shop.stars_offer(100)
    fake.fail_next = [err(400, "recipient_invalid")]
    with pytest.raises(ShopError):
        await shop.place_order(1, offer, "@bob")
    o = (await db.recent_orders())[0]
    assert o["refunded"] == 1 and fake.orders == {}
    assert await shop.sync_order(o["id"]) is None
    await _consistent(db)


# ---------- خرابی پایگاه داده ----------
async def test_database_failure_mid_purchase_changes_nothing(env, monkeypatch):
    db, shop, fake, _ = env
    offer = await shop.stars_offer(100)
    before = (await db.get_user(1)).balance

    async def broken(c, oid):
        raise ConnectionError("database connection lost")
    monkeypatch.setattr(shop.queue, "enqueue", lambda *a, **k: broken(None, None))
    with pytest.raises(ConnectionError):
        await shop.place_order(1, offer, "@bob")
    assert (await db.get_user(1)).balance == before         # تراکنش کامل برگشت
    assert await db.recent_orders() == [] and fake.orders == {}
    await _consistent(db)


async def test_alert_for_database_outage_is_sent_without_database(env):
    from bot.monitor import AlertManager, evaluate_alerts
    db, shop, fake, _ = env
    bot, session = make_bot()
    ctx = Context(bot=bot, shop=shop, db=db, queue=shop.queue, locks=shop.locks, admins=type("A", (), {
        "all": lambda self: [100]})(), settings=None)
    await db.engine.dispose()

    async def down():
        raise ConnectionError("db down")
    db.ping = down
    res = await evaluate_alerts(ctx, AlertManager(db, bot, ctx.admins))
    assert res == {"db_error": True}
    assert any("پایگاه داده" in m.text for m in session.sent if type(m).__name__ == "SendMessage")


# ---------- ری‌استارت سرور ----------
@pytest.mark.skipif(bool(TEST_DATABASE_URL), reason="uses a SQLite file to simulate a process restart")
async def test_server_restart_recovers_queued_orders(tmp_path):
    path = str(tmp_path / "shop.db")
    fake = FakeStard()
    db1 = Database(path)
    await db1.connect()
    api1 = StardClient("sk_test_ok", transport=fake.transport(), max_retries=0)
    shop1 = Shop(db1, api1, queue=JobQueue(db1, "old"), locks=DistributedLock(db1, "old"))
    await db1.upsert_user(1, "a", "A")
    await db1.credit(1, 5_000_000, "topup")
    offer = await shop1.stars_offer(100)
    shop1.submit = lambda oid: asyncio.sleep(0)   # «کرش» قبل از ارسال
    oid = await shop1.place_order(1, offer, "@bob")
    await db1.write("UPDATE jobs SET run_at = :t", {"t": ago_ms(1)})
    [job] = await JobQueue(db1, "old").claim(1, lease=0.1)   # worker قدیمی کار را گرفته و مرده است
    await api1.close()
    await db1.close()                                        # ← سرور خاموش شد
    await asyncio.sleep(0.2)
    db2 = Database(path)                                     # ← سرور دوباره روشن شد
    await db2.connect()
    api2 = StardClient("sk_test_ok", transport=fake.transport(), max_retries=0)
    q2, locks2 = JobQueue(db2, "new"), DistributedLock(db2, "new")
    shop2 = Shop(db2, api2, queue=q2, locks=locks2)
    bot, _ = make_bot()
    w = JobWorker(Context(bot=bot, shop=shop2, db=db2, queue=q2, locks=locks2))
    await db2.write("UPDATE jobs SET run_at = :t", {"t": ago_ms(1)})
    await w.run_once()
    assert (await db2.get_order(oid))["stard_ref"] == "ord_test_1" and len(fake.orders) == 1
    assert (await db2.get_user(1)).balance == 5_000_000 - offer.price
    await api2.close()
    await db2.close()


async def test_checkout_state_survives_restart_with_redis():
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url("redis://localhost:6379/14")
        await r.ping()
    except Exception:
        pytest.skip("redis not available")
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
    await r.flushdb()
    key = StorageKey(bot_id=42, chat_id=1, user_id=1)
    s1 = RedisStorage(r, key_builder=DefaultKeyBuilder(prefix="stardfsm", with_destiny=True))
    await s1.set_state(key, "Buy:confirm")
    await s1.set_data(key, {"checkout_id": "abc", "recipient": "@bob"})
    s2 = RedisStorage(aioredis.from_url("redis://localhost:6379/14"),
                      key_builder=DefaultKeyBuilder(prefix="stardfsm", with_destiny=True))   # نمونه‌ی دیگر/بعد از ری‌استارت
    assert await s2.get_state(key) == "Buy:confirm" and (await s2.get_data(key))["checkout_id"] == "abc"
    await s1.close()
    await s2.close()


# ---------- بار ----------
async def test_load_concurrent_purchases_and_workers(env):
    """۴۰۰ خرید هم‌زمان از ۸۰ کاربر + ۴ worker هم‌زمان: هیچ موجودی منفی، سفارش تکراری یا ناسازگاری."""
    db, shop, fake, _ = env
    users = list(range(1000, 1080))
    for u in users:
        await db.upsert_user(u, None, "U")
        await db.credit(u, 1_000_000, "topup")
    offer = await shop.stars_offer(50)           # ~۲۴۱ هزار؛ هر کاربر پول ۴ خرید دارد
    t0 = time.perf_counter()
    res = await asyncio.gather(*(shop.place_order(u, offer, "@bob", checkout_id=f"{u}-{i}")
                                 for u in users for i in range(5)), return_exceptions=True)
    elapsed = time.perf_counter() - t0
    ok = [r for r in res if isinstance(r, int)]
    bad = [r for r in res if not isinstance(r, (int, InsufficientBalance))]
    assert not bad, bad[:3]
    per_user = 1_000_000 // offer.price
    assert len(ok) == len(users) * per_user
    for oid in ok:
        fake.orders[(await db.get_order(oid))["stard_ref"]]["status"] = "completed"
    bot, _ = make_bot()
    workers = [JobWorker(Context(bot=bot, shop=shop, db=db, queue=JobQueue(db, f"w{i}"), locks=DistributedLock(db, f"w{i}")),
                         concurrency=16) for i in range(4)]
    for _ in range(40):  # هر سفارش دو اجرا دارد: ارسال ← پیگیری
        await db.write("UPDATE jobs SET run_at = :t WHERE status = 'queued'", {"t": ago_ms(1)})
        if sum(await asyncio.gather(*(w.run_once() for w in workers))) == 0:
            break
    done = await db.scalar("SELECT COUNT(*) FROM orders WHERE status = 'completed'")
    assert done == len(ok) and len(fake.orders) == len(ok)
    await _consistent(db)
    print(f"\n[load] {len(res)} purchase attempts in {elapsed:.2f}s ({len(res) / elapsed:.0f}/s) on {db.dialect}")


@pytest.mark.skipif(not os.environ.get("STRESS"), reason="set STRESS=1 for the long stress test")
async def test_stress_5000_purchases(env):
    db, shop, fake, _ = env
    fake.balance = 10**13  # کیف پول Stard شبیه‌سازی‌شده باید برای ۵۰۰۰ سفارش کافی باشد
    users = list(range(5000, 5500))
    for u in users:
        await db.upsert_user(u, None, "U")
        await db.credit(u, 10_000_000, "topup")
    offer = await shop.stars_offer(50)
    t0 = time.perf_counter()
    res = await asyncio.gather(*(shop.place_order(u, offer, "@b", checkout_id=f"{u}-{i}") for u in users
                                 for i in range(10)), return_exceptions=True)
    elapsed = time.perf_counter() - t0
    from collections import Counter
    errors = Counter(f"{type(r).__name__}: {str(r)[:120]}" for r in res if not isinstance(r, int))
    assert not errors, errors.most_common(5)
    await _consistent(db)
    print(f"\n[stress] {len(res)} purchases in {elapsed:.1f}s ({len(res) / elapsed:.0f}/s) on {db.dialect}")
