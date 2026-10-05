"""لایه‌ی پایگاه داده (SQLite با aiosqlite).

همه‌ی تغییرات موجودی اتمیک‌اند و در جدول ledger ثبت می‌شوند تا هر تومان قابل ردیابی باشد.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,           -- آیدی عددی تلگرام
    username    TEXT,
    first_name  TEXT,
    balance     INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    banned      INTEGER NOT NULL DEFAULT 0,
    referrer_id INTEGER,                       -- معرف (زیرمجموعه‌گیری)
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    type          TEXT NOT NULL,               -- stars | product | boost | reaction
    category      TEXT NOT NULL,               -- stars | premium | star_gift | boost | reaction
    product_id    INTEGER,
    title         TEXT NOT NULL,
    quantity      INTEGER NOT NULL DEFAULT 1,
    duration      INTEGER,                     -- فقط بوست: مدت به روز
    recipient     TEXT,
    gift_message  TEXT,
    quote_id      TEXT,                        -- پیش‌قیمت قفل‌شده‌ی Stard
    base_amount   INTEGER NOT NULL,            -- قیمت خرید از Stard
    price         INTEGER NOT NULL,            -- مبلغ پرداختی کاربر (بعد از تخفیف)
    discount      INTEGER NOT NULL DEFAULT 0,
    coupon        TEXT,
    status        TEXT NOT NULL,               -- new | pending | processing | manual | completed | cancelled | refunded | failed
    stard_ref     TEXT,
    failure_reason TEXT,
    refunded      INTEGER NOT NULL DEFAULT 0,
    ref_paid      INTEGER NOT NULL DEFAULT 0,  -- پاداش معرف پرداخت شده؟
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
    kind       TEXT NOT NULL,                  -- topup | order | refund | admin | referral
    ref        TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id);
CREATE TABLE IF NOT EXISTS coupons (
    code       TEXT PRIMARY KEY,
    percent    REAL NOT NULL,
    max_uses   INTEGER NOT NULL DEFAULT 0,     -- 0 = نامحدود
    used       INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coupon_uses (
    code     TEXT NOT NULL,
    user_id  INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    PRIMARY KEY (code, user_id)
);
"""

# ستون‌هایی که در نسخه‌ی ۲ اضافه شدند؛ برای پایگاه داده‌ی قدیمی با ALTER اضافه می‌شوند
MIGRATIONS = {
    "users": {"referrer_id": "INTEGER"},
    "orders": {"duration": "INTEGER", "discount": "INTEGER NOT NULL DEFAULT 0", "coupon": "TEXT",
               "ref_paid": "INTEGER NOT NULL DEFAULT 0"},
}

ACTIVE_STATUSES = ("new", "pending", "processing")   # سفارش‌هایی که worker با Stard همگام می‌کند
OPEN_STATUSES = ACTIVE_STATUSES + ("manual",)         # همه‌ی سفارش‌های تمام‌نشده


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class User:
    id: int
    username: str | None
    first_name: str | None
    balance: int
    banned: bool
    created_at: str
    referrer_id: int | None = None


class InsufficientBalance(Exception):
    pass


class CouponInvalid(Exception):
    pass


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        # یک اتصال مشترک داریم؛ قفل نمی‌گذارد تراکنش‌های هم‌زمان در هم بروند
        self._lock = asyncio.Lock()
        self._settings: dict[str, str] | None = None  # کش تنظیمات در حافظه

    async def connect(self) -> None:
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self._migrate()

    async def _migrate(self) -> None:
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in await self._all(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

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

    async def backup(self, dest: str) -> None:
        """یک کپی سالم و فشرده از پایگاه داده (حتی وقتی ربات روشن است)."""
        if os.path.exists(dest):
            os.remove(dest)
        async with self._lock:
            await self.conn.execute("VACUUM INTO ?", (dest,))

    # ---------- تنظیمات (با کش در حافظه) ----------
    async def _load_settings(self) -> dict[str, str]:
        if self._settings is None:
            self._settings = {r["key"]: r["value"] for r in await self._all("SELECT key, value FROM settings")}
        return self._settings

    async def get_setting(self, key: str, default: Any = None) -> Any:
        return (await self._load_settings()).get(key, default)

    async def set_setting(self, key: str, value: Any) -> None:
        await self._write(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))
        (await self._load_settings())[key] = str(value)

    async def del_setting(self, key: str) -> None:
        await self._write("DELETE FROM settings WHERE key=?", (key,))
        (await self._load_settings()).pop(key, None)

    async def get_json(self, key: str, default: Any = None) -> Any:
        raw = await self.get_setting(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    async def set_json(self, key: str, value: Any) -> None:
        await self.set_setting(key, json.dumps(value, ensure_ascii=False))

    # ---------- کاربران ----------
    async def upsert_user(self, uid: int, username: str | None, first_name: str | None,
                          referrer_id: int | None = None) -> tuple[User, bool]:
        """کاربر را ثبت یا به‌روز می‌کند. (کاربر، تازه‌ساخته‌شده؟) را برمی‌گرداند.

        معرف فقط موقع ساخت کاربر ثبت می‌شود و بعداً عوض نمی‌شود.
        """
        u = await self.get_user(uid)
        if u is not None:
            if u.username != username or u.first_name != first_name:
                await self._write("UPDATE users SET username=?, first_name=? WHERE id=?", (username, first_name, uid))
                u.username, u.first_name = username, first_name
            return u, False
        if referrer_id == uid or (referrer_id is not None and await self.get_user(referrer_id) is None):
            referrer_id = None
        cur = await self._write(
            "INSERT OR IGNORE INTO users(id, username, first_name, referrer_id, created_at) VALUES(?, ?, ?, ?, ?)",
            (uid, username, first_name, referrer_id, now()))
        return await self.get_user(uid), cur.rowcount == 1

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

    async def count_referrals(self, uid: int) -> int:
        return (await self._one("SELECT COUNT(*) c FROM users WHERE referrer_id=?", (uid,)))["c"]

    async def referral_earnings(self, uid: int) -> int:
        r = await self._one("SELECT COALESCE(SUM(amount),0) s FROM ledger WHERE user_id=? AND kind='referral'", (uid,))
        return r["s"]

    async def top_buyers(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._all(
            """SELECT u.id, u.username, u.first_name, COUNT(o.id) n, SUM(o.price) total
               FROM orders o JOIN users u ON u.id=o.user_id WHERE o.status='completed'
               GROUP BY u.id ORDER BY total DESC LIMIT ?""", (limit,))

    # ---------- موجودی (اتمیک) ----------
    async def credit(self, uid: int, amount: int, kind: str, ref: str | None = None) -> int:
        if amount <= 0:
            raise ValueError("amount must be positive")
        async with self._tx():
            cur = await self.conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))
            if cur.rowcount != 1:
                raise ValueError("user not found")
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

    async def user_ledger(self, uid: int, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit))

    # ---------- سفارش ----------
    async def create_order_and_debit(self, *, user_id: int, type_: str, category: str, product_id: int | None,
                                     title: str, quantity: int, recipient: str | None, gift_message: str | None,
                                     quote_id: str | None, base_amount: int, price: int,
                                     duration: int | None = None, status: str = "new",
                                     coupon: str | None = None, discount: int = 0) -> int:
        """کسر موجودی، ثبت مصرف کد تخفیف و ساخت سفارش در یک تراکنش؛ یا همه انجام می‌شوند یا هیچ‌کدام."""
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?", (price, user_id, price))
            if cur.rowcount != 1:
                raise InsufficientBalance()
            t = now()
            cur = await self.conn.execute(
                """INSERT INTO orders(user_id, type, category, product_id, title, quantity, duration, recipient,
                                      gift_message, quote_id, base_amount, price, discount, coupon, status,
                                      created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, type_, category, product_id, title, quantity, duration, recipient, gift_message,
                 quote_id, base_amount, price, discount, coupon, status, t, t))
            oid = cur.lastrowid
            if coupon:
                cur = await self.conn.execute(
                    "UPDATE coupons SET used = used + 1 WHERE code=? AND active=1 AND (max_uses=0 OR used < max_uses)",
                    (coupon,))
                if cur.rowcount != 1:
                    raise CouponInvalid()
                try:
                    await self.conn.execute("INSERT INTO coupon_uses(code, user_id, order_id) VALUES(?,?,?)",
                                            (coupon, user_id, oid))
                except aiosqlite.IntegrityError as e:
                    raise CouponInvalid() from e
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

    async def set_order_status(self, oid: int, new: str, *, only_from: tuple[str, ...]) -> bool:
        """تغییر وضعیت فقط اگر وضعیت فعلی یکی از only_from باشد (جلوگیری از رقابت دو مدیر/worker)."""
        q = ",".join("?" * len(only_from))
        cur = await self._write(f"UPDATE orders SET status=?, updated_at=? WHERE id=? AND status IN ({q}) AND refunded=0",
                                (new, now(), oid, *only_from))
        return cur.rowcount == 1

    async def refund_order(self, oid: int, status: str, reason: str | None = None) -> bool:
        """برگشت پول سفارش به کاربر، فقط یک بار. True اگر همین فراخوانی پول را برگرداند.

        سفارش انجام‌شده برگشت نمی‌خورد. کد تخفیف مصرف‌شده هم آزاد می‌شود.
        """
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE orders SET refunded=1, status=?, failure_reason=COALESCE(?, failure_reason), updated_at=? "
                "WHERE id=? AND refunded=0 AND status != 'completed'", (status, reason, now(), oid))
            if cur.rowcount != 1:
                return False
            row = await self._one("SELECT user_id, price, coupon FROM orders WHERE id=?", (oid,))
            await self.conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (row["price"], row["user_id"]))
            await self._ledger(row["user_id"], row["price"], "refund", str(oid))
            if row["coupon"]:
                cur = await self.conn.execute("DELETE FROM coupon_uses WHERE code=? AND order_id=?", (row["coupon"], oid))
                if cur.rowcount:
                    await self.conn.execute("UPDATE coupons SET used = MAX(used - 1, 0) WHERE code=?", (row["coupon"],))
        return True

    async def pay_referral(self, oid: int, percent: float) -> tuple[int, int] | None:
        """پاداش معرف برای سفارش انجام‌شده، فقط یک بار. (آیدی معرف، مبلغ) یا None.

        پاداش = درصد از مبلغ سفارش، ولی هیچ‌وقت بیشتر از سود همان سفارش نمی‌شود تا فروشگاه ضرر نکند.
        """
        async with self._tx():
            cur = await self.conn.execute(
                "UPDATE orders SET ref_paid=1 WHERE id=? AND ref_paid=0 AND status='completed' AND refunded=0", (oid,))
            if cur.rowcount != 1:
                return None
            row = await self._one(
                "SELECT o.price, o.base_amount, u.referrer_id FROM orders o JOIN users u ON u.id=o.user_id WHERE o.id=?",
                (oid,))
            if not row or not row["referrer_id"] or percent <= 0:
                return None
            amount = min(int(row["price"] * percent // 100), max(row["price"] - row["base_amount"], 0))
            if amount <= 0:
                return None
            cur = await self.conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                                          (amount, row["referrer_id"]))
            if cur.rowcount != 1:
                return None
            await self._ledger(row["referrer_id"], amount, "referral", str(oid))
        return row["referrer_id"], amount

    async def user_orders(self, uid: int, limit: int = 10, offset: int = 0) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                               (uid, limit, offset))

    async def count_user_orders(self, uid: int) -> int:
        return (await self._one("SELECT COUNT(*) c FROM orders WHERE user_id=?", (uid,)))["c"]

    async def user_spent(self, uid: int) -> int:
        r = await self._one("SELECT COALESCE(SUM(price),0) s FROM orders WHERE user_id=? AND status='completed'", (uid,))
        return r["s"]

    async def active_orders(self) -> list[aiosqlite.Row]:
        q = ",".join("?" * len(ACTIVE_STATUSES))
        return await self._all(f"SELECT * FROM orders WHERE status IN ({q}) ORDER BY id", ACTIVE_STATUSES)

    async def orders_by_status(self, statuses: tuple[str, ...], limit: int = 20) -> list[aiosqlite.Row]:
        q = ",".join("?" * len(statuses))
        return await self._all(f"SELECT * FROM orders WHERE status IN ({q}) ORDER BY id DESC LIMIT ?",
                               (*statuses, limit))

    async def recent_orders(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    # ---------- کد تخفیف ----------
    async def create_coupon(self, code: str, percent: float, max_uses: int) -> bool:
        cur = await self._write("INSERT OR IGNORE INTO coupons(code, percent, max_uses, created_at) VALUES(?,?,?,?)",
                                (code, percent, max_uses, now()))
        return cur.rowcount == 1

    async def get_coupon(self, code: str) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM coupons WHERE code=?", (code,))

    async def list_coupons(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM coupons ORDER BY created_at DESC LIMIT 30")

    async def delete_coupon(self, code: str) -> None:
        await self._write("DELETE FROM coupons WHERE code=?", (code,))

    async def coupon_used_by(self, code: str, uid: int) -> bool:
        return await self._one("SELECT 1 FROM coupon_uses WHERE code=? AND user_id=?", (code, uid)) is not None

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
                 (SELECT COUNT(*) FROM users WHERE banned=1) banned,
                 (SELECT COALESCE(SUM(balance),0) FROM users) balances,
                 (SELECT COUNT(*) FROM orders WHERE status='completed') done,
                 (SELECT COUNT(*) FROM orders WHERE status IN ('new','pending','processing','manual')) active,
                 (SELECT COUNT(*) FROM orders WHERE status='manual') manual,
                 (SELECT COUNT(*) FROM orders WHERE refunded=1) refunded,
                 (SELECT COALESCE(SUM(price),0) FROM orders WHERE status='completed') sales,
                 (SELECT COALESCE(SUM(price-base_amount),0) FROM orders WHERE status='completed') profit,
                 (SELECT COALESCE(SUM(amount),0) FROM topups WHERE status='approved') topups,
                 (SELECT COALESCE(SUM(amount),0) FROM ledger WHERE kind='referral') referral,
                 (SELECT COUNT(*) FROM topups WHERE status='pending') pending_topups""")
        return dict(r)

    async def period_stats(self, days: int) -> dict[str, int]:
        since = ago(days)
        r = await self._one(
            """SELECT
                 (SELECT COUNT(*) FROM users WHERE created_at >= ?) users,
                 (SELECT COUNT(*) FROM orders WHERE status='completed' AND created_at >= ?) done,
                 (SELECT COALESCE(SUM(price),0) FROM orders WHERE status='completed' AND created_at >= ?) sales,
                 (SELECT COALESCE(SUM(price-base_amount),0) FROM orders WHERE status='completed' AND created_at >= ?) profit""",
            (since, since, since, since))
        return dict(r)

    async def category_stats(self) -> list[aiosqlite.Row]:
        return await self._all(
            """SELECT category, COUNT(*) n, COALESCE(SUM(price),0) sales, COALESCE(SUM(price-base_amount),0) profit
               FROM orders WHERE status='completed' GROUP BY category ORDER BY sales DESC""")


def _user(row: aiosqlite.Row) -> User:
    return User(id=row["id"], username=row["username"], first_name=row["first_name"],
                balance=row["balance"], banned=bool(row["banned"]), created_at=row["created_at"],
                referrer_id=row["referrer_id"])
