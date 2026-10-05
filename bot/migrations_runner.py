"""اجرای مهاجرت‌های Alembic از داخل ربات، به‌علاوه‌ی ارتقای پایگاه داده‌های SQLite نسخه‌ی ۱ و ۲ که Alembic نداشتند."""
from __future__ import annotations

import logging
import os

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ستون‌هایی که نسخه‌ی ۲ به شِمای نسخه‌ی ۱ اضافه کرد (قبل از Alembic)
_V2_COLUMNS = {
    "users": {"referrer_id": "INTEGER"},
    "orders": {"duration": "INTEGER", "discount": "INTEGER NOT NULL DEFAULT 0", "coupon": "TEXT",
               "ref_paid": "INTEGER NOT NULL DEFAULT 0"},
}
_V2_TABLES = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topups (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL, photo_id TEXT, status TEXT NOT NULL DEFAULT 'pending', admin_id INTEGER,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coupons (code TEXT PRIMARY KEY, percent REAL NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coupon_uses (code TEXT NOT NULL, user_id INTEGER NOT NULL, order_id INTEGER NOT NULL,
    PRIMARY KEY (code, user_id));
CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT, created_at TEXT NOT NULL);
"""


def alembic_config() -> Config:
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "bot", "migrations"))
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def all_revisions() -> list[tuple[str, str]]:
    """(revision, توضیح) از قدیم به جدید."""
    script = ScriptDirectory.from_config(alembic_config())
    revs = list(script.walk_revisions())
    return [(r.revision, (r.doc or "").splitlines()[0]) for r in reversed(revs)]


async def current_revision(db) -> str | None:
    async with db.engine.connect() as c:
        return await c.run_sync(lambda sc: MigrationContext.configure(sc).get_current_revision())


async def pending_revisions(db) -> list[str]:
    cur = await current_revision(db)
    revs = [r for r, _ in all_revisions()]
    if cur is None:
        return revs
    return revs[revs.index(cur) + 1:] if cur in revs else []


def _legacy_v2_upgrade(sc) -> None:
    """پایگاه داده‌ی SQLite نسخه‌ی ۱/۲ را به شِمای دقیق نسخه‌ی ۲ (= revision 0001) می‌رساند."""
    for stmt in filter(None, (s.strip() for s in _V2_TABLES.split(";"))):
        sc.exec_driver_sql(stmt)
    insp = inspect(sc)
    for table, cols in _V2_COLUMNS.items():
        have = {c["name"] for c in insp.get_columns(table)}
        for col, decl in cols.items():
            if col not in have:
                sc.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def upgrade(db, target: str = "head") -> None:
    cfg = alembic_config()

    def run(sc) -> None:
        tables = set(inspect(sc).get_table_names())
        if "alembic_version" not in tables and "users" in tables:
            log.info("legacy database detected — upgrading v1/v2 schema and stamping 0001")
            _legacy_v2_upgrade(sc)
            cfg.attributes["connection"] = sc
            command.stamp(cfg, "0001")
        cfg.attributes["connection"] = sc
        command.upgrade(cfg, target)

    if db.is_sqlite:
        async with db._write_lock:
            async with db.engine.connect() as c:
                await c.run_sync(run)
                await c.commit()
    else:
        async with db.engine.connect() as c:
            # فقط یک نمونه هم‌زمان مهاجرت را اجرا کند
            await c.execute(text("SELECT pg_advisory_lock(727274)"))
            try:
                await c.run_sync(run)
                await c.commit()
            finally:
                await c.execute(text("SELECT pg_advisory_unlock(727274)"))
                await c.commit()
