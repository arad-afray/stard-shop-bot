import asyncio

import httpx
import pytest

from tests.conftest import new_db, requires_sqlite

from bot.db import Database, InsufficientBalance
from bot.handlers.prices import detect
from bot.pricing import apply_discount, apply_profit, to_float, to_int
from bot.shop import Shop, ShopError, normalize_post_link, normalize_username
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
    d = await new_db()
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
    tid, created = await db.create_topup(1, 50_000, "photo", "uniq1")
    assert created and await db.create_topup(1, 50_000, "photo", "uniq1") == (tid, False)  # رسید تکراری
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


# ---------- v2 ----------
def test_detect_group_triggers():
    assert detect("قیمت دلار") == "usd"
    assert detect("قيمت دلار؟") == "usd"          # ی عربی
    assert detect("قیمت تون") == "ton"
    assert detect("قیمت استارز") == "stars"
    assert detect("قیمت") == "all"
    assert detect("قیمتا") == "all"
    assert detect("قیمت دلار و تون") == "all"
    assert detect("دلار") == "usd"
    assert detect("سلام") is None
    assert detect("دلار امروز خیلی گرون شده ولی من نمیدونم چرا") is None
    assert detect("/start") is None
    assert detect("") is None and detect(None) is None


def test_normalize_post_link():
    assert normalize_post_link("https://t.me/durov/123") == "https://t.me/durov/123"
    assert normalize_post_link("t.me/durov/5?single") == "https://t.me/durov/5"
    assert normalize_post_link("https://t.me/c/1234567/89") == "https://t.me/c/1234567/89"
    assert normalize_post_link("https://t.me/durov") is None
    assert normalize_post_link("https://evil.com/durov/1") is None


def test_discount_never_below_cost():
    assert apply_discount(100_000, 50_000, 20) == (80_000, 20_000)
    assert apply_discount(100_000, 95_000, 20) == (95_000, 5_000)
    assert apply_discount(100_000, 0, 0) == (100_000, 0)
    assert apply_discount(100_000, 0, 150) == (0, 100_000)


def test_number_parsing():
    assert to_int("۱۲٬۵۰۰") == 12500 and to_int("1,000") == 1000 and to_int("abc") is None and to_int("-5") is None
    assert to_float("۱۲/۵") == 12.5 and to_float("10%") == 10 and to_float("nan") is None and to_float("x") is None


@requires_sqlite
async def test_migrates_v1_database(tmp_path):
    import aiosqlite
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as c:  # شِمای نسخه‌ی ۱
        await c.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0), banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL);
            CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, type TEXT NOT NULL,
                category TEXT NOT NULL, product_id INTEGER, title TEXT NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
                recipient TEXT, gift_message TEXT, quote_id TEXT, base_amount INTEGER NOT NULL, price INTEGER NOT NULL,
                status TEXT NOT NULL, stard_ref TEXT, failure_reason TEXT, refunded INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO users VALUES (7, 'old', 'Old', 5000, 0, '2026-01-01T00:00:00Z');
            INSERT INTO orders(user_id,type,category,title,base_amount,price,status,created_at,updated_at)
                VALUES (7,'stars','stars','t',1,2,'completed','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
        """)
        await c.commit()
    d = Database(path)
    await d.connect()
    u = await d.get_user(7)
    assert u.balance == 5000 and u.referrer_id is None
    o = await d.get_order(1)
    assert o["discount"] == 0 and o["ref_paid"] == 0 and o["duration"] is None
    await d.close()


async def test_refund_releases_coupon(db):
    await db.credit(1, 100_000, "topup")
    await db.create_coupon("X", 10, 1)
    oid = await db.create_order_and_debit(user_id=1, type_="stars", category="stars", product_id=None, title="t",
                                          quantity=50, recipient="@a", gift_message=None, quote_id=None,
                                          base_amount=1000, price=9000, coupon="X", discount=1000)
    assert (await db.get_coupon("X"))["used"] == 1
    assert await db.refund_order(oid, "failed")
    assert (await db.get_coupon("X"))["used"] == 0
    assert not await db.coupon_used_by("X", 1)


async def test_coupon_race_cannot_exceed_max_uses(db):
    from bot.db import CouponInvalid
    await db.credit(1, 1_000_000, "topup")
    await db.upsert_user(2, "b", "B")
    await db.credit(2, 1_000_000, "topup")
    await db.create_coupon("ONE", 10, 1)

    async def buy(uid):
        return await db.create_order_and_debit(user_id=uid, type_="stars", category="stars", product_id=None,
                                               title="t", quantity=50, recipient="@a", gift_message=None,
                                               quote_id=None, base_amount=1, price=1000, coupon="ONE", discount=100)
    res = await asyncio.gather(buy(1), buy(2), return_exceptions=True)
    assert sum(isinstance(r, CouponInvalid) for r in res) == 1
    assert (await db.get_coupon("ONE"))["used"] == 1
    # تراکنش ناموفق هیچ پولی کم نکرده
    total = (await db.get_user(1)).balance + (await db.get_user(2)).balance
    assert total == 2_000_000 - 1000


async def test_completed_order_never_refunded(db):
    await db.credit(1, 10_000, "topup")
    oid = await db.create_order_and_debit(user_id=1, type_="stars", category="stars", product_id=None, title="t",
                                          quantity=50, recipient="@a", gift_message=None, quote_id=None,
                                          base_amount=5000, price=6000)
    await db.update_order(oid, status="completed")
    assert await db.refund_order(oid, "refunded") is False
    assert (await db.get_user(1)).balance == 4000


async def test_referral_reward_capped_at_profit(db):
    await db.upsert_user(2, "ref", "Ref")
    await db.upsert_user(3, "c", "C", referrer_id=2)
    await db.credit(3, 100_000, "topup")
    oid = await db.create_order_and_debit(user_id=3, type_="stars", category="stars", product_id=None, title="t",
                                          quantity=50, recipient="@a", gift_message=None, quote_id=None,
                                          base_amount=9_900, price=10_000)
    assert await db.pay_referral(oid, 50) is None  # هنوز انجام نشده
    await db.update_order(oid, status="completed")
    assert await db.pay_referral(oid, 50) == (2, 100)  # سقف = سود ۱۰۰ تومان، نه ۵۰٪
    assert await db.pay_referral(oid, 50) is None
    assert (await db.get_user(2)).balance == 100


async def test_self_referral_ignored(db):
    u, created = await db.upsert_user(9, "x", "X", referrer_id=9)
    assert created and u.referrer_id is None
    u, created = await db.upsert_user(10, "y", "Y", referrer_id=12345)  # معرف ناموجود
    assert u.referrer_id is None
    u, created = await db.upsert_user(10, "y", "Y", referrer_id=1)       # بعداً عوض نمی‌شود
    assert not created and u.referrer_id is None


async def test_boost_order_and_sync(shop, db, fake):
    await db.credit(1, 5_000_000, "topup")
    offer = await shop.boost_offer(10, 7)
    assert offer.base_amount == 132_000 and offer.price == apply_profit(132_000, 10)
    oid = await shop.place_order(1, offer, "@chan")
    assert (await db.get_order(oid))["stard_ref"] == "ord_test_1"
    fake.orders["ord_test_1"]["status"] = "refunded"
    assert await shop.sync_order(oid) == ("pending", "refunded")
    assert (await db.get_user(1)).balance == 5_000_000
    assert ("GET", "/boosts/orders/ord_test_1") in fake.calls
    with pytest.raises(ShopError):
        await shop.boost_offer(10, 14)  # مدت ناموجود
    with pytest.raises(ShopError):
        await shop.boost_offer(5000, 7)  # بیشتر از سقف


async def test_admin_refund_cancels_at_stard_first(shop, db, fake):
    await db.credit(1, 5_000_000, "topup")
    oid = await shop.place_order(1, await shop.stars_offer(100), "@bob")
    assert await shop.admin_refund(oid) is True
    assert fake.orders["ord_test_1"]["status"] == "cancelled"
    assert (await db.get_user(1)).balance == 5_000_000
    # سفارش شروع‌شده در Stard لغو نمی‌شود و پول هم برنمی‌گردد
    oid2 = await shop.place_order(1, await shop.stars_offer(100), "@bob")
    fake.orders["ord_test_2"]["status"] = "processing"
    with pytest.raises(ShopError):
        await shop.admin_refund(oid2)
    assert not (await db.get_order(oid2))["refunded"]


async def test_catalog_is_cached(shop, fake):
    await shop.gifts()
    await shop.gifts()
    await shop.rates()
    await shop.rates()
    assert fake.calls.count(("GET", "/gifts")) == 1
    assert fake.calls.count(("GET", "/prices")) == 1


async def test_settings_cache_consistent(db):
    await db.set_setting("a", 1)
    assert await db.get_setting("a") == "1"
    await db.del_setting("a")
    assert await db.get_setting("a", "x") == "x"


@requires_sqlite
async def test_backup(db, tmp_path):
    await db.credit(1, 777, "topup")
    dest = str(tmp_path / "b.db")
    await db.backup(dest)
    d2 = Database(dest)
    await d2.connect()
    assert (await d2.get_user(1)).balance == 777
    await d2.close()


async def test_admin_refund_of_lost_response_order_does_not_double_spend(shop, db, fake):
    await db.credit(1, 5_000_000, "topup")
    offer = await shop.stars_offer(100)
    fake.drop_responses = 3  # سفارش در Stard ثبت شد ولی پاسخ نرسید؛ محلی 'new' می‌ماند
    oid = await shop.place_order(1, offer, "@bob")
    assert (await db.get_order(oid))["status"] == "new"
    fake.drop_responses = 3  # Stard هنوز در دسترس نیست → نباید پول را برگرداند
    with pytest.raises(ShopError):
        await shop.admin_refund(oid)
    assert not (await db.get_order(oid))["refunded"]
    # شبکه برگشت: سفارش واقعی پیدا و در Stard لغو می‌شود، بعد پول برمی‌گردد
    assert await shop.admin_refund(oid) is True
    assert len(fake.orders) == 1 and fake.orders["ord_test_1"]["status"] == "cancelled"
    assert (await db.get_user(1)).balance == 5_000_000
