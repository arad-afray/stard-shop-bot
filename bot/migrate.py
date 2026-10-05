"""اجرای مهاجرت از خط فرمان (برای به‌روزرسانی و Docker):  python -m bot.migrate [--check | --to REV]

--check:    فقط وضعیت را چاپ می‌کند (کد خروج 1 اگر مهاجرت در انتظار باشد).
--to REV:   upgrade یا downgrade تا revision مشخص (برای Rollback بعد از به‌روزرسانی ناموفق).
"""
from __future__ import annotations

import asyncio
import sys

from .config import get_settings
from .db import Database
from .migrations_runner import current_revision, head_revision, pending_revisions, upgrade


async def main(check: bool, to: str | None = None) -> int:
    s = get_settings()
    db = Database(s.database_path, url=s.database_url.get_secret_value() if s.database_url else None)
    await db.connect(create=False)
    try:
        pending = await pending_revisions(db)
        print(f"current={await current_revision(db)} head={head_revision()} pending={pending}")
        if check:
            return 1 if pending else 0
        if to:
            from alembic import command
            from .migrations_runner import alembic_config, all_revisions
            revs = [r for r, _ in all_revisions()]
            cur = await current_revision(db)
            if to not in revs:
                print(f"unknown revision {to}")
                return 2
            cfg = alembic_config()
            down = cur in revs and revs.index(to) < revs.index(cur)

            def run(sc):
                cfg.attributes["connection"] = sc
                (command.downgrade if down else command.upgrade)(cfg, to)
            async with db.engine.connect() as c:
                await c.run_sync(run)
                await c.commit()
        else:
            await upgrade(db)
        print(f"now at {await current_revision(db)}")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    to = sys.argv[sys.argv.index("--to") + 1] if "--to" in sys.argv and sys.argv.index("--to") + 1 < len(sys.argv) else None
    sys.exit(asyncio.run(main("--check" in sys.argv, to)))
