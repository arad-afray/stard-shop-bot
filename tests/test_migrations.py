"""مهاجرت‌ها: شِمای حاصل از Alembic باید دقیقاً با bot/models.py یکی باشد، و downgrade/upgrade کار کند."""
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from bot.migrations_runner import alembic_config, current_revision, head_revision, pending_revisions
from bot.models import metadata
from tests.conftest import new_db


def _diff(sc):
    mc = MigrationContext.configure(sc, opts={"compare_type": True})
    diffs = compare_metadata(mc, metadata)
    # SQLite نوع ستون‌های قدیمی را دقیق گزارش نمی‌کند؛ فقط ساختار (جدول/ستون/ایندکس) مقایسه می‌شود
    return [d for d in diffs if not (isinstance(d, list) and d and d[0][0] == "modify_type")]


async def test_models_match_migrations():
    db = await new_db()
    try:
        assert await current_revision(db) == head_revision()
        assert await pending_revisions(db) == []
        async with db.engine.connect() as c:
            diffs = await c.run_sync(_diff)
        assert diffs == [], diffs
    finally:
        await db.close()


async def test_downgrade_and_upgrade_roundtrip():
    db = await new_db()
    try:
        cfg = alembic_config()

        def run(sc, target, up):
            cfg.attributes["connection"] = sc
            (command.upgrade if up else command.downgrade)(cfg, target)
        async with db.engine.connect() as c:
            await c.run_sync(run, "0001", False)
            await c.commit()
        assert await current_revision(db) == "0001"
        async with db.engine.connect() as c:
            await c.run_sync(run, "head", True)
            await c.commit()
        assert await current_revision(db) == head_revision()
    finally:
        await db.close()
