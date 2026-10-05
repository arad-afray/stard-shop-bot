"""تنظیمات مشترک تست‌ها.

پیش‌فرض SQLite است. برای اجرای همه‌ی تست‌ها روی PostgreSQL:
    TEST_DATABASE_URL=postgresql+asyncpg://postgres@localhost/shop_test pytest -q
(پایگاه داده‌ی تست قبل از هر تست کامل پاک می‌شود — هرگز آدرس پایگاه داده‌ی واقعی را ندهید.)
"""
from __future__ import annotations

import os

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bot.app import setup
from bot.config import Settings
from bot.db import Database
from bot.locks import DistributedLock, RateLimiter
from bot.queue import JobQueue
from bot.shop import Shop
from bot.stard_api import StardClient
from tests.fake_stard import FakeStard
from tests.helpers import ADMIN, RecordingSession, make_bot

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


async def new_db() -> Database:
    if TEST_DATABASE_URL:
        eng = create_async_engine(TEST_DATABASE_URL)
        async with eng.begin() as c:
            await c.execute(text("DROP SCHEMA public CASCADE"))
            await c.execute(text("CREATE SCHEMA public"))
        await eng.dispose()
        d = Database(url=TEST_DATABASE_URL)
    else:
        d = Database(":memory:")
    await d.connect()
    return d


requires_sqlite = pytest.mark.skipif(bool(TEST_DATABASE_URL), reason="SQLite-only test")


@pytest.fixture
def no_unhandled_errors(caplog):
    """هر خطای مدیریت‌نشده در هندلرها تست را رد می‌کند (حتی اگر error handler آن را بگیرد)."""
    yield
    # در teardown، caplog.records فقط رکوردهای همین مرحله است؛ رکوردهای اجرای خود تست در get_records("call")اند
    errors = [r for r in caplog.get_records("call")
              if r.levelname in ("ERROR", "CRITICAL") and r.name.startswith(("bot", "aiogram"))]
    assert not errors, [r.getMessage() for r in errors]


@pytest.fixture
async def env():
    db = await new_db()
    fake = FakeStard()
    api = StardClient("sk_test_ok", transport=fake.transport())
    queue, locks = JobQueue(db, "test"), DistributedLock(db, "test")
    shop = Shop(db, api, default_profit=10, queue=queue, locks=locks)
    settings = Settings(bot_token="1:x", stard_api_key="sk_test_ok", admin_ids=[ADMIN])
    bot, session = make_bot(RecordingSession())
    dp = Dispatcher(storage=MemoryStorage())
    await setup(dp, db=db, shop=shop, settings=settings, queue=queue, locks=locks, limiter=RateLimiter(db))
    yield dp, bot, session, db, fake
    await api.close()
    await db.close()
