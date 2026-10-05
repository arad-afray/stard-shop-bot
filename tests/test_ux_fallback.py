"""خطای پیش‌بینی‌نشده هنگام خرید: کاربر باید دقیق بداند سفارش ثبت شد یا نه (تا دوباره خرید نکند)."""
from bot.shop import Shop
from bot.ui import Act, StarsQty
from tests.helpers import CUSTOMER, click, send


async def _to_confirm(dp, bot, db):
    await send(dp, bot, CUSTOMER, "/start")
    await db.credit(CUSTOMER, 10_000_000, "topup")
    await click(dp, bot, CUSTOMER, StarsQty(qty=100))
    await send(dp, bot, CUSTOMER, "@friend_one")


async def test_error_after_order_created_tells_user_it_is_registered(env, monkeypatch):
    dp, bot, session, db, fake = env
    real = Shop._place_order

    async def created_then_crash(self, *a, **k):
        await real(self, *a, **k)
        raise TimeoutError("pool exhausted")
    monkeypatch.setattr(Shop, "_place_order", created_then_crash)
    await _to_confirm(dp, bot, db)
    await click(dp, bot, CUSTOMER, Act(name="confirm"))
    assert "ثبت شده" in session.texts()[-1] and "دوباره خرید نکنید" in session.texts()[-1]
    assert len(await db.recent_orders()) == 1


async def test_error_before_order_tells_user_nothing_was_charged(env, monkeypatch):
    dp, bot, session, db, fake = env

    async def crash(self, *a, **k):
        raise TimeoutError("pool exhausted")
    monkeypatch.setattr(Shop, "_place_order", crash)
    await _to_confirm(dp, bot, db)
    await click(dp, bot, CUSTOMER, Act(name="confirm"))
    assert "هیچ مبلغی کسر نشده" in session.texts()[-1]
    assert (await db.get_user(CUSTOMER)).balance == 10_000_000
