"""Feature Flag، دکمه‌ها، قیمت‌گذاری پویا، VIP، انبار و سقف خرید، ریسک، Test Mode، پاداش و گزارش مالی."""
import asyncio
import random

import pytest

from bot import finance
from bot.db import ago, later
from bot.features import RewardError, claim_reward, in_rollout
from bot.pricing import apply_profit
from bot.risk import RiskEngine
from bot.shop import Shop, ShopError
from bot.stard_api import StardClient
from tests.conftest import new_db
from tests.fake_stard import FakeStard


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr("bot.stard_api._sleep", fast)


@pytest.fixture
async def env():
    db = await new_db()
    fake = FakeStard()
    api = StardClient("sk_test_ok", transport=fake.transport(), max_retries=0)
    shop = Shop(db, api, default_profit=10)
    u, _ = await db.upsert_user(1, "alice", "Alice", language_code="fa")
    await db.credit(1, 50_000_000, "topup")
    yield db, shop, fake, u
    await api.close()
    await db.close()


# ---------- Feature Flag و دکمه‌ها ----------
def test_rollout_is_deterministic_and_proportional():
    assert in_rollout("x", 5, 100) and not in_rollout("x", 5, 0)
    inside = sum(in_rollout("spin", uid, 25) for uid in range(10_000))
    assert 2200 < inside < 2800
    assert all(in_rollout("spin", u, 25) == in_rollout("spin", u, 25) for u in range(100))


async def test_flags_and_buttons_gate_categories(env):
    db, shop, fake, u = env
    f = shop.features
    assert await shop.enabled_categories(1) == ["stars", "star_gift", "premium", "boost", "reaction"]  # nft/... خاموش
    await f.set("nft_shop", enabled=True, admin_id=9)
    assert "nft" in await shop.enabled_categories(1)
    await f.toggle_button("stars", "enabled", admin_id=9)
    assert ("stars", False) in await f.shop_buttons(1)          # دیده می‌شود ولی خریدنی نیست
    with pytest.raises(ShopError):
        await shop.stars_offer(100, user=u)
    await f.toggle_button("stars", "visible", admin_id=9)
    assert "stars" not in [k for k, _ in await f.shop_buttons(1)]
    await f.move_button("boost", -10)
    assert (await f.buttons())[0]["key"] == "boost"
    await f.set("premium_shop", rollout=0)
    assert "premium" not in await shop.enabled_categories(1)
    assert [a["action"] for a in await db.audit_entries(limit=10)].count("flag_set") == 2


async def test_legacy_v2_category_switch_is_respected(env):
    db, shop, *_ = env
    await db.set_setting("cat:boost", "0")
    assert "boost" not in await shop.enabled_categories(1)


# ---------- قیمت‌گذاری ----------
async def test_flash_sale_scheduled_rule_vip_and_cost_floor(env):
    db, shop, fake, u = env
    c = shop.commerce
    base_price = apply_profit(500 * 4382, 10)
    assert (await shop.stars_offer(500, user=u)).price == base_price           # بدون قانون: همان قیمت v2
    rid = await c.add_rule(name="Flash", percent=-5, category="stars", ends_at=later(3600))
    await c.add_rule(name="Future", percent=-50, category="stars", starts_at=later(3600))  # هنوز شروع نشده
    await c.add_rule(name="Expired", percent=-50, category="stars", ends_at=ago(1))
    off = await shop.stars_offer(500, user=u)
    assert off.price < base_price and any("Flash" in n for n in off.notes)
    await c.set_level(1, "Gold", 3)
    await c.assign(1, 1, days=30)
    vip = await shop.stars_offer(500, user=u)
    assert vip.price < off.price and any("Gold" in n for n in vip.notes)
    await c.add_rule(name="Crazy", percent=-90, category="stars")
    floor = await shop.stars_offer(500, user=u)
    assert floor.price >= floor.base_amount                                       # هرگز زیر قیمت خرید
    await c.toggle_rule(rid)
    assert await c.matching_rules("stars", None, u, None) != []


async def test_vip_auto_level_by_spending_and_segment_rule(env):
    db, shop, fake, u = env
    c = shop.commerce
    await c.set_level(2, "Silver", 0, min_spent=1)
    assert await c.user_level(1) is None
    oid = await shop.place_order(1, await shop.stars_offer(50, user=u), "@bob", user=u)
    await db.update_order(oid, status="completed")
    lvl = await c.user_level(1)
    assert lvl["name"] == "Silver" and lvl["source"] == "auto"
    await c.add_rule(name="VIP only", percent=-10, segment="vip:2")
    assert (await shop.stars_offer(100, user=u)).notes


async def test_stock_limit_visibility_and_language(env):
    db, shop, fake, u = env
    c = shop.commerce
    await c.set_control("stars", stock=150, daily_limit=120)
    offer = await shop.stars_offer(100, user=u)
    await shop.place_order(1, offer, "@bob", user=u)
    with pytest.raises(ShopError, match="موجودی|سقف"):
        await shop.place_order(1, await shop.stars_offer(100, user=u), "@bob", user=u)
    assert (await c.control("stars"))["sold"] == 100
    await c.set_control("stars", languages="en", daily_limit=None, stock=None)
    with pytest.raises(ShopError, match="منطقه"):
        await shop.place_order(1, await shop.stars_offer(50, user=u), "@bob", user=u)
    await shop.place_order(1, await shop.stars_offer(50, user=u), "@bob", user=u, is_admin=True)  # مدیر معاف
    await c.set_control("premium:4518", hidden=1)
    assert await shop.premium_plans(user=u) == []
    with pytest.raises(ShopError):
        await shop.product_offer(4518, "premium", user=u)


async def test_stock_is_atomic_under_concurrency(env):
    db, shop, fake, u = env
    await shop.commerce.set_control("stars", stock=200)
    for uid in range(10, 20):
        await db.upsert_user(uid, None, "U")
        await db.credit(uid, 5_000_000, "topup")
    offer = await shop.stars_offer(50)
    res = await asyncio.gather(*(shop.place_order(uid, offer, "@bob", checkout_id=f"c{uid}") for uid in range(10, 20)),
                               return_exceptions=True)
    assert sum(isinstance(r, int) for r in res) == 4
    assert (await shop.commerce.control("stars"))["sold"] == 200


async def test_refund_releases_stock(env):
    db, shop, fake, u = env
    await shop.commerce.set_control("stars", stock=100)
    oid = await shop.place_order(1, await shop.stars_offer(100), "@bob")
    await db.refund_order(oid, "failed")
    assert (await shop.commerce.control("stars"))["sold"] == 0


# ---------- پیشنهاد اختصاصی ----------
async def test_personal_offer_is_user_bound_and_expires(env):
    db, shop, fake, u = env
    await db.upsert_user(2, "bob", "Bob")
    await db.create_coupon("ALICE20", 20, 1, user_id=1, category="stars", expires_at=later(3600))
    offer = await shop.stars_offer(100, user=u)
    final, off = await shop.check_coupon("alice20", 1, offer)
    assert off > 0
    with pytest.raises(ShopError):
        await shop.check_coupon("ALICE20", 2, offer)                       # مال کاربر دیگر
    with pytest.raises(ShopError):
        await shop.check_coupon("ALICE20", 1, await shop.boost_offer(5, 7))  # بخش دیگر
    await db.write("UPDATE coupons SET expires_at = :t", {"t": ago(1)})
    with pytest.raises(ShopError, match="مهلت"):
        await shop.check_coupon("ALICE20", 1, offer)


# ---------- Test Mode ----------
async def test_test_mode_uses_simulator_and_is_excluded_from_stats(env):
    db, shop, fake, u = env
    await db.set_setting("test_mode", "admins")
    assert await shop.is_test_for(True) and not await shop.is_test_for(False)
    offer = await shop.stars_offer(100, user=u, test=True)
    oid = await shop.place_order(1, offer, "@bob")
    o = await db.get_order(oid)
    assert o["is_test"] == 1 and o["stard_ref"].startswith("ord_sim_") and fake.orders == {}
    await db.write("UPDATE orders SET created_at = :t WHERE id = :i", {"t": ago(1), "i": oid})
    assert await shop.sync_order(oid) == ("pending", "completed")
    assert (await db.stats())["done"] == 0                                 # سفارش آزمایشی در آمار نیست
    assert not any(c for c in fake.calls if c[1] == "/orders")             # هیچ درخواستی به API واقعی نرفت


# ---------- ریسک ----------
async def test_risk_score_and_auto_ban_rules(env):
    db, shop, fake, u = env
    bans = []

    async def on_ban(uid, score, reasons):
        bans.append(uid)
    risk = RiskEngine(db, on_ban=on_ban, is_admin=lambda uid: uid == 99)
    for _ in range(20):
        await risk.record(1, "spam")
    assert (await db.get_user(1)).risk_score == 100 and not (await db.get_user(1)).banned  # خاموش (پیش‌فرض)
    await risk.set_rules(enabled=True, threshold=50, min_kinds=2)
    await risk.record(1, "spam")
    assert not (await db.get_user(1)).banned           # فقط یک نوع رفتار → Ban نمی‌شود
    await risk.record(1, "coupon_fail")
    assert (await db.get_user(1)).banned and bans == [1]
    actions = [a["action"] for a in await db.audit_entries(limit=10)]
    assert actions.index("ban") < actions.index("auto_ban_evidence")    # شواهد قبل از Ban ثبت شد
    await db.upsert_user(99, "adm", "A")
    for k in ("spam", "coupon_fail", "rate_limit") * 10:
        await risk.record(99, k)
    assert not (await db.get_user(99)).banned           # مدیر هرگز
    await risk.reset(1, admin_id=5)
    assert (await db.get_user(1)).risk_score == 0


# ---------- پاداش ----------
async def test_daily_reward_once_per_day_even_concurrently(env):
    db, shop, fake, u = env
    with pytest.raises(RewardError):
        await claim_reward(db, 1, "daily")
    await db.set_setting("daily_reward_amount", 5000)
    before = (await db.get_user(1)).balance
    res = await asyncio.gather(*(claim_reward(db, 1, "daily") for _ in range(5)), return_exceptions=True)
    assert sum(r == 5000 for r in res) == 1
    assert (await db.get_user(1)).balance == before + 5000
    amount = await claim_reward(db, 1, "spin", rng=random.Random(1))
    assert amount in (0, 1000, 5000, 20000)
    with pytest.raises(RewardError):
        await claim_reward(db, 1, "spin")
    assert not await db.ledger_balance_check()


# ---------- مالی ----------
async def _completed(db, uid, price, base, refunded=False):
    oid = await db.create_order_and_debit(user_id=uid, type_="stars", category="stars", product_id=None, title="t",
                                          quantity=50, recipient="@a", gift_message=None, quote_id=None,
                                          base_amount=base, price=price)
    if refunded:
        await db.refund_order(oid, "failed", "api")
    else:
        await db.update_order(oid, status="completed")
    return oid


async def test_finance_summary_fees_and_profit(env):
    db, shop, fake, u = env
    await _completed(db, 1, 110_000, 100_000)
    await _completed(db, 1, 220_000, 200_000)
    await _completed(db, 1, 55_000, 50_000, refunded=True)
    await db.set_json("fees", {"payment_percent": 1, "payment_fixed": 100})
    a, b, _ = finance.period_range("day")
    s = await finance.summary(db, a, b)
    assert s.revenue == 330_000 and s.cost == 300_000 and s.orders == 2 and s.aov == 165_000
    assert s.fees == 3300 + 200 and s.net_profit == 30_000 - 3500 and s.refunds == 55_000
    dash = await finance.revenue_dashboard(db)
    assert dash["today"]["cur"].revenue == 330_000 and dash["today"]["growth"] == "جدید"
    daily = await finance.series(db, "daily", 7)
    assert len(daily) == 7 and daily[-1][1] == 330_000
    assert len(await finance.series(db, "monthly", 3)) == 3 and len(await finance.series(db, "weekly", 4)) == 4
    chart = finance.text_chart([(lbl, rev) for lbl, rev, _ in daily])
    assert "█" in chart and "330K" in chart
    fb = finance.fee_breakdown(100_000, 80_000, {**finance.DEFAULT_FEES, "telegram_percent": 2}, refund_rate=1)
    assert fb["net"] == 100_000 - 80_000 - 2000 - 1000 and fb["margin"] == 17.0


async def test_ledger_and_transaction_search(env):
    db, shop, fake, u = env
    oid = await _completed(db, 1, 55_000, 50_000, refunded=True)
    rows = await finance.ledger_search(db, f"user:1 type:refund order:{oid}")
    assert len(rows) == 1 and rows[0]["amount"] == 55_000
    assert await finance.ledger_search(db, "min:999999999") == []
    assert (await finance.order_search(db, f"{oid}"))[0]["id"] == oid
    assert await finance.order_search(db, "status:completed") == []
    today = finance.utc_today().isoformat()
    assert await finance.ledger_search(db, f"from:{today} to:{today}")
    assert (await finance.refund_counts(db))["completed"]["n"] == 1


async def test_reports_csv_xlsx_pdf(env):
    db, shop, fake, u = env
    await _completed(db, 1, 110_000, 100_000)
    await db.write("UPDATE orders SET title = '=HYPERLINK(1)'")   # تزریق فرمول
    a, b, label = finance.period_range("month")
    csv_data = await finance.export_csv(db, a, b)
    assert b"'=HYPERLINK" in csv_data and b"110000" in csv_data
    xlsx = await finance.export_xlsx(db, a, b, label)
    assert xlsx[:2] == b"PK"
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(xlsx))
    assert wb["Summary"]["B2"].value == 110_000 and wb["Orders"]["E2"].value.startswith("'=")
    pdf = await finance.export_pdf(db, a, b, label)
    assert pdf[:4] == b"%PDF"


async def test_failed_payments_lists_api_rejections(env):
    db, shop, fake, u = env
    fake.balance = 0
    with pytest.raises(ShopError):
        await shop.place_order(1, await shop.stars_offer(50), "@bob")
    rows = await finance.failed_payments(db)
    assert rows and rows[0]["failure_reason"] == "insufficient_funds"


# ---------- ابزار سیستمی ----------
async def test_security_scan_and_db_stats(env, tmp_path):
    db, shop, fake, u = env
    from bot.config import Settings
    from bot.systools import db_stats, security_scan
    s = Settings(bot_token="bad", stard_api_key="nope", admin_ids=[], log_level="DEBUG", http_host="0.0.0.0",
                 log_dir=str(tmp_path), backup_dir=str(tmp_path / "b"))
    findings = await security_scan(s, db, run_pip_audit=False)
    titles = " | ".join(f.title for f in findings)
    assert findings[0].severity == "critical" and "ADMIN_IDS" in titles and "Debug" in titles
    assert "HTTP" in titles and not any("سورس‌کد" in f.title for f in findings)
    st = await db_stats(db)
    assert st["users"] == 1 and st["tables"]["orders"]["rows"] == 0 and "orders" in st["indexes"]
