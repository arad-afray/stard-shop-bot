"""گزارش روزانه و ثبت نسخه در مستندات."""
import re
from datetime import datetime
from pathlib import Path

from bot import __version__, reports
from bot.worker import Context

import pytest

from bot.shop import Shop
from bot.stard_api import StardClient
from tests.conftest import new_db
from tests.fake_stard import FakeStard

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_written_everywhere():
    # نسخه‌ی فعلی همیشه در README (روی GitHub) و CHANGELOG نوشته شده باشد
    first = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0]
    assert __version__ in first, "عنوان README باید نسخه‌ی فعلی را داشته باشد"
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## {re.escape(__version__)}\b", changelog, re.M), "بخش نسخه در CHANGELOG نیست"


def test_day_bounds_tehran():
    a, b, day = reports.day_bounds(datetime(2026, 10, 8, 23, 30, tzinfo=reports.TEHRAN))
    assert day == "2026-10-08"
    assert a == "2026-10-07T20:30:00Z" and b == "2026-10-08T20:30:00Z"


@pytest.fixture
async def shop():
    db = await new_db()
    api = StardClient("sk_test_ok", "https://stard-market.ir/api/v1", transport=FakeStard().transport(), max_retries=2)
    yield Shop(db, api, default_profit=10)
    await api.close()
    await db.close()


class _Admins:
    def all(self):
        return [100]


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def test_daily_report_sent_once_per_day(shop, monkeypatch):
    db = shop.db
    await db.upsert_user(1, "a", "A")
    await db.credit(1, 10_000_000, "topup")
    await shop.place_order(1, await shop.stars_offer(100), "@bob")
    text = await reports.build(db)
    assert "گزارش روزانه" in text and "کاربر جدید" in text

    bot = _Bot()
    ctx = Context(bot=bot, shop=shop, db=db, queue=None, locks=None, admins=_Admins())
    late = datetime(2026, 10, 8, 23, 5, tzinfo=reports.TEHRAN)
    early = datetime(2026, 10, 8, 9, 0, tzinfo=reports.TEHRAN)

    class _DT(datetime):
        current = early

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(reports, "datetime", _DT)
    await reports.daily_report(ctx)
    assert bot.sent == []  # هنوز به ساعت ارسال نرسیده
    _DT.current = late
    await reports.daily_report(ctx)
    await reports.daily_report(ctx)  # بار دوم همان روز ارسال نمی‌شود
    assert len(bot.sent) == 1
    await db.set_json("daily_report", {"enabled": False})
    _DT.current = datetime(2026, 10, 9, 23, 5, tzinfo=reports.TEHRAN)
    await reports.daily_report(ctx)
    assert len(bot.sent) == 1  # خاموش
