"""تنظیمات مشترک تست‌ها.

پیش‌فرض SQLite است. برای اجرای همه‌ی تست‌ها روی PostgreSQL:
    TEST_DATABASE_URL=postgresql+asyncpg://postgres@localhost/shop_test pytest -q
(پایگاه داده‌ی تست قبل از هر تست کامل پاک می‌شود — هرگز آدرس پایگاه داده‌ی واقعی را ندهید.)
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bot.db import Database

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
