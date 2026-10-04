import asyncio

import httpx
import pytest

from bot.db import Database, InsufficientBalance
from bot.pricing import apply_profit
from bot.shop import Shop, ShopError, normalize_username
from bot.stard_api import StardClient, StardError
from tests.fake_stard import FakeStard, err


# ---------- pricing ----------
def test_apply_profit_rounds_up():
    assert apply_profit(2_191_000, 10) == 2_411_000          # 2,410,100 → 2,411,000
    assert apply_profit(3_788_624, 0) == 3_789_000
    assert apply_profit(1000, 0) == 1000
    assert apply_profit(1001, 0, round_to=1) == 1001
    assert apply_profit(100_000, 12.5) == 113_000         # 112,500 → 113,000
    assert apply_profit(100_000, 12.5, round_to=1) == 112_500
    assert apply_profit(0, 50) == 0


def test_apply_profit_never_below_cost():
    for base in (1, 999, 12_345, 4_382 * 77):
        for pct in (0, 0.01, 3.3, 10, 99.99):
            assert apply_profit(base, pct) >= base * (1 + pct / 100)


def test_apply_profit_rejects_negative():
    with pytest.raises(ValueError):
        apply_profit(100, -1)


def test_normalize_username():
    assert normalize_username("@durov") == "@durov"
    assert normalize_username("durov_1") == "@durov_1"
    assert normalize_username("https://t.me/durov") == "@durov"
    assert normalize_username("ab") is None
    assert normalize_username("1abcde") is None
    assert normalize_username("سلام") is None


# ---------- fixtures ----------
@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.connect()
    await d.upsert_user(1, "alice", "Alice")
    yield d
    await d.close()


@pytest.fixture
def fake():
    return FakeStard()


@pytest.fixture
async def shop(db, fake):
    api = StardClient("sk_test_ok", "https://stard-market.ir/api/v1", transport=fake.transport(), max_retries=2)
    s = Shop(db, api, default_profit=10)
    yield s
    await api.close()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr("bot.stard_api.asyncio.sleep", fast)


# ---------- db ----------
async def test_debit_is_atomic_under_concurrency(db):
    await db.credit(1, 1000, "topup")
    results = await asyncio.gather(*(db.debit(1, 300, "order") for _ in range(10)), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len(ok) == 3
    assert all(isinstance(r, InsufficientBalance) for r in results if isinstance(r, Exception))
    assert (await db.get_user(1)).balance == 100


async def test_topup_resolves_once(db):
    tid = await db.create_topup(1, 50_000, "photo")
    assert await db.resolve_topup(tid, 99, True) is not None
    assert await db.resolve_topup(tid, 98, True) is None
    assert (await db.get_user(1)).balance == 50_000


async def test_refund_only_once(db):
    await db.credit(1, 10_000, "topup")
    oid = await db.create_order_and_debit(user_id=1, type_="stars", category="stars", product_id=None, title="t",
                                          quantity=50, recipient="@a", gift_message=None, quote_id=None,
                                          base_amount=5000, price=6000)
    assert (await db.get_user(1)).balance == 4000
    assert await db.refund_order(oid, "failed") is True
    assert await db.refund_order(oid, "failed") is False
    assert (await db.get_user(1)).balance == 10_000


async def test_profit_settings(shop):
    assert await shop.get_profit("stars") == 10
    await shop.set_profit(20)
    await shop.set_profit(5, "stars")
    assert await shop.get_profit("stars") == 5
    assert await shop.get_profit("premium") == 20
    await shop.clear_category_profit("stars")
    assert await shop.get_profit("stars") == 20
    with pytest.raises(ShopError):
        await shop.set_profit(-1)


# ---------- api client ----------
async def test_client_parses_errors(fake):
    api = StardClient("sk_test_bad", transport=fake.transport())
    with pytest.raises(StardError) as e:
        await api.ping()
    assert e.value.status == 401 and e.value.code == "invalid_api_key"
    await api.close()


async def test_client_retries_get_on_5xx(fake):
    api = StardClient("sk_test_ok", transport=fake.transport(), max_retries=3)
    fake.fail_next = [err(503, "api_error"), httpx.ConnectError("down"), httpx.Response(429, headers={"Retry-After": "1"}, json={})]
    assert (await api.ping())["ok"] is True
    await api.close()


async def test_client_never_retries_post_without_key(fake):
    api = StardClient("sk_test_ok", transport=fake.transport(), max_retries=3)
    fake.fail_next = [err(503, "api_error")]
    with pytest.raises(StardError):
        await api.quote("stars", quantity=100)
    assert fake.calls.count(("POST", "/orders/quote")) == 1
    await api.close()


# ---------- shop flow ----------
async def test_buy_stars_with_profit(shop, db, fake):
    await db.credit(1, 5_000_000, "topup")
    offer = await shop.stars_offer(500)
    assert offer.base_amount == 500 * 4382
    assert offer.price == apply_profit(500 * 4382, 10)
    oid = await shop.place_order(1, offer, "@bob")
    o = await db.get_order(oid)
    assert o["status"] == "pending" and o["stard_ref"] == "ord_test_1"
    assert (await db.get_user(1)).balance == 5_000_000 - offer.price
    assert fake.orders["ord_test_1"]["metadata"]["bot_order_id"] == oid


async def test_stars_quantity_validated(shop):
    with pytest.raises(ShopError):
        await shop.stars_offer(10)


async def test_insufficient_user_balance_creates_nothing(shop, db, fake):
    offer = await shop.stars_offer(50)
    with pytest.raises(InsufficientBalance):
        await shop.place_order(1, offer, "@bob")
    assert fake.orders == {}
    assert await db.recent_orders() == []


async def test_stard_rejection_refunds_user(shop, db, fake):
    await db.credit(1, 5_000_000, "topup")
    fake.balance = 0  # کیف پول API خالی است
    offer = await shop.stars_offer(100)
    with pytest.raises(ShopError):
        await shop.place_order(1, offer, "@bob")
    assert (await db.get_user(1)).balance == 5_000_000
    o = (await db.recent_orders())[0]
    assert o["refunded"] == 1 and o["failure_reason"] == "insufficient_funds"


async def test_price_change_refunds(shop, db, fake):
    await db.credit(1, 5_000_000, "topup")
    offer = await shop.stars_offer(100)
    fake.unit += 10  # قیمت بعد از quote بالا رفت
    with pytest.raises(ShopError):
        await shop.place_order(1, offer, "@bob")
    assert (await db.get_user(1)).balance == 5_000_000


async def test_lost_response_is_resubmitted_without_double_charge(shop, db, fake):
    await db.credit(1, 5_000_000, "topup")
    offer = await shop.stars_offer(100)
    fake.drop_responses = 3  # سفارش در Stard ثبت می‌شود ولی هیچ پاسخی نمی‌رسد
    oid = await shop.place_order(1, offer, "@bob")
    assert (await db.get_order(oid))["status"] == "new"
    # worker دوباره با همان Idempotency-Key می‌فرستد
    assert await shop.sync_order(oid) == ("new", "pending")
    assert len(fake.orders) == 1
    assert fake.balance == 100_000_000 - 100 * 4382


async def test_sync_completes_and_refunds(shop, db, fake):
    await db.credit(1, 10_000_000, "topup")
    a = await shop.place_order(1, await shop.stars_offer(50), "@bob")
    b = await shop.place_order(1, await shop.product_offer(4518, "premium"), "@bob")
    before = (await db.get_user(1)).balance
    fake.orders["ord_test_1"]["status"] = "completed"
    fake.orders["ord_test_2"]["status"] = "failed"
    assert await shop.sync_order(a) == ("pending", "completed")
    assert await shop.sync_order(b) == ("pending", "failed")
    assert await shop.sync_order(b) is None  # نهایی؛ دوباره برگشت نمی‌خورد
    price_b = (await db.get_order(b))["price"]
    assert (await db.get_user(1)).balance == before + price_b
    s = await db.stats()
    assert s["done"] == 1 and s["refunded"] == 1


async def test_product_offer_checks_category(shop):
    with pytest.raises(ShopError):
        await shop.product_offer(4518, "star_gift")
    gifts = await shop.gifts()
    assert gifts[0]["sell_price"] == apply_profit(112275, 10)
