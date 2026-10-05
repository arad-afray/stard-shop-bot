"""پنل مدیریت v3 و قابلیت‌های جدید کاربر از طریق Dispatcher واقعی: همه‌ی صفحه‌ها و فرم‌ها باید بدون خطا کار کنند."""
import pytest

from bot.ui import SA, Act, Adm, CatPage, Fn, Nav, Op, PickProduct, StarsQty
from bot.updater import ReleaseInfo
from tests.helpers import ADMIN, CUSTOMER, click, click_raw, send

pytestmark = pytest.mark.usefixtures("no_unhandled_errors")


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """بدون اینترنت: GitHub، PyPI و pip-audit شبیه‌سازی می‌شوند؛ پشتیبان و لاگ در پوشه‌ی موقت."""
    async def check(self):
        return ReleaseInfo(current="2.0.0", latest="99.0.0", tag="v99.0.0", newer=True, changelog="- big release",
                           published_at="2026-10-05 10:00")

    async def deps(**_):
        return {"python": "3.11", "python_ok": True, "packages": [{"name": "aiogram", "current": "3", "latest": "3",
                                                                    "status": "ok"}],
                "docker_base": "python:3.12-slim", "node": "—", "system": {"git": True}}

    async def no_audit():
        return []
    monkeypatch.setattr("bot.updater.Updater.check", check)
    monkeypatch.setattr("bot.handlers.ops.dependency_report", deps)
    monkeypatch.setattr("bot.systools._pip_audit", no_audit)
    monkeypatch.chdir(tmp_path)


async def _orders(db, n=3):
    await db.upsert_user(CUSTOMER, "user200", "U")
    await db.credit(CUSTOMER, 50_000_000, "topup")
    for i in range(n):
        oid = await db.create_order_and_debit(user_id=CUSTOMER, type_="stars", category="stars", product_id=None,
                                              title="⭐ 50", quantity=50, recipient="@a", gift_message=None,
                                              quote_id=None, base_amount=200_000, price=250_000)
        if i == 0:
            await db.update_order(oid, status="completed")
        elif i == 1:
            await db.refund_order(oid, "failed", "insufficient_funds")


async def test_every_admin_page_renders(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await _orders(db)
    pages = [
        Adm(name="home"), Op(a="menu"), Op(a="hc"), Op(a="res"), Op(a="logs"), Op(a="logl", v="ERROR"),
        Op(a="logs_svc", v="worker"), Op(a="logx"), Op(a="bk"), Op(a="bkc"), Op(a="upd"), Op(a="updlog"),
        Op(a="mig"), Op(a="diag"), Op(a="keys"), Op(a="wh"), Op(a="q"), Op(a="sec"), Op(a="dbs"), Op(a="deps"),
        Op(a="audit"), Op(a="al"), Op(a="alt", v="disk_high"), Op(a="maint"),
        SA(a="menu"), SA(a="mkt"), SA(a="btn"), SA(a="bt", v="stars|u"), SA(a="bt", v="boost|e"),
        SA(a="bt", v="boost|i"), SA(a="flags"), SA(a="fl", v="nft_shop|t"), SA(a="fl", v="spin|r"),
        SA(a="rules"), SA(a="ctl"), SA(a="vip"), SA(a="rew"), SA(a="tm"), SA(a="tms", v="admins"), SA(a="slug"),
        SA(a="inact"), SA(a="cb"), SA(a="cbt"), SA(a="ntf"), SA(a="ntft", v="refund"), SA(a="risk"), SA(a="riskt"),
        Fn(a="menu"), Fn(a="rev", v="daily"), Fn(a="rev", v="weekly"), Fn(a="rev", v="monthly"),
        Fn(a="profit", v="day"), Fn(a="profit", v="total"), Fn(a="ledger"), Fn(a="ref"), Fn(a="ref", v="completed"),
        Fn(a="failed"), Fn(a="rep"), Fn(a="repx", v="month|csv"), Fn(a="repx", v="week|xlsx"),
        Fn(a="repx", v="day|pdf"), Fn(a="fee"), Adm(name="cats"), Adm(name="stats"), Adm(name="wallet"),
    ]
    for p in pages:
        await click(dp, bot, ADMIN, p)
    docs = [m for m in session.sent if type(m).__name__ == "SendDocument"]
    assert len(docs) == 3  # CSV، Excel، PDF
    assert any("COMMAND CENTER" in t for t in session.texts())
    assert any("Health Check" in t for t in session.texts())
    assert any("v99.0.0" in t for t in session.texts())
    assert await db.audit_entries(action="flag_set") and await db.audit_entries(action="test_mode")


async def test_admin_forms(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await _orders(db)
    forms = [
        (SA(a="flash"), "stars 10 6", "Flash Sale"),
        (SA(a="rule+"), "Yalda -8 star_gift all now +2d", "ثبت شد"),
        (SA(a="rule+"), "bad input", "نادرست"),
        (SA(a="ctl+"), "stars stock 1000", "ذخیره شد"),
        (SA(a="ctl+"), "premium:4518 hidden 1", "ذخیره شد"),
        (SA(a="ctl+"), "low_stock 100", "ذخیره شد"),
        (SA(a="vip+"), "1 Gold 5 - 1000000", "ذخیره شد"),
        (SA(a="rewa"), "5000", "ذخیره شد"),
        (SA(a="spin"), "0:50 1000:50", "ذخیره شد"),
        (SA(a="slug+"), "username usernames", "ذخیره شد"),
        (SA(a="cbe"), "14 سلام برگردید", "ذخیره شد"),
        (SA(a="riske"), "60 2 24", "ذخیره شد"),
        (SA(a="uoffer", v=str(CUSTOMER)), "15 1 3", "پیشنهاد ساخته شد"),
        (SA(a="uvip", v=str(CUSTOMER)), "1 30", "انجام شد"),
        (Op(a="logq"), "worker", "فیلتر اعمال شد"),
        (Op(a="logr"), "2026-10-01 2026-10-05", "بازه اعمال شد"),
        (Op(a="alth"), "disk_percent 85", "disk_percent"),
        (Op(a="mainttxt"), "به‌زودی برمی‌گردیم", "ذخیره شد"),
        (Op(a="keyc"), "grafana metrics:read", "sbk_"),
        (Fn(a="lq"), f"user:{CUSTOMER} type:refund", "Ledger"),
        (Fn(a="tx"), f"user:{CUSTOMER}", "نتیجه"),
        (Fn(a="repc"), "2026-10-01 2026-10-31", "بازه"),
        (Fn(a="fees"), "0 0 1.5 500", "ذخیره شد"),
        (Fn(a="calc"), "2474000 2249000 1", "Net Profit"),
    ]
    for cd, text, expect in forms:
        await click(dp, bot, ADMIN, cd)
        await send(dp, bot, ADMIN, text)
        joined = " ".join(session.texts()[-3:])
        assert expect in joined, (cd, text, joined[-300:])
    # کارت کاربر با آمار کامل
    await click(dp, bot, ADMIN, Adm(name="ucard", arg=str(CUSTOMER)))
    card = session.texts()[-1]
    for field in ("Total Orders", "Successful", "Failed", "Total Spent", "Total Profit", "Balance", "Referrals",
                  "Last Activity", "Registration", "Gold"):
        assert field in card, field
    assert (await db.list_coupons(user_id=CUSTOMER))[0]["percent"] == 15
    assert any("پیشنهاد اختصاصی" in t for t in session.texts())   # کاربر خبردار شد


async def test_owner_only_dangerous_actions(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await send(dp, bot, CUSTOMER, "/start")
    await db.set_json("admins", [CUSTOMER])  # مدیر غیرمالک
    dp.workflow_data["admins"].extra = {CUSTOMER}
    for cd in (Op(a="upd?"), Op(a="mig?"), Op(a="keyc"), Op(a="bkr", v="20261005-101010-manual.zip")):
        await click(dp, bot, CUSTOMER, cd)
        assert "فقط مالک" in session.alerts()[-1]


async def test_maintenance_blocks_users_not_admins(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await click(dp, bot, ADMIN, Op(a="maint!"))
    await send(dp, bot, CUSTOMER, "🛍 فروشگاه")
    assert "نگهداری" in session.texts()[-1] or "Maintenance" in session.texts()[-1] or "به‌روزرسانی" in session.texts()[-1]
    await send(dp, bot, ADMIN, "🛍 فروشگاه")
    assert "فروشگاه" in session.texts()[-1]
    await click(dp, bot, ADMIN, Op(a="maint!"))
    await send(dp, bot, CUSTOMER, "🛍 فروشگاه")
    assert "دسته‌ی مورد نظر" in session.texts()[-1]


async def test_disabled_button_shows_lock_and_hidden_disappears(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await click(dp, bot, ADMIN, SA(a="bt", v="boost|e"))     # غیرفعال
    await click(dp, bot, ADMIN, SA(a="bt", v="reaction|v"))  # مخفی
    await send(dp, bot, CUSTOMER, "🛍 فروشگاه")
    kb = [b.text for row in session.sent[-1].reply_markup.inline_keyboard for b in row]
    assert any("بوست" in t and "🔒" in t for t in kb) and not any("ریکشن" in t for t in kb)
    await click(dp, bot, CUSTOMER, Nav(to="boost"))
    assert "غیرفعال" in session.alerts()[-1]


async def test_nft_catalog_purchase_with_flag(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await click(dp, bot, ADMIN, SA(a="fl", v="nft_shop|t"))
    await send(dp, bot, CUSTOMER, "/start")
    await db.credit(CUSTOMER, 100_000_000, "topup")
    await click(dp, bot, CUSTOMER, Nav(to="nft"))
    assert "Plush Pepe" in str(session.sent[-2].reply_markup) or "Plush Pepe" in str(session.sent[-1])
    await click(dp, bot, CUSTOMER, CatPage(cat="nft", page=0))
    await click(dp, bot, CUSTOMER, PickProduct(cat="nft", pid=7001))
    await send(dp, bot, CUSTOMER, "@user200")
    await click(dp, bot, CUSTOMER, Act(name="confirm"))
    o = (await db.recent_orders())[0]
    assert o["category"] == "nft" and o["product_id"] == 7001 and o["stard_ref"]


async def test_test_mode_purchase_for_admin(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await click(dp, bot, ADMIN, SA(a="tms", v="admins"))
    await db.credit(ADMIN, 10_000_000, "topup")
    await click(dp, bot, ADMIN, StarsQty(qty=100))
    await send(dp, bot, ADMIN, "@friend_one")
    assert "Test Mode" in session.texts()[-1]
    await click(dp, bot, ADMIN, Act(name="confirm"))
    o = (await db.recent_orders())[0]
    assert o["is_test"] == 1 and o["stard_ref"].startswith("ord_sim_") and fake.orders == {}


async def test_rewards_and_account_page(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await click(dp, bot, ADMIN, SA(a="fl", v="daily_reward|t"))
    await db.set_setting("daily_reward_amount", 3000)
    await send(dp, bot, CUSTOMER, "/start")
    await send(dp, bot, CUSTOMER, "👤 حساب کاربری")
    kb = str(session.sent[-1].reply_markup)
    assert "rw:daily" in kb and "rw:spin" not in kb
    await click_raw(dp, bot, CUSTOMER, "rw:daily")
    assert "3,000" in session.texts()[-1]
    await click_raw(dp, bot, CUSTOMER, "rw:daily")
    assert "فردا" in session.alerts()[-1]
    await click_raw(dp, bot, CUSTOMER, "rw:spin")           # flag خاموش
    assert "فعال نیست" in session.alerts()[-1]


async def test_inactive_campaign_and_broadcast_queue(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await db.upsert_user(555, "old", "Old")
    await db.write("UPDATE users SET last_seen = '2020-01-01T00:00:00Z' WHERE id = 555")
    await click(dp, bot, ADMIN, SA(a="camp", v="30"))
    await send(dp, bot, ADMIN, "برگرد! کد BACK10")
    await click(dp, bot, ADMIN, SA(a="camp!"))
    b = await db.one("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 1")
    assert b["segment"] == "inactive:30" and b["status"] == "queued"
    job = await db.one("SELECT * FROM jobs WHERE kind = 'broadcast'")
    assert job is not None


async def test_refund_center_and_order_refund_flow(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await send(dp, bot, CUSTOMER, "/start")
    await db.credit(CUSTOMER, 10_000_000, "topup")
    await click(dp, bot, CUSTOMER, StarsQty(qty=100))
    await send(dp, bot, CUSTOMER, "@friend_one")
    await click(dp, bot, CUSTOMER, Act(name="confirm"))
    o = (await db.recent_orders())[0]
    fake.orders["ord_test_1"]["status"] = "processing"     # شروع‌شده؛ لغو ممکن نیست
    await click(dp, bot, ADMIN, Adm(name="o_refund", arg=str(o["id"])))
    r = await db.one("SELECT * FROM refunds WHERE order_id = :o", {"o": o["id"]})
    assert r["status"] == "failed" and r["error"]
    fake.orders["ord_test_1"]["status"] = "pending"
    await click(dp, bot, ADMIN, Adm(name="o_refund", arg=str(o["id"])))
    r = await db.one("SELECT * FROM refunds WHERE order_id = :o", {"o": o["id"]})
    assert r["status"] == "completed"
    await click(dp, bot, ADMIN, Fn(a="ref"))
    assert "Completed" in session.texts()[-1]
