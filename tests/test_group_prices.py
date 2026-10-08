"""جواب قیمت در گروه: تشخیص سؤال، محاسبه با تعداد، تنظیمات پنل، فهرست گروه‌ها و لینک خرید."""
from decimal import Decimal

import pytest
from aiogram.types import Chat

from bot import group_prices as gp
from bot.handlers import prices as p
from bot.ui import Adm, GPrice
from tests.helpers import ADMIN, CUSTOMER, click, send

GROUP = Chat(id=-500, type="supergroup", title="Stard Group")
OTHER = Chat(id=-600, type="supergroup", title="Other")


def to_chat(session, chat_id):
    return [m for m in session.sent if type(m).__name__ == "SendMessage" and m.chat_id == chat_id]


def last_markup(session):
    return str(next(m.reply_markup for m in reversed(session.sent) if getattr(m, "reply_markup", None)))


async def ask(dp, bot, text, chat=GROUP):
    p._last.pop((chat.id,), None)       # فاصله‌ی ضد سیل گروه در تست‌ها
    await send(dp, bot, CUSTOMER, text, chat=chat)


@pytest.fixture(autouse=True)
def _reset():
    p._last.clear()
    yield
    p._last.clear()


@pytest.mark.parametrize("text,kind,amount", [
    ("قیمت", "all", None), ("قیمت تون", "ton", None), ("تون", "ton", None), ("استارز؟", "stars", None),
    ("تون چنده؟", "ton", None), ("10 تون", "ton", "10"), ("۱۰ تون چنده", "ton", "10"),
    ("قیمت ۵۰۰ استارز", "stars", "500"), ("1k استارز", "stars", "1000"), ("۲٫۵ تون به تومان", "ton", "2.5"),
    ("100 دلار چند تومنه", "usd", "100"), ("ده هزار استارز", "stars", "10000"), ("یه تون چنده؟", "ton", "1"),
    ("10,000 استارز", "stars", "10000"), ("100تون", "ton", "100"), ("PRICE TON", "ton", None),
])
def test_parse_questions(text, kind, amount):
    q = gp.parse(text)
    assert q is not None and q.kind == kind
    assert q.amount == (Decimal(amount) if amount else None)


@pytest.mark.parametrize("text", [
    "سلام", "من 10 تون دارم", "10 تون و 5 دلار", "0 تون", "دلار امروز خیلی گرون شده ولی من نمیدونم چرا",
    "/start", "", None, "99999999 تون", "قیمت این گوشی رو کسی میدونه چنده دوستان",
])
def test_parse_ignores_conversation(text):
    assert gp.parse(text) is None


def test_parse_respects_config_and_custom_words():
    cfg = gp.DEFAULT_CFG | {"bare_word": False, "amounts": False, "words": {"ton": ["تنکوین"], "usd": [], "stars": []}}
    assert gp.parse("تون", cfg) is None
    assert gp.parse("10 تون", cfg) is None
    assert gp.parse("قیمت تون", cfg) == gp.Query("ton")
    assert gp.parse("قیمت تنکوین", cfg) == gp.Query("ton")


def test_amount_text_conversions():
    rates = {"usd": {"amount": 100_000}, "ton": {"amount": 300_000, "usd": 3.0}}
    text, buy = gp.amount_text(gp.Query("ton", Decimal("2.5")), rates, star_sell=1_000, stars_total=None)
    assert "750,000" in text and "$7.5" in text and "750 استارز" in text and buy is None
    text, buy = gp.amount_text(gp.Query("usd", Decimal(3)), rates, star_sell=1_000, stars_total=None)
    assert "300,000" in text and "1 TON" in text and "300 استارز" in text
    text, buy = gp.amount_text(gp.Query("stars", Decimal(500)), rates, star_sell=1_000, stars_total=520_000)
    assert "520,000" in text and buy == 500
    text, buy = gp.amount_text(gp.Query("stars", Decimal(10)), rates, star_sell=1_000, stars_total=10_000)
    assert "حداقل خرید" in text and buy is None


def test_chat_modes():
    cfg = dict(gp.DEFAULT_CFG, chats=[-500])
    assert gp.chat_allowed(cfg | {"mode": "all"}, -600)
    assert gp.chat_allowed(cfg | {"mode": "allow"}, -500) and not gp.chat_allowed(cfg | {"mode": "allow"}, -600)
    assert not gp.chat_allowed(cfg | {"mode": "deny"}, -500) and gp.chat_allowed(cfg | {"mode": "deny"}, -600)


async def test_group_amount_reply_with_buy_link(env):
    dp, bot, session, db, fake = env
    await ask(dp, bot, "10 تون چنده؟")
    assert "3,857,700" in session.texts()[-1]          # 10 × 385,770
    await ask(dp, bot, "۵۰۰ استارز")
    assert "500 استارز" in session.texts()[-1]
    assert "start=stars_500" in last_markup(session)
    await ask(dp, bot, "100 دلار")
    assert "25,630,000" in session.texts()[-1]
    # گروه ثبت شد تا در پنل قابل انتخاب باشد
    assert (await db.get_json("group_chats"))["-500"] == "Stard Group"
    # دکمه‌ی به‌روزرسانی همان سؤال با تعداد را دوباره حساب می‌کند
    await click(dp, bot, CUSTOMER, GPrice(what="ton", amt="10"))


async def test_cooldown_and_flood(env):
    dp, bot, session, db, fake = env
    await ask(dp, bot, "قیمت تون")
    n = len(to_chat(session, -500))
    await ask(dp, bot, "قیمت تون")       # تکراری در فاصله‌ی کوتاه
    assert len(to_chat(session, -500)) == n


async def test_admin_controls_each_kind(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await click(dp, bot, ADMIN, Adm(name="group"))
    assert "جواب قیمت در گروه" in session.texts()[-1]
    await click(dp, bot, ADMIN, Adm(name="group", arg="k|ton"))       # خاموش کردن جواب تون
    n = len(to_chat(session, -500))
    await ask(dp, bot, "قیمت تون")
    await ask(dp, bot, "5 تون")
    assert len(to_chat(session, -500)) == n
    await ask(dp, bot, "قیمت استارز")           # بقیه روشن‌اند
    assert len(to_chat(session, -500)) == n + 1
    await click(dp, bot, ADMIN, Adm(name="group", arg="amt"))          # خاموش کردن سؤال با تعداد
    n = len(to_chat(session, -500))
    await ask(dp, bot, "100 دلار")
    assert len(to_chat(session, -500)) == n
    await click(dp, bot, ADMIN, Adm(name="group", arg="t"))            # خاموش کردن کل قابلیت
    p._last.clear()
    await ask(dp, bot, "قیمت دلار")
    assert len(to_chat(session, -500)) == n
    for arg in ("cd", "del", "buy", "bare", "t"):
        await click(dp, bot, ADMIN, Adm(name="group", arg=arg))
    cfg = await gp.get_config(db)
    assert cfg["enabled"] and cfg["cooldown"] == 10 and cfg["delete_after"] == 1
    assert not cfg["buy_button"] and not cfg["bare_word"] and not cfg["kinds"]["ton"]


async def test_admin_selects_groups(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await db.set_json("group_chats", {"-500": "Stard Group", "-600": "Other"})
    await click(dp, bot, ADMIN, Adm(name="grp_chats", arg="mode"))      # all → allow
    await click(dp, bot, ADMIN, Adm(name="grp_chats", arg="-500"))      # فقط این گروه
    assert "Stard Group" in last_markup(session)
    n = len(to_chat(session, -500))
    await ask(dp, bot, "قیمت", chat=OTHER)
    assert to_chat(session, -600) == []
    await ask(dp, bot, "قیمت")
    assert len(to_chat(session, -500)) == n + 1


async def test_admin_custom_words_and_tester(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await click(dp, bot, ADMIN, Adm(name="grp_words"))
    await send(dp, bot, ADMIN, "تون: تنکوین، تون کوین\nاستارز: استار تلگرام")
    assert "ذخیره شد" in "\n".join(session.texts()[-2:])
    await ask(dp, bot, "قیمت تنکوین")
    assert "TON" in session.texts()[-1]
    await click(dp, bot, ADMIN, Adm(name="grp_test"))
    await send(dp, bot, ADMIN, "2 تون چنده؟")
    assert "تشخیص" in session.texts()[-1] and "771,540" in session.texts()[-1]
    await send(dp, bot, ADMIN, "سلام خوبی")
    assert "جواب نمی‌دهد" in session.texts()[-1]


async def test_deep_link_opens_stars_purchase(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, CUSTOMER, "/start stars_500")
    assert any("500 استارز" in t and "قیمت" in t for t in session.texts()[-2:])


async def test_inline_mode(env):
    from aiogram.types import InlineQuery, Update
    from tests.helpers import tg_user
    dp, bot, session, db, fake = env

    async def inline(text, n):
        iq = InlineQuery(id=str(n), from_user=tg_user(CUSTOMER), query=text, offset="")
        await dp.feed_update(bot, Update(update_id=90_000 + n, inline_query=iq))
        return next(m for m in reversed(session.sent) if type(m).__name__ == "AnswerInlineQuery")

    r = await inline("10 تون", 1)
    assert len(r.results) == 1 and "3,857,700" in r.results[0].input_message_content.message_text
    assert r.results[0].description and "<b>" not in r.results[0].description
    r = await inline("", 2)                                    # بدون متن: چند نمونه‌ی آماده
    assert len(r.results) == 4
    r = await inline("۵۰۰ استارز", 3)
    assert "start=stars_500" in str(r.results[0].reply_markup)
    await click(dp, bot, ADMIN, Adm(name="group", arg="inl"))  # خاموش از پنل
    r = await inline("10 تون", 4)
    assert r.results == []


def test_parse_with_greeting():
    assert gp.parse("سلام قیمت تون چنده؟") == gp.Query("ton")
    assert gp.parse("سلام داداش 10 تون چند") == gp.Query("ton", Decimal(10))
    assert gp.parse("سلام خوبی") is None
