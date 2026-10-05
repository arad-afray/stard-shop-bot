"""محیط Alembic. هم از داخل ربات (با اتصال آماده) و هم از خط فرمان (alembic upgrade head) کار می‌کند."""
from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from bot.models import metadata

config = context.config


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, render_as_batch=True,
                      compare_type=False)
    with context.begin_transaction():
        context.run_migrations()


async def _run_cli() -> None:
    from bot.config import get_settings
    from bot.db import build_url
    s = get_settings()
    engine = create_async_engine(build_url(s.database_url, s.database_path))
    async with engine.begin() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    asyncio.run(_run_cli())
