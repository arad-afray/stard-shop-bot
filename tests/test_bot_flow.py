"""تست سرتاسری: آپدیت‌های واقعی تلگرام از Dispatcher عبور می‌کنند (بدون اینترنت)."""
import datetime as dt
import itertools

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser

from bot.config import Settings
from bot.db import Database
from bot.handlers import admin, shop as shop_handlers, topup, user
from bot.middlewares import UserMiddleware
from bot.shop import Shop
from bot.stard_api import StardClient
from bot.ui import Act, Adm, StarsQty
from tests.fake_stard import FakeStard

ADMIN, CUSTOMER = 100, 200
ids = itertools.count(1)


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        name = type(method).__name__
        if name in ("SendMessage", "SendPhoto", "EditMessageText", "EditMessageCaption", "CopyMessage"):
            if name == "CopyMessage":
                return method.__returning__(message_id=next(ids))
            return Message(message_id=next(ids), date=dt.datetime.now(),
                           chat=Chat(id=getattr(method, "chat_id", None) or 1, type="private"),
                           text=getattr(method, "text", None) or "")
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    def texts(self) -> list[str]:
        return [getattr(m, "text", None) or getattr(m, "caption", None) or "" for m in self.sent]


@pytest.fixture
async def env():
    db = Database(":memory:")
    await db.connect()
    fake = FakeStard()
    api = StardClient("sk_test_ok", transport=fake.transport())
    shop = Shop(db, api, default_profit=10)
    settings = Settings(bot_token="1:x", stard_api_key="sk_test_ok", admin_ids=[ADMIN])
    session = RecordingSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    mw = UserMiddleware(db, settings.admin_ids)
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    for r in (user.router, admin.router, topup.router, shop_handlers.router):
        r._parent_router = None  # روترها ماژول‌سطح‌اند؛ برای هر تست دوباره وصل می‌شوند
    dp.include_routers(user.router, admin.router, topup.router, shop_handlers.router)
    dp.workflow_data.update(db=db, shop=shop, settings=settings, admin_ids=settings.admin_ids)
    yield dp, bot, session, db, fake
    await api.close()
    await db.close()


def tg_user(uid):
    return TgUser(id=uid, is_bot=False, first_name=f"U{uid}", username=f"user{uid}")


async def send(dp, bot, uid, text=None, photo=None):
    msg = Message(message_id=next(ids), date=dt.datetime.now(), chat=Chat(id=uid, type="private"),
                  from_user=tg_user(uid), text=text, photo=photo)
    await dp.feed_update(bot, Update(update_id=next(ids), message=msg))


async def click(dp, bot, uid, data):
    msg = Message(message_id=next(ids), date=dt.datetime.now(), chat=Chat(id=uid, type="private"), text="x",
                  from_user=tg_user(42))
    cb = CallbackQuery(id=str(next(ids)), from_user=tg_user(uid), chat_instance="c", message=msg,
                       data=data.pack())
    await dp.feed_update(bot, Update(update_id=next(ids), callback_query=cb))


async def test_full_purchase_flow(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, CUSTOMER, "/start")
    assert "خوش آمدید" in session.texts()[-1]

    await db.credit(CUSTOMER, 10_000_000, "topup")
    await send(dp, bot, CUSTOMER, "🛍 فروشگاه")
    await click(dp, bot, CUSTOMER, StarsQty(qty=500))
    assert "یوزرنیم" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, "bad")
    assert "معتبر نیست" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, "@friend_one")
    assert "پیش‌فاکتور" in session.texts()[-1]
    await click(dp, bot, CUSTOMER, Act(name="confirm"))
    assert "ثبت شد" in session.texts()[-1]
    assert len(fake.orders) == 1
    o = (await db.recent_orders())[0]
    assert o["recipient"] == "@friend_one" and o["stard_ref"] == "ord_test_1"


async def test_custom_stars_and_persian_digits(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, CUSTOMER, "/start")
    await click(dp, bot, CUSTOMER, StarsQty(qty=0))
    await send(dp, bot, CUSTOMER, "۷۵۰")
    assert "750" in session.texts()[-1] or "۷۵۰" in session.texts()[-1]


async def test_topup_flow_and_admin_approval(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, CUSTOMER, "/start")
    await send(dp, bot, CUSTOMER, "💳 شارژ کیف پول")
    assert "فعال نشده" in session.texts()[-1]
    await db.set_setting("card_number", "6037-9911-1111-1111")
    await send(dp, bot, CUSTOMER, "💳 شارژ کیف پول")
    await send(dp, bot, CUSTOMER, "50000")
    assert "6037" in session.texts()[-1]
    from aiogram.types import PhotoSize
    await send(dp, bot, CUSTOMER, photo=[PhotoSize(file_id="F", file_unique_id="U", width=1, height=1)])
    assert any(type(m).__name__ == "SendPhoto" and m.chat_id == ADMIN for m in session.sent)
    tid = (await db.pending_topups())[0]["id"]
    await click(dp, bot, CUSTOMER, Adm(name="tp_ok", arg=str(tid)))  # کاربر عادی نباید بتواند
    assert (await db.get_user(CUSTOMER)).balance == 0
    await send(dp, bot, ADMIN, "/start")
    await click(dp, bot, ADMIN, Adm(name="tp_ok", arg=str(tid)))
    assert (await db.get_user(CUSTOMER)).balance == 50_000


async def test_admin_sets_profit_and_bans(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await send(dp, bot, CUSTOMER, "/start")
    await send(dp, bot, CUSTOMER, "/admin")
    assert "پنل مدیریت" not in session.texts()[-1]
    await send(dp, bot, ADMIN, "⚙️ پنل مدیریت")
    assert "پنل مدیریت" in session.texts()[-1]
    await click(dp, bot, ADMIN, Adm(name="pset", arg="stars"))
    await send(dp, bot, ADMIN, "۲۵")
    assert await db.get_setting("profit:stars") == "25.0"
    await click(dp, bot, ADMIN, Adm(name="ban", arg=str(CUSTOMER)))
    await send(dp, bot, CUSTOMER, "🛍 فروشگاه")
    assert "مسدود" in session.texts()[-1]


async def test_shop_closed(env):
    dp, bot, session, db, fake = env
    await db.set_setting("shop_open", "0")
    await send(dp, bot, CUSTOMER, "🛍 فروشگاه")
    assert "بسته" in session.texts()[-1]


async def test_double_confirm_creates_one_order(env):
    import asyncio
    dp, bot, session, db, fake = env
    await send(dp, bot, CUSTOMER, "/start")
    await db.credit(CUSTOMER, 10_000_000, "topup")
    await click(dp, bot, CUSTOMER, StarsQty(qty=100))
    await send(dp, bot, CUSTOMER, "@friend_one")
    await asyncio.gather(*(click(dp, bot, CUSTOMER, Act(name="confirm")) for _ in range(5)))
    assert len(fake.orders) == 1
    assert len(await db.recent_orders()) == 1
