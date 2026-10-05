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
from bot.app import setup
from bot.shop import Shop
from bot.stard_api import StardClient
from bot.ui import JOIN_CHECK, Act, Adm, BoostDur, BoostQty, Nav, ReactQty, StarsQty
from tests.fake_stard import FakeStard

ADMIN, CUSTOMER = 100, 200
ids = itertools.count(1)


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[TelegramMethod] = []
        self.members: dict[int, str] = {}  # وضعیت عضویت کاربران در کانال اجباری

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return TgUser(id=42, is_bot=True, first_name="Shop", username="shop_bot")
        if name == "GetChatMember":
            from aiogram.types import ChatMemberLeft, ChatMemberMember
            if self.members.get(method.user_id, "left") == "member":
                return ChatMemberMember(user=tg_user(method.user_id))
            return ChatMemberLeft(user=tg_user(method.user_id))
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
        return [getattr(m, "text", None) or getattr(m, "caption", None) or "" for m in self.sent
                if type(m).__name__ != "AnswerCallbackQuery"]

    def alerts(self) -> list[str]:
        return [m.text or "" for m in self.sent if type(m).__name__ == "AnswerCallbackQuery"]


@pytest.fixture(autouse=True)
def no_unhandled_errors(caplog):
    """هر خطای مدیریت‌نشده در هندلرها تست را رد می‌کند (حتی اگر error handler آن را بگیرد)."""
    yield
    errors = [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL") and r.name.startswith(("bot", "aiogram"))]
    assert not errors, [r.getMessage() for r in errors]


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
    await setup(dp, db=db, shop=shop, settings=settings)
    yield dp, bot, session, db, fake
    await api.close()
    await db.close()


def tg_user(uid):
    return TgUser(id=uid, is_bot=False, first_name=f"U{uid}", username=f"user{uid}")


async def send(dp, bot, uid, text=None, photo=None, chat=None):
    msg = Message(message_id=next(ids), date=dt.datetime.now(), chat=chat or Chat(id=uid, type="private"),
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


async def click_raw(dp, bot, uid, data: str):
    msg = Message(message_id=next(ids), date=dt.datetime.now(), chat=Chat(id=uid, type="private"), text="x",
                  from_user=tg_user(42))
    cb = CallbackQuery(id=str(next(ids)), from_user=tg_user(uid), chat_instance="c", message=msg, data=data)
    await dp.feed_update(bot, Update(update_id=next(ids), callback_query=cb))


async def test_boost_purchase_flow(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, CUSTOMER, "/start")
    await db.credit(CUSTOMER, 10_000_000, "topup")
    await click(dp, bot, CUSTOMER, Nav(to="boost"))
    await click(dp, bot, CUSTOMER, BoostDur(days=7))
    assert "تعداد بوست" in session.texts()[-1]
    await click(dp, bot, CUSTOMER, BoostQty(days=7, qty=10))
    assert "کانال" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, "https://t.me/my_channel")
    assert "پیش‌فاکتور" in session.texts()[-1]
    await click(dp, bot, CUSTOMER, Act(name="confirm"))
    assert "ثبت شد" in session.texts()[-1]
    o = (await db.recent_orders())[0]
    assert o["type"] == "boost" and o["duration"] == 7 and o["recipient"] == "@my_channel"
    assert o["base_amount"] == 10 * 13200 and o["stard_ref"] == "ord_test_1"
    assert ("POST", "/boosts/orders") in fake.calls


async def test_reaction_manual_order_and_referral(env):
    dp, bot, session, db, fake = env
    await db.set_setting("referral_percent", 5)
    await send(dp, bot, ADMIN, "/start")
    await send(dp, bot, 300, "/start")                       # معرف
    await send(dp, bot, CUSTOMER, "/start ref_300")          # کاربر از لینک دعوت
    assert (await db.get_user(CUSTOMER)).referrer_id == 300
    await db.credit(CUSTOMER, 10_000_000, "topup")
    await click(dp, bot, CUSTOMER, ReactQty(qty=100))
    await send(dp, bot, CUSTOMER, "not a link")
    assert "معتبر نیست" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, "https://t.me/mychannel/123")
    await click(dp, bot, CUSTOMER, Act(name="confirm"))
    o = (await db.recent_orders())[0]
    assert o["status"] == "manual" and o["recipient"] == "https://t.me/mychannel/123"
    assert fake.orders == {}  # ریکشن به Stard فرستاده نمی‌شود
    # مدیر خبردار شد، با دکمه
    assert any(type(m).__name__ == "SendMessage" and m.chat_id == ADMIN and "سفارش دستی" in m.text
               for m in session.sent)
    await click(dp, bot, CUSTOMER, Adm(name="o_done", arg=str(o["id"])))  # کاربر عادی نمی‌تواند
    assert (await db.get_order(o["id"]))["status"] == "manual"
    await click(dp, bot, ADMIN, Adm(name="o_done", arg=str(o["id"])))
    assert (await db.get_order(o["id"]))["status"] == "completed"
    reward = min(o["price"] * 5 // 100, o["price"] - o["base_amount"])
    assert (await db.get_user(300)).balance == reward > 0
    await click(dp, bot, ADMIN, Adm(name="o_done", arg=str(o["id"])))  # دوباره: بی‌اثر
    assert (await db.get_user(300)).balance == reward


async def test_admin_refunds_manual_order(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await send(dp, bot, CUSTOMER, "/start")
    await db.credit(CUSTOMER, 1_000_000, "topup")
    await click(dp, bot, CUSTOMER, ReactQty(qty=10))
    await send(dp, bot, CUSTOMER, "t.me/c/1234567/89")
    await click(dp, bot, CUSTOMER, Act(name="confirm"))
    o = (await db.recent_orders())[0]
    assert (await db.get_user(CUSTOMER)).balance == 1_000_000 - o["price"]
    await click(dp, bot, ADMIN, Adm(name="o_refund", arg=str(o["id"])))
    assert (await db.get_user(CUSTOMER)).balance == 1_000_000
    await click(dp, bot, ADMIN, Adm(name="o_done", arg=str(o["id"])))  # بعد از برگشت پول، انجام نمی‌شود
    assert (await db.get_order(o["id"]))["status"] == "cancelled"


async def test_coupon_flow(env):
    dp, bot, session, db, fake = env
    await db.create_coupon("OFF20", 20, 1)
    await send(dp, bot, CUSTOMER, "/start")
    await db.credit(CUSTOMER, 10_000_000, "topup")
    await click(dp, bot, CUSTOMER, StarsQty(qty=500))
    await send(dp, bot, CUSTOMER, "@friend_one")
    await click(dp, bot, CUSTOMER, Act(name="coupon"))
    await send(dp, bot, CUSTOMER, "WRONG")
    assert "معتبر نیست" in " ".join(session.texts()[-2:])
    await click(dp, bot, CUSTOMER, Act(name="coupon"))
    await send(dp, bot, CUSTOMER, "off20")
    assert "اعمال شد" in session.texts()[-1]
    await click(dp, bot, CUSTOMER, Act(name="confirm"))
    o = (await db.recent_orders())[0]
    assert o["coupon"] == "OFF20" and o["discount"] > 0 and o["price"] >= o["base_amount"]
    assert (await db.get_coupon("OFF20"))["used"] == 1
    # ظرفیت پر شد
    await click(dp, bot, CUSTOMER, StarsQty(qty=50))
    await send(dp, bot, CUSTOMER, "@friend_one")
    await click(dp, bot, CUSTOMER, Act(name="coupon"))
    await send(dp, bot, CUSTOMER, "OFF20")
    assert "ظرفیت" in " ".join(session.texts()[-2:])


async def test_force_join(env):
    dp, bot, session, db, fake = env
    await db.set_json("force_join", [{"chat": -1001, "title": "Main", "url": "https://t.me/main"}])
    await db.set_setting("force_join_on", "1")
    await send(dp, bot, CUSTOMER, "/start")
    assert "عضو شوید" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, "🛍 فروشگاه")
    assert "عضو شوید" in session.texts()[-1]
    await send(dp, bot, ADMIN, "/start")                    # مدیر محدود نمی‌شود
    assert "خوش آمدید" in session.texts()[-1]
    session.members[CUSTOMER] = "member"
    await click_raw(dp, bot, CUSTOMER, JOIN_CHECK)
    assert "تأیید شد" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, "🛍 فروشگاه")
    assert "فروشگاه" in session.texts()[-1] and "عضو" not in session.texts()[-1]


async def test_group_price_reply(env):
    dp, bot, session, db, fake = env
    group = Chat(id=-500, type="supergroup", title="G")
    await send(dp, bot, CUSTOMER, "قیمت دلار", chat=group)
    assert "دلار" in session.texts()[-1] and "256,300" in session.texts()[-1]
    sent_before = len(session.sent)
    await send(dp, bot, CUSTOMER, "سلام بچه‌ها خوبین؟", chat=group)   # بی‌ربط: جواب نمی‌دهد
    await send(dp, bot, CUSTOMER, "🛍 فروشگاه", chat=group)            # منوی خصوصی در گروه کار نمی‌کند
    assert len(session.sent) == sent_before
    await db.set_setting("group_prices", "0")
    from bot.handlers import prices as p
    p._last.clear()
    await send(dp, bot, CUSTOMER, "قیمت تون", chat=group)
    assert len(session.sent) == sent_before


async def test_private_prices_and_referral_link(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, CUSTOMER, "/start")
    await send(dp, bot, CUSTOMER, "💹 قیمت لحظه‌ای")
    assert "TON" in session.texts()[-1] and "استارز" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, "🎁 دعوت دوستان")
    assert f"start=ref_{CUSTOMER}" in session.texts()[-1]


async def test_category_disabled(env):
    dp, bot, session, db, fake = env
    await db.set_setting("cat:stars", "0")
    await send(dp, bot, CUSTOMER, "/start")
    await click(dp, bot, CUSTOMER, Nav(to="stars"))
    assert not any("حداقل ۵۰" in t for t in session.texts())
    await click(dp, bot, CUSTOMER, StarsQty(qty=100))
    assert "غیرفعال" in session.texts()[-1]


async def test_owner_adds_admin(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await send(dp, bot, CUSTOMER, "/start")
    await click(dp, bot, ADMIN, Adm(name="adm_add"))
    await send(dp, bot, ADMIN, str(CUSTOMER))
    await send(dp, bot, CUSTOMER, "/admin")
    assert "پنل مدیریت" in session.texts()[-1]
    # مدیر غیرمالک نمی‌تواند مدیر اضافه کند
    await click(dp, bot, CUSTOMER, Adm(name="adm_add"))
    assert "فقط مالک" in session.alerts()[-1]


async def test_admin_panel_views_do_not_crash(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    for name in ("stats", "profit", "orders", "cats", "join", "coupons", "group", "wallet", "settings",
                 "admins", "sim", "home", "toggle", "toggle"):
        await click(dp, bot, ADMIN, Adm(name=name))
    assert not any("خطایی پیش آمد" in a for a in session.alerts())
    await click(dp, bot, ADMIN, Adm(name="backup"))
    assert any(type(m).__name__ == "SendDocument" for m in session.sent)
