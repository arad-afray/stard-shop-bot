"""بررسی سلامت کد و محیط بعد از به‌روزرسانی (در پردازه‌ی جدا، با کد تازه):  python -m bot.selfcheck

همه‌ی ماژول‌ها import می‌شوند، تنظیمات خوانده می‌شود، پایگاه داده وصل و revision آن با head مقایسه می‌شود.
کد خروج 0 یعنی سالم.
"""
from __future__ import annotations

import asyncio
import importlib
import pkgutil
import sys


async def main() -> int:
    import bot
    for m in pkgutil.walk_packages(bot.__path__, "bot."):
        if m.name.endswith(("__main__", ".selfcheck", ".migrate")) or ".migrations." in m.name:
            continue
        importlib.import_module(m.name)
    from .config import get_settings
    from .db import Database
    from .migrations_runner import current_revision, head_revision
    s = get_settings()
    db = Database(s.database_path, url=s.database_url.get_secret_value() if s.database_url else None)
    await db.connect(create=False)
    try:
        await db.ping()
        cur, head = await current_revision(db), head_revision()
        if cur != head:
            print(f"FAIL: database revision {cur} != code head {head}")
            return 2
    finally:
        await db.close()
    print(f"OK version={bot.__version__} revision={head}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
