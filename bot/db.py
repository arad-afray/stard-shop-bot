"""لایه‌ی پایگاه داده (SQLite با aiosqlite).

همه‌ی تغییرات موجودی اتمیک‌اند و در جدول ledger ثبت می‌شوند تا هر تومان قابل ردیابی باشد.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,           -- آیدی عددی تلگرام
    username    TEXT,
    first_name  TEXT,
    balance     INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    banned      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    type          TEXT NOT NULL,               -- stars | product
    category      TEXT NOT NULL,               -- stars | premium | star_gift
    product_id    INTEGER,
    title         TEXT NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 1,
    recipient     TEXT,
    gift_message  TEXT,
    quote_id      TEXT,                        -- پیش‌قیمت قفل‌شده‌ی Stard
    base_amount   INTEGER NOT NULL,            -- قیمت خرید از Stard
    price         INTEGER NOT NULL,            -- قیمت فروش به کاربر
    status        TEXT NOT NULL,               -- new | pending | processing | completed | cancelled | refunded | failed
    stard_ref     TEXT,
    failure_reason TEXT,
    refunded      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS topups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    amount     INTEGER NOT NULL,
    photo_id   TEXT,
    status     TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    admin_id   INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,               -- مثبت = واریز، منفی = برداشت
    kind       TEXT NOT NULL,                  -- topup | order | refund | admin
    ref        TEXT,
    created_at TEXT NOT NULL
);
"""

ACTIVE_STATUSES = ("new", "pending", "processing")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class User:
    id: int
    username: str | None
    first_name: str | None
    balance: int
    banned: bool
    created_at: str


class InsufficientBalance(Exception):
    pass


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        # یک اتصال مشترک داریم؛ قفل نمی‌گذارد تراکنش‌های هم‌زمان در هم بروند
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---------- کمکی ----------
    @asynccontextmanager
    async def _tx(self):
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    async def _write(self, sql: str, args: tuple = ()) -> aiosqlite.Cursor:
        async with self._lock:
            return await self.conn.execute(sql, args)

    async def _one(self, sql: str, args: tuple = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, args: tuple = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, args) as cur:
            return list(await cur.fetchall())

    # ---------- تنظیمات ----------
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = await self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self._write(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    async def del_setting(self, key: str) -> None:
        await self._write("DELETE FROM settings WHERE key=?", (key,))

    # ---------- کاربران ----------
    async def upsert_user(self, uid: int, username: str | None, first_name: str | None) -> User:
        await self._write(
            """INSERT INTO users(id, username, first_name, created_at) VALUES(?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
            (uid, username, first_name, now()))
        return await self.get_user(uid)

    async def get_user(self, uid: int) -> User | None:
        row = await self._one("SELECT * FROM users WHERE id=?", (uid,))
        return _user(row) if row else None

    async def find_user(self, query: str) -> User | None:
        q = query.strip().lstrip("@")
        if q.isdigit():
            return await self.get_user(int(q))
        row = await self._one("SELECT * FROM users WHERE lower(username)=lower(?)", (q,))
        return _user(row) if row else None

    async def set_banned(self, uid: int, banned: bool) -> None:
        await self._write("UPDATE users SET banned=? WHERE id=?", (int(banned), uid))

    async def all_user_ids(self) -> list[int]:
        return [r["id"] for r in await self._all("SELECT id FROM users WHERE banned=0")]

    # ---------- موجودی (اتمیک) ----------
    async def credit(self, uid: int, amount: int, kind: str, ref: str | None = None) -> int:
        if amount <= 0:
            raise ValueError("amount must be positive")
        async with self._tx():
            await self.conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))
            await self._ledger(uid, amount, kind, ref)
        return (await self.get_user(uid)).balance

    async def debit(self, uid: int, amount: int, kind: str, ref: str | None = None) -> int:
        if amount <= 0:
            raise ValueError("amount must be positive")
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?", (amount, uid, amount))
            if cur.rowcount != 1:
                raise InsufficientBalance()
            await self._ledger(uid, -amount, kind, ref)
        return (await self.get_user(uid)).balance

    async def _ledger(self, uid: int, amount: int, kind: str, ref: str | None) -> None:
        await self.conn.execute("INSERT INTO ledger(user_id, amount, kind, ref, created_at) VALUES(?,?,?,?,?)",
                                (uid, amount, kind, ref, now()))

    # ---------- سفارش ----------
    async def create_order_and_debit(self, *, user_id: int, type_: str, category: str, product_id: int | None,
                                     title: str, quantity: int, recipient: str | None, gift_message: str | None,
                                     quote_id: str | None, base_amount: int, price: int) -> int:
        """کسر موجودی و ساخت سفارش در یک تراکنش؛ یا هر دو انجام می‌شوند یا هیچ‌کدام."""
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?", (price, user_id, price))
            if cur.rowcount != 1:
                raise InsufficientBalance()
            t = now()
            cur = await self.conn.execute(
                """INSERT INTO orders(user_id, type, category, product_id, title, quantity, recipient, gift_message,
                                      quote_id, base_amount, price, status, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?, 'new', ?, ?)""",
                (user_id, type_, category, product_id, title, quantity, recipient, gift_message,
                 quote_id, base_amount, price, t, t))
            oid = cur.lastrowid
            await self._ledger(user_id, -price, "order", str(oid))
        return oid

    async def get_order(self, oid: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM orders WHERE id=?", (oid,))

    async def update_order(self, oid: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        await self._write(f"UPDATE orders SET {cols} WHERE id=?", (*fields.values(), oid))

    async def refund_order(self, oid: int, status: str, reason: str | None = None) -> bool:
        """برگشت پول سفارش به کاربر، فقط یک بار. True اگر همین فراخوانی پول را برگرداند."""
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE orders SET refunded=1, status=?, failure_reason=COALESCE(?, failure_reason), updated_at=? "
                "WHERE id=? AND refunded=0", (status, reason, now(), oid))
            if cur.rowcount != 1:
                return False
            row = await self._one("SELECT user_id, price FROM orders WHERE id=?", (oid,))
            await self.conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (row["price"], row["user_id"]))
            await self._ledger(row["user_id"], row["price"], "refund", str(oid))
        return True

    async def user_orders(self, uid: int, limit: int = 10, offset: int = 0) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                               (uid, limit, offset))

    async def count_user_orders(self, uid: int) -> int:
        return (await self._one("SELECT COUNT(*) c FROM orders WHERE user_id=?", (uid,)))["c"]

    async def active_orders(self) -> list[aiosqlite.Row]:
        q = ",".join("?" * len(ACTIVE_STATUSES))
        return await self._all(f"SELECT * FROM orders WHERE status IN ({q}) ORDER BY id", ACTIVE_STATUSES)

    async def recent_orders(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    # ---------- شارژ ----------
    async def create_topup(self, uid: int, amount: int, photo_id: str) -> int:
        cur = await self._write(
            "INSERT INTO topups(user_id, amount, photo_id, created_at) VALUES(?,?,?,?)", (uid, amount, photo_id, now()))
        return cur.lastrowid

    async def get_topup(self, tid: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM topups WHERE id=?", (tid,))

    async def pending_topups(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM topups WHERE status='pending' ORDER BY id")

    async def resolve_topup(self, tid: int, admin_id: int, approve: bool) -> aiosqlite.Row | None:
        """تأیید یا رد شارژ، فقط یک بار (جلوگیری از دوبار زدن دکمه توسط دو مدیر)."""
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE topups SET status=?, admin_id=? WHERE id=? AND status='pending'",
                ("approved" if approve else "rejected", admin_id, tid))
            if cur.rowcount != 1:
                return None
            row = await self._one("SELECT * FROM topups WHERE id=?", (tid,))
            if approve:
                await self.conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                                        (row["amount"], row["user_id"]))
                await self._ledger(row["user_id"], row["amount"], "topup", str(tid))
        return row

    # ---------- آمار ----------
    async def stats(self) -> dict[str, int]:
        r = await self._one(
            """SELECT
                 (SELECT COUNT(*) FROM users) users,
                 (SELECT COALESCE(SUM(balance),0) FROM users) balances,
                 (SELECT COUNT(*) FROM orders WHERE status='completed') done,
                 (SELECT COUNT(*) FROM orders WHERE status IN ('new','pending','processing')) active,
                 (SELECT COUNT(*) FROM orders WHERE refunded=1) refunded,
                 (SELECT COALESCE(SUM(price),0) FROM orders WHERE status='completed') sales,
                 (SELECT COALESCE(SUM(price-base_amount),0) FROM orders WHERE status='completed') profit,
                 (SELECT COUNT(*) FROM topups WHERE status='pending') pending_topups""")
        return dict(r)


def _user(row: aiosqlite.Row) -> User:
    return User(id=row["id"], username=row["username"], first_name=row["first_name"],
                balance=row["balance"], banned=bool(row["banned"]), created_at=row["created_at"])
