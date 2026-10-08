"""صف، worker، بازیابی بعد از کرش، قفل توزیع‌شده و محدودیت نرخ (روی SQLite و PostgreSQL)."""
import asyncio

import pytest

from bot.db import InsufficientBalance, ago_ms
from bot.locks import DistributedLock, RateLimiter
from bot.queue import JobQueue, RetryLater
from bot.shop import Shop
from bot.stard_api import StardClient
from bot.worker import HANDLERS, Context, JobWorker, Scheduler, recover_orders, start_broadcast
from tests.conftest import new_db
from tests.fake_stard import FakeStard
from tests.helpers import make_bot


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr("bot.stard_api._sleep", fast)


@pytest.fixture
async def env():
    db = await new_db()
    fake = FakeStard()
    api = StardClient("sk_test_ok", transport=fake.transport(), max_retries=1)
    q = JobQueue(db, "w1")
    locks = DistributedLock(db, "w1")
    shop = Shop(db, api, default_profit=10, queue=q, locks=locks)
    bot, session = make_bot()
    ctx = Context(bot=bot, shop=shop, db=db, queue=q, locks=locks)
    await db.upsert_user(1, "alice", "Alice")
    yield ctx, fake, session
    await api.close()
    await db.close()


async def _drain(worker: JobWorker, ctx: Context, rounds: int = 20) -> None:
    """کارها را تا وقتی آماده‌اند اجرا می‌کند؛ کارهای زمان‌بندی‌شده برای آینده را «الان» می‌کند."""
    for _ in range(rounds):
        await ctx.db.write("UPDATE jobs SET run_at = :t WHERE status = 'queued'", {"t": ago_ms(1)})
        if not await worker.run_once():
            break


# ---------- صف ----------
async def test_dedupe_one_active_job_per_key(env):
    ctx, *_ = env
    assert await ctx.queue.enqueue("order.sync", {"oid": 1}, dedupe_key="order:1")
    assert not await ctx.queue.enqueue("order.submit", {"oid": 1}, dedupe_key="order:1")
    [job] = await ctx.queue.claim(5)
    await ctx.queue.complete(job)
    assert await ctx.queue.enqueue("order.sync", {"oid": 1}, dedupe_key="order:1")  # بعد از اتمام آزاد شد


async def test_concurrent_claims_never_overlap(env):
    ctx, *_ = env
    for i in range(60):
        await ctx.queue.enqueue("noop", {"i": i})
    queues = [JobQueue(ctx.db, f"w{i}") for i in range(6)]
    results = await asyncio.gather(*(q.claim(15) for q in queues))
    ids = [j.id for r in results for j in r]
    assert len(ids) == len(set(ids)) == 60


async def test_crashed_worker_lease_is_recovered_and_fenced(env):
    ctx, *_ = env
    await ctx.queue.enqueue("noop", {})
    [job] = await ctx.queue.claim(1, lease=0)  # worker w1 کار را گرفت و «کرش کرد»
    await asyncio.sleep(1.1)
    other = JobQueue(ctx.db, "w2")
    [again] = await other.claim(1)
    assert again.id == job.id and again.attempts == 2
    assert await ctx.queue.complete(job) is False   # worker قدیمی دیگر صاحب کار نیست (fencing)
    assert await other.complete(again) is True


async def test_failures_backoff_then_dead_letter_then_retry(env):
    ctx, *_ = env

    async def boom(job, c):
        raise ValueError("boom")
    HANDLERS["test.boom"] = (boom, 5)
    try:
        await ctx.queue.enqueue("test.boom", {}, max_attempts=3)
        w = JobWorker(ctx)
        await _drain(w, ctx)
        [dead] = await ctx.queue.dead_jobs()
        assert dead["attempts"] == 3 and "boom" in dead["last_error"]
        assert await ctx.queue.retry_dead(dead["id"])
        assert (await ctx.queue.stats())["queued"] == 1
    finally:
        HANDLERS.pop("test.boom")


async def test_timeout_counts_as_failure(env):
    ctx, *_ = env

    async def slow(job, c):
        await asyncio.sleep(10)
    HANDLERS["test.slow"] = (slow, 0.05)
    try:
        await ctx.queue.enqueue("test.slow", {}, max_attempts=1)
        await JobWorker(ctx).run_once()
        [dead] = await ctx.queue.dead_jobs()
        assert "TimeoutError" in dead["last_error"]
    finally:
        HANDLERS.pop("test.slow")


async def test_retry_later_does_not_burn_attempts(env):
    ctx, *_ = env
    calls = []

    async def later(job, c):
        calls.append(job.attempts)
        if len(calls) < 5:
            raise RetryLater(0)
    HANDLERS["test.later"] = (later, 5)
    try:
        await ctx.queue.enqueue("test.later", {}, max_attempts=2)
        await _drain(JobWorker(ctx), ctx)
        assert calls == [1, 1, 1, 1, 1] and (await ctx.queue.stats())["done"] == 1
    finally:
        HANDLERS.pop("test.later")


# ---------- سفارش‌ها از طریق صف ----------
async def test_bot_crash_after_payment_worker_delivers_once(env):
    """ربات بعد از کسر پول و قبل از ارسال به Stard کرش می‌کند → worker سفارش را یک بار می‌فرستد."""
    ctx, fake, session = env
    await ctx.db.credit(1, 5_000_000, "topup")
    offer = await ctx.shop.stars_offer(100)
    orig = ctx.shop.submit

    async def crash(oid):
        raise SystemExit("bot crashed")
    ctx.shop.submit = crash
    with pytest.raises(SystemExit):
        await ctx.shop.place_order(1, offer, "@bob")
    ctx.shop.submit = orig
    o = (await ctx.db.recent_orders())[0]
    assert o["status"] == "new" and fake.orders == {}
    assert (await ctx.queue.stats())["queued"] == 1     # outbox: کار ارسال همراه پرداخت ثبت شده بود
    fake_done = lambda: fake.orders["ord_test_1"].update(status="completed")  # noqa: E731
    w = JobWorker(ctx)
    await _drain(w, ctx, rounds=1)
    assert len(fake.orders) == 1 and (await ctx.db.get_order(o["id"]))["status"] == "pending"
    fake_done()
    await _drain(w, ctx)
    assert (await ctx.db.get_order(o["id"]))["status"] == "completed"
    assert len(fake.orders) == 1 and fake.balance == 100_000_000 - 100 * 4382
    assert any("تحویل داده شد" in t for t in session.texts())


async def test_worker_retries_lost_responses_without_double_charge(env):
    ctx, fake, _ = env
    await ctx.db.credit(1, 5_000_000, "topup")
    offer = await ctx.shop.stars_offer(100)
    fake.drop_responses = 10  # Stard سفارش را می‌سازد ولی پاسخ‌ها گم می‌شوند
    oid = await ctx.shop.place_order(1, offer, "@bob")
    w = JobWorker(ctx)
    await _drain(w, ctx, rounds=3)
    fake.drop_responses = 0
    await _drain(w, ctx, rounds=2)
    assert len(fake.orders) == 1 and fake.balance == 100_000_000 - 100 * 4382
    assert (await ctx.db.get_order(oid))["stard_ref"] == "ord_test_1"


async def test_api_failure_refunds_exactly_once_via_worker(env):
    ctx, fake, session = env
    await ctx.db.credit(1, 5_000_000, "topup")
    oid = await ctx.shop.place_order(1, await ctx.shop.stars_offer(100), "@bob")
    fake.orders["ord_test_1"]["status"] = "failed"
    w = JobWorker(ctx)
    await asyncio.gather(_drain(w, ctx), _drain(JobWorker(Context(**{**ctx.__dict__, "queue": JobQueue(ctx.db, "w2")})), ctx))
    assert (await ctx.db.get_user(1)).balance == 5_000_000
    refunds = await ctx.db.all("SELECT * FROM ledger WHERE kind = 'refund' AND ref = :r", {"r": str(oid)})
    assert len(refunds) == 1


async def test_recover_orders_requeues_orphans(env):
    ctx, fake, _ = env
    await ctx.db.credit(1, 5_000_000, "topup")
    ctx.shop.queue = None  # سفارشی که بدون صف ساخته شده (مثل نسخه‌ی ۲)
    ctx.shop.submit = lambda oid: asyncio.sleep(0)
    oid = await ctx.shop.place_order(1, await ctx.shop.stars_offer(50), "@bob")
    assert (await ctx.queue.stats())["queued"] == 0
    await recover_orders(ctx)
    await recover_orders(ctx)  # دوباره: کار تکراری نمی‌سازد
    assert (await ctx.queue.stats())["queued"] == 1
    del ctx.shop.submit
    await _drain(JobWorker(ctx), ctx, rounds=1)
    assert (await ctx.db.get_order(oid))["stard_ref"] == "ord_test_1"


async def test_scheduler_runs_each_task_on_one_instance_only(env):
    ctx, *_ = env
    runs = []

    async def task(c):
        runs.append(c.locks.owner)
    s1 = Scheduler(ctx, [("t", 60, task)])
    s2 = Scheduler(Context(**{**ctx.__dict__, "locks": DistributedLock(ctx.db, "other")}), [("t", 60, task)])
    await asyncio.gather(s1.tick(), s2.tick())
    assert len(runs) == 1


# ---------- خرید هم‌زمان و تکراری ----------
async def test_same_checkout_concurrently_creates_one_order(env):
    ctx, fake, _ = env
    await ctx.db.credit(1, 5_000_000, "topup")
    offer = await ctx.shop.stars_offer(100)
    oids = await asyncio.gather(*(ctx.shop.place_order(1, offer, "@bob", checkout_id="chk-1") for _ in range(8)))
    assert len(set(oids)) == 1 and len(fake.orders) == 1
    assert (await ctx.db.get_user(1)).balance == 5_000_000 - offer.price
    assert len(await ctx.db.all("SELECT id FROM ledger WHERE kind = 'order'")) == 1


async def test_concurrent_purchases_never_overdraw(env):
    ctx, fake, _ = env
    for uid in range(10, 20):
        await ctx.db.upsert_user(uid, f"u{uid}", "U")
        await ctx.db.credit(uid, 1_000_000, "topup")
    offer = await ctx.shop.stars_offer(50)  # هر کاربر پول ~۴ خرید دارد
    tasks = [ctx.shop.place_order(uid, offer, "@bob", checkout_id=f"{uid}-{i}") for uid in range(10, 20) for i in range(8)]
    res = await asyncio.gather(*tasks, return_exceptions=True)
    ok = [r for r in res if isinstance(r, int)]
    assert all(isinstance(r, (int, InsufficientBalance)) for r in res)
    per_user = 1_000_000 // offer.price
    assert len(ok) == 10 * per_user
    assert not await ctx.db.ledger_balance_check()  # موجودی همه با ledger می‌خواند
    for uid in range(10, 20):
        assert (await ctx.db.get_user(uid)).balance == 1_000_000 - per_user * offer.price


# ---------- پیام همگانی ----------
async def test_broadcast_queue_handles_blocked_and_resumes_after_crash(env):
    ctx, fake, session = env
    import bot.worker as wk
    for uid in range(100, 125):
        await ctx.db.upsert_user(uid, None, "U")
    session.blocked = {105, 110}
    bid = await start_broadcast(ctx.db, ctx.queue, admin_id=1, from_chat=1, message_id=99)
    old_chunk, wk.BROADCAST_CHUNK = wk.BROADCAST_CHUNK, 10
    old_rate, wk.BROADCAST_RATE = wk.BROADCAST_RATE, 10_000
    try:
        await _drain(JobWorker(ctx), ctx)
    finally:
        wk.BROADCAST_CHUNK, wk.BROADCAST_RATE = old_chunk, old_rate
    b = await ctx.db.one("SELECT * FROM broadcasts WHERE id = :i", {"i": bid})
    total = await ctx.db.count_users()
    assert b["status"] == "done" and b["blocked"] == 2 and b["sent"] == total - 2
    copies = [m.chat_id for m in session.sent if type(m).__name__ == "CopyMessage"]
    assert len(copies) == len(set(copies))  # هیچ‌کس دو بار پیام نگرفت
    assert (await ctx.db.get_user(105)).__dict__  # کاربر هنوز هست
    assert await ctx.db.scalar("SELECT blocked FROM users WHERE id = 105") == 1


# ---------- قفل و محدودیت نرخ ----------
async def test_distributed_lock_exclusive_and_expires(env):
    ctx, *_ = env
    a, b = DistributedLock(ctx.db, "a"), DistributedLock(ctx.db, "b")
    assert await a.acquire("x", 0.5)
    assert not await b.acquire("x", 10)
    await asyncio.sleep(1.1)
    assert await b.acquire("x", 10)   # صاحب قبلی «کرش کرده»؛ قفل منقضی شد
    assert not await a.acquire("x", 10)


async def test_rate_limiter_db_and_redis(env):
    ctx, *_ = env
    rl = RateLimiter(ctx.db)
    assert [await rl.hit("k", 3, 60) for _ in range(5)] == [True, True, True, False, False]
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url("redis://localhost:6379/15")
        await r.ping()
    except Exception:
        pytest.skip("redis not available")
    await r.flushdb()
    rr = RateLimiter(ctx.db, r)
    assert [await rr.hit("k", 2, 60) for _ in range(3)] == [True, True, False]
    await r.aclose()


async def test_scheduler_first_tick_runs_even_right_after_boot(env, monkeypatch):
    # روی سیستمی که تازه روشن شده monotonic کوچک است؛ کار ۲۴ ساعته نباید تا فردا عقب بیفتد
    from types import SimpleNamespace

    import bot.worker as w
    ctx, *_ = env
    runs = []

    async def task(c):
        runs.append(1)
    monkeypatch.setattr(w, "time", SimpleNamespace(monotonic=lambda: 5.0))
    s = Scheduler(ctx, [("daily", 86400, task)])
    await s.tick()
    await s.tick()
    assert runs == [1]
