"""لایه‌ی پایگاه داده روی SQLAlchemy Core (async).

- Production: PostgreSQL (asyncpg) با Connection Pool.  DATABASE_URL=postgresql+asyncpg://…
- Development: SQLite (aiosqlite).                      DATABASE_PATH=data/shop.db

همه‌ی تغییرات مالی اتمیک‌اند: یک UPDATE شرطی (balance >= مبلغ) داخل یک تراکنش، همراه ثبت در ledger
و audit_log. برای جلوگیری از عملیات تکراری، به جای قفل داخل پردازه از قیدهای یکتای پایگاه داده
استفاده می‌شود (checkout_id سفارش، رسید شارژ، refunds.order_id، …) تا با چند نمونه‌ی هم‌زمان هم درست باشد.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from . import models

ACTIVE_STATUSES = ("new", "pending", "processing")   # سفارش‌هایی که با Stard همگام می‌شوند
OPEN_STATUSES = ACTIVE_STATUSES + ("manual",)         # همه‌ی سفارش‌های تمام‌نشده
SETTINGS_TTL = 3.0  # ثانیه؛ تغییر تنظیمات در نمونه‌های دیگر حداکثر بعد از این مدت دیده می‌شود

Row = dict[str, Any]


def _norm(v: Any) -> Any:
    # PostgreSQL برای SUM مقدار Decimal برمی‌گرداند؛ برای یکسانی با SQLite به int تبدیل می‌شود
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def _row(r) -> Row:
    return {k: _norm(v) for k, v in r._mapping.items()}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(days: float = 0, *, seconds: float = 0) -> str:
    return ts(datetime.now(timezone.utc) - timedelta(days=days, seconds=seconds))


def later(seconds: float) -> str:
    return ts(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def now_ms() -> str:
    """زمان دقیق (میلی‌ثانیه) برای صف، قفل و heartbeat؛ همه‌ی مقایسه‌های آن جدول‌ها با همین قالب‌اند."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def later_ms(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ago_ms(seconds: float) -> str:
    return later_ms(-seconds)


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class User:
    id: int
    username: str | None
    first_name: str | None
    balance: int
    banned: bool
    created_at: str
    referrer_id: int | None = None
    language_code: str | None = None
    last_seen: str | None = None
    risk_score: int = 0
    ban_reason: str | None = None
    blocked: bool = False


class InsufficientBalance(Exception):
    pass


class CouponInvalid(Exception):
    pass


def build_url(database_url: str | None, database_path: str) -> str:
    if database_url:
        url = database_url.strip()
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://"):]
        return url
    return f"sqlite+aiosqlite:///{database_path}"


class Database:
    def __init__(self, path: str = "data/shop.db", *, url: str | None = None, pool_size: int = 10,
                 max_overflow: int = 20):
        self._tmp: str | None = None
        if url is None and path == ":memory:":
            # پایگاه داده‌ی حافظه‌ای در SQLAlchemy بین اتصال‌ها مشترک نیست؛ برای تست یک فایل موقت می‌سازیم
            fd, self._tmp = tempfile.mkstemp(suffix=".db", prefix="shop-test-")
            os.close(fd)
            path = self._tmp
        self.path = path
        self.url = build_url(url, path)
        self.pool_size, self.max_overflow = pool_size, max_overflow
        self.engine: AsyncEngine | None = None
        self.is_sqlite = self.url.startswith("sqlite")
        # SQLite فقط یک نویسنده دارد؛ داخل پردازه نوشتن‌ها را صف می‌کنیم تا busy نشود.
        # بین پردازه‌ها busy_timeout و BEGIN IMMEDIATE کار را انجام می‌دهند.
        self._write_lock = asyncio.Lock()
        self._settings: dict[str, str] | None = None
        self._settings_at = 0.0

    @property
    def dialect(self) -> str:
        return "sqlite" if self.is_sqlite else "postgresql"

    async def connect(self, *, create: bool = True) -> None:
        """اتصال و ساخت/ارتقای شِما. create=False یعنی فقط اتصال (مثلاً برای مهاجرت دستی)."""
        if self.is_sqlite:
            db_file = self.url.split("///", 1)[1]
            os.makedirs(os.path.dirname(os.path.abspath(db_file)), exist_ok=True)
            self.engine = create_async_engine(self.url, connect_args={"timeout": 30})
            _sqlite_events(self.engine)
        else:
            self.engine = create_async_engine(self.url, pool_size=self.pool_size, max_overflow=self.max_overflow,
                                              pool_pre_ping=True, pool_recycle=1800)
        if create:
            from .migrations_runner import upgrade
            await upgrade(self)

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None
        if self._tmp:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(self._tmp + suffix)
                except OSError:
                    pass
            self._tmp = None

    # ---------- کمکی ----------
    @asynccontextmanager
    async def tx(self) -> AsyncIterator[AsyncConnection]:
        """تراکنش نوشتن. در SQLite با BEGIN IMMEDIATE (قفل نوشتن از ابتدا) و صف داخل پردازه."""
        if self.is_sqlite:
            async with self._write_lock:
                async with self.engine.connect() as c:
                    c.sync_connection.info["immediate"] = True
                    try:
                        async with c.begin():
                            yield c
                    finally:
                        c.sync_connection.info.pop("immediate", None)
        else:
            async with self.engine.begin() as c:
                yield c

    async def one(self, sql: str, params: dict | None = None, *, c: AsyncConnection | None = None) -> Row | None:
        if c is not None:
            r = (await c.execute(text(sql), params or {})).first()
            return _row(r) if r is not None else None
        async with self.engine.connect() as conn:
            r = (await conn.execute(text(sql), params or {})).first()
            return _row(r) if r is not None else None

    async def all(self, sql: str, params: dict | None = None, *, c: AsyncConnection | None = None) -> list[Row]:
        if c is not None:
            return [_row(r) for r in await c.execute(text(sql), params or {})]
        async with self.engine.connect() as conn:
            return [_row(r) for r in await conn.execute(text(sql), params or {})]

    async def scalar(self, sql: str, params: dict | None = None) -> Any:
        async with self.engine.connect() as conn:
            return _norm((await conn.execute(text(sql), params or {})).scalar())

    async def write(self, sql: str, params: dict | None = None) -> int:
        """یک دستور نوشتن در تراکنش خودش؛ تعداد ردیف‌های تغییرکرده را برمی‌گرداند."""
        async with self.tx() as c:
            return (await c.execute(text(sql), params or {})).rowcount

    async def ping(self) -> float:
        """زمان پاسخ پایگاه داده به ثانیه."""
        t = time.perf_counter()
        await self.scalar("SELECT 1")
        return time.perf_counter() - t

    @staticmethod
    async def _x(c: AsyncConnection, sql: str, params: dict | None = None):
        return await c.execute(text(sql), params or {})

    # ---------- audit ----------
    @staticmethod
    async def audit_in(c: AsyncConnection, *, admin_id: int | None, action: str, user_id: int | None = None,
                       order_id: int | None = None, ref: str | None = None, amount: int | None = None,
                       before: Any = None, after: Any = None, reason: str | None = None) -> None:
        def enc(v):
            return None if v is None else json.dumps(v, ensure_ascii=False, default=str)[:4000]
        await c.execute(text(
            "INSERT INTO audit_log(admin_id, action, user_id, order_id, ref, amount, before, after, reason, created_at) "
            "VALUES(:a, :act, :u, :o, :ref, :amt, :b, :af, :r, :t)"),
            {"a": admin_id, "act": action, "u": user_id, "o": order_id, "ref": ref, "amt": amount,
             "b": enc(before), "af": enc(after), "r": (reason or None) and reason[:500], "t": now()})

    async def audit(self, **kw) -> None:
        async with self.tx() as c:
            await self.audit_in(c, **kw)

    async def audit_entries(self, *, limit: int = 20, offset: int = 0, admin_id: int | None = None,
                            user_id: int | None = None, action: str | None = None) -> list[Row]:
        where, p = ["1=1"], {"lim": limit, "off": offset}
        if admin_id is not None:
            where.append("admin_id = :a")
            p["a"] = admin_id
        if user_id is not None:
            where.append("user_id = :u")
            p["u"] = user_id
        if action:
            where.append("action = :act")
            p["act"] = action
        return await self.all(f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY id DESC "
                              "LIMIT :lim OFFSET :off", p)

    # ---------- تنظیمات (کش کوتاه‌مدت؛ چندنمونه‌ای امن) ----------
    async def _load_settings(self) -> dict[str, str]:
        if self._settings is None or time.monotonic() - self._settings_at > SETTINGS_TTL:
            self._settings = {r["key"]: r["value"] for r in await self.all("SELECT key, value FROM settings")}
            self._settings_at = time.monotonic()
        return self._settings

    async def get_setting(self, key: str, default: Any = None) -> Any:
        return (await self._load_settings()).get(key, default)

    async def set_setting(self, key: str, value: Any) -> None:
        await self.write("INSERT INTO settings(key, value) VALUES(:k, :v) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", {"k": key, "v": str(value)})
        (await self._load_settings())[key] = str(value)

    async def del_setting(self, key: str) -> None:
        await self.write("DELETE FROM settings WHERE key = :k", {"k": key})
        (await self._load_settings()).pop(key, None)

    def invalidate_settings(self) -> None:
        self._settings = None

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
                          referrer_id: int | None = None, language_code: str | None = None) -> tuple[User, bool]:
        """کاربر را ثبت یا به‌روز می‌کند. (کاربر، تازه‌ساخته‌شده؟) را برمی‌گرداند.

        معرف فقط موقع ساخت کاربر ثبت می‌شود. last_seen حداکثر هر ۶۰ ثانیه یک بار نوشته می‌شود تا فشار
        نوشتن روی پایگاه داده کم بماند.
        """
        u = await self.get_user(uid)
        t = now()
        if u is not None:
            stale = (u.last_seen or "") < ago(seconds=60)
            if u.username != username or u.first_name != first_name or stale or \
                    (language_code and u.language_code != language_code):
                await self.write("UPDATE users SET username=:un, first_name=:fn, last_seen=:t, blocked=0, "
                                 "language_code=COALESCE(:lc, language_code) WHERE id=:id",
                                 {"un": username, "fn": first_name, "t": t, "lc": language_code, "id": uid})
                u.username, u.first_name, u.last_seen = username, first_name, t
                u.language_code = language_code or u.language_code
            return u, False
        if referrer_id == uid or (referrer_id is not None and await self.get_user(referrer_id) is None):
            referrer_id = None
        n = await self.write(
            "INSERT INTO users(id, username, first_name, language_code, referrer_id, last_seen, created_at) "
            "VALUES(:id, :un, :fn, :lc, :ref, :t, :t) ON CONFLICT(id) DO NOTHING",
            {"id": uid, "un": username, "fn": first_name, "lc": language_code, "ref": referrer_id, "t": t})
        return await self.get_user(uid), n == 1

    async def get_user(self, uid: int) -> User | None:
        row = await self.one("SELECT * FROM users WHERE id = :id", {"id": uid})
        return _user(row) if row else None

    async def find_user(self, query: str) -> User | None:
        q = query.strip().lstrip("@")
        if q.isdigit():
            return await self.get_user(int(q))
        row = await self.one("SELECT * FROM users WHERE lower(username) = lower(:q)", {"q": q})
        return _user(row) if row else None

    async def set_banned(self, uid: int, banned: bool, *, admin_id: int | None = None, reason: str | None = None) -> bool:
        async with self.tx() as c:
            before = await self.one("SELECT banned FROM users WHERE id = :id", {"id": uid}, c=c)
            if before is None:
                return False
            await self._x(c, "UPDATE users SET banned = :b, ban_reason = :r WHERE id = :id",
                          {"b": int(banned), "r": reason if banned else None, "id": uid})
            await self.audit_in(c, admin_id=admin_id, action="ban" if banned else "unban", user_id=uid,
                                before={"banned": before["banned"]}, after={"banned": int(banned)}, reason=reason)
        return True

    async def all_user_ids(self) -> list[int]:
        return [r["id"] for r in await self.all("SELECT id FROM users WHERE banned = 0")]

    async def user_ids_page(self, after_id: int, limit: int, *, inactive_since: str | None = None) -> list[int]:
        """صفحه‌بندی بر اساس آیدی (برای پیام همگانی با تعداد بسیار زیاد کاربر)."""
        extra = " AND (last_seen IS NULL OR last_seen < :since)" if inactive_since else ""
        rows = await self.all(f"SELECT id FROM users WHERE banned = 0 AND id > :a{extra} ORDER BY id LIMIT :l",
                              {"a": after_id, "l": limit, "since": inactive_since})
        return [r["id"] for r in rows]

    async def count_users(self, *, inactive_since: str | None = None) -> int:
        if inactive_since:
            return await self.scalar("SELECT COUNT(*) FROM users WHERE banned = 0 AND "
                                     "(last_seen IS NULL OR last_seen < :s)", {"s": inactive_since})
        return await self.scalar("SELECT COUNT(*) FROM users WHERE banned = 0")

    async def count_referrals(self, uid: int) -> int:
        return await self.scalar("SELECT COUNT(*) FROM users WHERE referrer_id = :u", {"u": uid})

    async def referral_earnings(self, uid: int) -> int:
        return await self.scalar("SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE user_id = :u AND kind = 'referral'",
                                 {"u": uid})

    async def top_buyers(self, limit: int = 10) -> list[Row]:
        return await self.all(
            """SELECT u.id, u.username, u.first_name, COUNT(o.id) AS n, SUM(o.price) AS total
               FROM orders o JOIN users u ON u.id = o.user_id WHERE o.status = 'completed' AND o.is_test = 0
               GROUP BY u.id, u.username, u.first_name ORDER BY total DESC LIMIT :l""", {"l": limit})

    # ---------- موجودی (اتمیک) ----------
    async def credit(self, uid: int, amount: int, kind: str, ref: str | None = None, *,
                     admin_id: int | None = None, reason: str | None = None) -> int:
        if amount <= 0:
            raise ValueError("amount must be positive")
        async with self.tx() as c:
            r = (await self._x(c, "UPDATE users SET balance = balance + :a WHERE id = :u RETURNING balance",
                               {"a": amount, "u": uid})).first()
            if r is None:
                raise ValueError("user not found")
            await self._ledger(c, uid, amount, kind, ref)
            if kind == "admin":
                await self.audit_in(c, admin_id=admin_id, action="balance_add", user_id=uid, amount=amount, ref=ref,
                                    before={"balance": r[0] - amount}, after={"balance": r[0]}, reason=reason)
        return r[0]

    async def debit(self, uid: int, amount: int, kind: str, ref: str | None = None, *,
                    admin_id: int | None = None, reason: str | None = None) -> int:
        if amount <= 0:
            raise ValueError("amount must be positive")
        async with self.tx() as c:
            r = (await self._x(c, "UPDATE users SET balance = balance - :a WHERE id = :u AND balance >= :a "
                                  "RETURNING balance", {"a": amount, "u": uid})).first()
            if r is None:
                raise InsufficientBalance()
            await self._ledger(c, uid, -amount, kind, ref)
            if kind == "admin":
                await self.audit_in(c, admin_id=admin_id, action="balance_sub", user_id=uid, amount=amount, ref=ref,
                                    before={"balance": r[0] + amount}, after={"balance": r[0]}, reason=reason)
        return r[0]

    async def _ledger(self, c: AsyncConnection, uid: int, amount: int, kind: str, ref: str | None) -> None:
        await self._x(c, "INSERT INTO ledger(user_id, amount, kind, ref, created_at) VALUES(:u, :a, :k, :r, :t)",
                      {"u": uid, "a": amount, "k": kind, "r": ref, "t": now()})

    async def user_ledger(self, uid: int, limit: int = 10) -> list[Row]:
        return await self.all("SELECT * FROM ledger WHERE user_id = :u ORDER BY id DESC LIMIT :l", {"u": uid, "l": limit})

    async def ledger_balance_check(self) -> list[Row]:
        """کاربرانی که موجودی‌شان با مجموع ledger نمی‌خواند (باید همیشه خالی باشد)."""
        return await self.all(
            """SELECT u.id, u.balance, COALESCE(SUM(l.amount), 0) AS ledger_sum FROM users u
               LEFT JOIN ledger l ON l.user_id = u.id GROUP BY u.id, u.balance
               HAVING u.balance != COALESCE(SUM(l.amount), 0)""")

    # ---------- سفارش ----------
    async def create_order_and_debit(self, *, user_id: int, type_: str, category: str, product_id: int | None,
                                     title: str, quantity: int, recipient: str | None, gift_message: str | None,
                                     quote_id: str | None, base_amount: int, price: int,
                                     duration: int | None = None, status: str = "new",
                                     coupon: str | None = None, discount: int = 0,
                                     checkout_id: str | None = None, is_test: bool = False,
                                     stock_key: str | None = None, after_insert=None) -> int:
        """کسر موجودی، ثبت کد تخفیف، کسر موجودی انبار و ساخت سفارش در یک تراکنش.

        checkout_id: اگر با همین شناسه قبلاً سفارشی ساخته شده باشد (کلیک دوباره، دو نمونه‌ی ربات، تلاش
        دوباره بعد از قطعی)، همان سفارش برمی‌گردد و هیچ پولی دوباره کسر نمی‌شود.
        after_insert(c, oid): کار اضافه داخل همان تراکنش (مثلاً صف کردن ارسال سفارش — الگوی outbox).
        """
        if checkout_id:
            existing = await self.one("SELECT id FROM orders WHERE checkout_id = :c", {"c": checkout_id})
            if existing:
                return existing["id"]
        try:
            async with self.tx() as c:
                t = now()
                oid = (await self._x(c,
                    """INSERT INTO orders(user_id, type, category, product_id, title, quantity, duration, recipient,
                                          gift_message, quote_id, base_amount, price, discount, coupon, status,
                                          checkout_id, is_test, created_at, updated_at)
                       VALUES(:u, :ty, :cat, :pid, :ti, :q, :d, :rcp, :gm, :qid, :base, :p, :disc, :cp, :st,
                              :chk, :test, :t, :t) RETURNING id""",
                    {"u": user_id, "ty": type_, "cat": category, "pid": product_id, "ti": title, "q": quantity,
                     "d": duration, "rcp": recipient, "gm": gift_message, "qid": quote_id, "base": base_amount,
                     "p": price, "disc": discount, "cp": coupon, "st": status, "chk": checkout_id,
                     "test": int(is_test), "t": t})).scalar_one()
                r = await self._x(c, "UPDATE users SET balance = balance - :p WHERE id = :u AND balance >= :p",
                                  {"p": price, "u": user_id})
                if r.rowcount != 1:
                    raise InsufficientBalance()
                if coupon:
                    r = await self._x(c, "UPDATE coupons SET used = used + 1 WHERE code = :c AND active = 1 "
                                         "AND (max_uses = 0 OR used < max_uses) "
                                         "AND (expires_at IS NULL OR expires_at > :t)", {"c": coupon, "t": t})
                    if r.rowcount != 1:
                        raise CouponInvalid()
                    try:
                        async with c.begin_nested():
                            await self._x(c, "INSERT INTO coupon_uses(code, user_id, order_id) VALUES(:c, :u, :o)",
                                          {"c": coupon, "u": user_id, "o": oid})
                    except IntegrityError as e:
                        raise CouponInvalid() from e
                if stock_key:
                    r = await self._x(c, "UPDATE product_controls SET sold = sold + :q WHERE key = :k "
                                         "AND (stock IS NULL OR stock - sold >= :q)", {"q": quantity, "k": stock_key})
                    if r.rowcount != 1 and await self.one("SELECT 1 AS x FROM product_controls WHERE key = :k",
                                                          {"k": stock_key}, c=c):
                        raise OutOfStock()
                await self._ledger(c, user_id, -price, "order", str(oid))
                if after_insert is not None:
                    await after_insert(c, oid)
        except IntegrityError:
            if checkout_id:
                existing = await self.one("SELECT id FROM orders WHERE checkout_id = :c", {"c": checkout_id})
                if existing:
                    return existing["id"]
            raise
        return oid

    async def get_order(self, oid: int) -> Row | None:
        return await self.one("SELECT * FROM orders WHERE id = :id", {"id": oid})

    async def get_order_by_ref(self, ref: str) -> Row | None:
        return await self.one("SELECT * FROM orders WHERE stard_ref = :r", {"r": ref})

    async def update_order(self, oid: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        await self.write(f"UPDATE orders SET {cols} WHERE id = :__id", {**fields, "__id": oid})

    async def set_order_status(self, oid: int, new: str, *, only_from: tuple[str, ...],
                               admin_id: int | None = None, reason: str | None = None) -> bool:
        """تغییر وضعیت فقط اگر وضعیت فعلی یکی از only_from باشد (جلوگیری از رقابت دو مدیر/worker)."""
        params = {"new": new, "t": now(), "id": oid, **{f"s{i}": s for i, s in enumerate(only_from)}}
        q = ", ".join(f":s{i}" for i in range(len(only_from)))
        async with self.tx() as c:
            r = await self._x(c, f"UPDATE orders SET status = :new, updated_at = :t WHERE id = :id "
                                 f"AND status IN ({q}) AND refunded = 0", params)
            ok = r.rowcount == 1
            if ok and admin_id is not None:
                await self.audit_in(c, admin_id=admin_id, action=f"order_{new}", order_id=oid,
                                    before={"status": only_from}, after={"status": new}, reason=reason)
        return ok

    async def refund_order(self, oid: int, status: str, reason: str | None = None, *,
                           admin_id: int | None = None) -> bool:
        """برگشت پول سفارش به کاربر، فقط یک بار. True اگر همین فراخوانی پول را برگرداند.

        سه لایه‌ی محافظت: شرط refunded=0 روی UPDATE، کلید اصلی refunds.order_id، و تراکنش واحد.
        سفارش انجام‌شده برگشت نمی‌خورد. کد تخفیف و موجودی انبار مصرف‌شده آزاد می‌شوند.
        """
        async with self.tx() as c:
            t = now()
            r = await self._x(c, "UPDATE orders SET refunded = 1, status = :s, "
                                 "failure_reason = COALESCE(:r, failure_reason), updated_at = :t "
                                 "WHERE id = :id AND refunded = 0 AND status != 'completed'",
                              {"s": status, "r": reason, "t": t, "id": oid})
            if r.rowcount != 1:
                return False
            row = await self.one("SELECT user_id, price, coupon, category, product_id, quantity FROM orders "
                                 "WHERE id = :id", {"id": oid}, c=c)
            await self._x(c, "INSERT INTO refunds(order_id, user_id, amount, status, reason, admin_id, created_at, "
                             "updated_at) VALUES(:o, :u, :a, 'completed', :r, :adm, :t, :t) "
                             "ON CONFLICT(order_id) DO UPDATE SET status = 'completed', amount = excluded.amount, "
                             "error = NULL, updated_at = excluded.updated_at, "
                             "admin_id = COALESCE(excluded.admin_id, refunds.admin_id)",
                          {"o": oid, "u": row["user_id"], "a": row["price"], "r": reason, "adm": admin_id, "t": t})
            await self._x(c, "UPDATE users SET balance = balance + :p WHERE id = :u",
                          {"p": row["price"], "u": row["user_id"]})
            await self._ledger(c, row["user_id"], row["price"], "refund", str(oid))
            if row["coupon"]:
                r = await self._x(c, "DELETE FROM coupon_uses WHERE code = :c AND order_id = :o",
                                  {"c": row["coupon"], "o": oid})
                if r.rowcount:
                    await self._x(c, "UPDATE coupons SET used = CASE WHEN used > 0 THEN used - 1 ELSE 0 END "
                                     "WHERE code = :c", {"c": row["coupon"]})
            for key in (f"{row['category']}:{row['product_id']}" if row["product_id"] else None, row["category"]):
                if key:
                    await self._x(c, "UPDATE product_controls SET sold = CASE WHEN sold >= :q THEN sold - :q "
                                     "ELSE 0 END WHERE key = :k", {"q": row["quantity"], "k": key})
            await self.audit_in(c, admin_id=admin_id, action="refund", user_id=row["user_id"], order_id=oid,
                                amount=row["price"], after={"status": status}, reason=reason)
        return True

    async def pay_referral(self, oid: int, percent: float) -> tuple[int, int] | None:
        """پاداش معرف برای سفارش انجام‌شده، فقط یک بار. (آیدی معرف، مبلغ) یا None.

        پاداش = درصد از مبلغ سفارش، ولی هیچ‌وقت بیشتر از سود همان سفارش نمی‌شود تا فروشگاه ضرر نکند.
        """
        async with self.tx() as c:
            r = await self._x(c, "UPDATE orders SET ref_paid = 1 WHERE id = :id AND ref_paid = 0 "
                                 "AND status = 'completed' AND refunded = 0", {"id": oid})
            if r.rowcount != 1:
                return None
            row = await self.one("SELECT o.price, o.base_amount, u.referrer_id FROM orders o "
                                 "JOIN users u ON u.id = o.user_id WHERE o.id = :id", {"id": oid}, c=c)
            if not row or not row["referrer_id"] or percent <= 0:
                return None
            amount = min(int(row["price"] * percent // 100), max(row["price"] - row["base_amount"], 0))
            if amount <= 0:
                return None
            r = await self._x(c, "UPDATE users SET balance = balance + :a WHERE id = :u",
                              {"a": amount, "u": row["referrer_id"]})
            if r.rowcount != 1:
                return None
            await self._ledger(c, row["referrer_id"], amount, "referral", str(oid))
        return row["referrer_id"], amount

    async def user_orders(self, uid: int, limit: int = 10, offset: int = 0) -> list[Row]:
        return await self.all("SELECT * FROM orders WHERE user_id = :u ORDER BY id DESC LIMIT :l OFFSET :o",
                              {"u": uid, "l": limit, "o": offset})

    async def count_user_orders(self, uid: int) -> int:
        return await self.scalar("SELECT COUNT(*) FROM orders WHERE user_id = :u", {"u": uid})

    async def user_spent(self, uid: int) -> int:
        return await self.scalar("SELECT COALESCE(SUM(price), 0) FROM orders WHERE user_id = :u "
                                 "AND status = 'completed' AND is_test = 0", {"u": uid})

    async def user_order_stats(self, uid: int) -> Row:
        return await self.one(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) AS ok,
                      COALESCE(SUM(CASE WHEN refunded = 1 THEN 1 ELSE 0 END), 0) AS failed,
                      COALESCE(SUM(CASE WHEN status = 'completed' THEN price ELSE 0 END), 0) AS spent,
                      COALESCE(SUM(CASE WHEN status = 'completed' THEN price - base_amount ELSE 0 END), 0) AS profit,
                      MAX(created_at) AS last_order
               FROM orders WHERE user_id = :u""", {"u": uid})

    async def user_orders_since(self, uid: int, since: str, *, category: str | None = None,
                                product_id: int | None = None) -> int:
        """تعداد سفارش‌های غیربرگشتی کاربر از یک زمان (برای سقف خرید)."""
        sql = "SELECT COALESCE(SUM(quantity), 0) FROM orders WHERE user_id = :u AND created_at >= :s AND refunded = 0"
        p: dict[str, Any] = {"u": uid, "s": since}
        if category:
            sql += " AND category = :c"
            p["c"] = category
        if product_id is not None:
            sql += " AND product_id = :p"
            p["p"] = product_id
        return await self.scalar(sql, p)

    async def count_orders_since(self, uid: int, since: str) -> int:
        return await self.scalar("SELECT COUNT(*) FROM orders WHERE user_id = :u AND created_at >= :s "
                                 "AND refunded = 0", {"u": uid, "s": since})

    async def active_orders(self) -> list[Row]:
        return await self.all("SELECT * FROM orders WHERE status IN ('new', 'pending', 'processing') ORDER BY id")

    async def orders_by_status(self, statuses: tuple[str, ...], limit: int = 20) -> list[Row]:
        p = {f"s{i}": s for i, s in enumerate(statuses)}
        q = ", ".join(f":s{i}" for i in range(len(statuses)))
        return await self.all(f"SELECT * FROM orders WHERE status IN ({q}) ORDER BY id DESC LIMIT :l", {**p, "l": limit})

    async def recent_orders(self, limit: int = 10) -> list[Row]:
        return await self.all("SELECT * FROM orders ORDER BY id DESC LIMIT :l", {"l": limit})

    # ---------- کد تخفیف ----------
    async def create_coupon(self, code: str, percent: float, max_uses: int, *, user_id: int | None = None,
                            category: str | None = None, expires_at: str | None = None,
                            admin_id: int | None = None) -> bool:
        async with self.tx() as c:
            r = await self._x(c, "INSERT INTO coupons(code, percent, max_uses, user_id, category, expires_at, created_at) "
                                 "VALUES(:c, :p, :m, :u, :cat, :e, :t) ON CONFLICT(code) DO NOTHING",
                              {"c": code, "p": percent, "m": max_uses, "u": user_id, "cat": category,
                               "e": expires_at, "t": now()})
            if r.rowcount == 1:
                await self.audit_in(c, admin_id=admin_id, action="coupon_create", user_id=user_id, ref=code,
                                    after={"percent": percent, "max_uses": max_uses, "category": category,
                                           "expires_at": expires_at})
            return r.rowcount == 1

    async def get_coupon(self, code: str) -> Row | None:
        return await self.one("SELECT * FROM coupons WHERE code = :c", {"c": code})

    async def list_coupons(self, *, user_id: int | None = None) -> list[Row]:
        if user_id is not None:
            return await self.all("SELECT * FROM coupons WHERE user_id = :u ORDER BY created_at DESC", {"u": user_id})
        return await self.all("SELECT * FROM coupons ORDER BY created_at DESC LIMIT 30")

    async def delete_coupon(self, code: str, *, admin_id: int | None = None) -> None:
        async with self.tx() as c:
            await self._x(c, "DELETE FROM coupons WHERE code = :c", {"c": code})
            await self.audit_in(c, admin_id=admin_id, action="coupon_delete", ref=code)

    async def coupon_used_by(self, code: str, uid: int) -> bool:
        return await self.one("SELECT 1 AS x FROM coupon_uses WHERE code = :c AND user_id = :u",
                              {"c": code, "u": uid}) is not None

    async def expiring_offers(self, within_hours: int) -> list[Row]:
        return await self.all("SELECT * FROM coupons WHERE user_id IS NOT NULL AND active = 1 AND expires_at IS NOT NULL "
                              "AND expires_at > :n AND expires_at <= :e",
                              {"n": now(), "e": later(within_hours * 3600)})

    # ---------- شارژ ----------
    async def create_topup(self, uid: int, amount: int, photo_id: str,
                           file_unique_id: str | None = None) -> tuple[int, bool]:
        """(شناسه، تازه؟). همان رسید (file_unique_id) دو بار ثبت نمی‌شود."""
        if file_unique_id:
            ex = await self.one("SELECT id FROM topups WHERE user_id = :u AND file_unique_id = :f",
                                {"u": uid, "f": file_unique_id})
            if ex:
                return ex["id"], False
        try:
            async with self.tx() as c:
                tid = (await self._x(c, "INSERT INTO topups(user_id, amount, photo_id, file_unique_id, status, created_at) "
                                        "VALUES(:u, :a, :p, :f, 'pending', :t) RETURNING id",
                                     {"u": uid, "a": amount, "p": photo_id, "f": file_unique_id, "t": now()})).scalar_one()
        except IntegrityError:
            ex = await self.one("SELECT id FROM topups WHERE user_id = :u AND file_unique_id = :f",
                                {"u": uid, "f": file_unique_id})
            if ex:
                return ex["id"], False
            raise
        return tid, True

    async def get_topup(self, tid: int) -> Row | None:
        return await self.one("SELECT * FROM topups WHERE id = :id", {"id": tid})

    async def pending_topups(self) -> list[Row]:
        return await self.all("SELECT * FROM topups WHERE status = 'pending' ORDER BY id")

    async def resolve_topup(self, tid: int, admin_id: int, approve: bool) -> Row | None:
        """تأیید یا رد شارژ، فقط یک بار (جلوگیری از دوبار زدن دکمه توسط دو مدیر یا دو نمونه)."""
        async with self.tx() as c:
            r = await self._x(c, "UPDATE topups SET status = :s, admin_id = :a WHERE id = :id AND status = 'pending'",
                              {"s": "approved" if approve else "rejected", "a": admin_id, "id": tid})
            if r.rowcount != 1:
                return None
            row = await self.one("SELECT * FROM topups WHERE id = :id", {"id": tid}, c=c)
            if approve:
                await self._x(c, "UPDATE users SET balance = balance + :a WHERE id = :u",
                              {"a": row["amount"], "u": row["user_id"]})
                await self._ledger(c, row["user_id"], row["amount"], "topup", str(tid))
            await self.audit_in(c, admin_id=admin_id, action="topup_approve" if approve else "topup_reject",
                                user_id=row["user_id"], ref=f"topup:{tid}", amount=row["amount"],
                                before={"status": "pending"}, after={"status": row["status"]})
        return row

    # ---------- آمار ----------
    async def stats(self) -> dict[str, int]:
        r = await self.one(
            """SELECT
                 (SELECT COUNT(*) FROM users) AS users,
                 (SELECT COUNT(*) FROM users WHERE banned = 1) AS banned,
                 (SELECT COALESCE(SUM(balance), 0) FROM users) AS balances,
                 (SELECT COUNT(*) FROM orders WHERE status = 'completed' AND is_test = 0) AS done,
                 (SELECT COUNT(*) FROM orders WHERE status IN ('new', 'pending', 'processing', 'manual')) AS active,
                 (SELECT COUNT(*) FROM orders WHERE status = 'manual') AS manual,
                 (SELECT COUNT(*) FROM orders WHERE refunded = 1) AS refunded,
                 (SELECT COALESCE(SUM(price), 0) FROM orders WHERE status = 'completed' AND is_test = 0) AS sales,
                 (SELECT COALESCE(SUM(price - base_amount), 0) FROM orders WHERE status = 'completed' AND is_test = 0) AS profit,
                 (SELECT COALESCE(SUM(amount), 0) FROM topups WHERE status = 'approved') AS topups,
                 (SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE kind = 'referral') AS referral,
                 (SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE kind = 'reward') AS rewards,
                 (SELECT COUNT(*) FROM topups WHERE status = 'pending') AS pending_topups""")
        return {k: int(v or 0) for k, v in r.items()}

    async def period_stats(self, days: float) -> dict[str, int]:
        since = ago(days)
        r = await self.one(
            """SELECT
                 (SELECT COUNT(*) FROM users WHERE created_at >= :s) AS users,
                 (SELECT COUNT(*) FROM orders WHERE status = 'completed' AND is_test = 0 AND created_at >= :s) AS done,
                 (SELECT COALESCE(SUM(price), 0) FROM orders WHERE status = 'completed' AND is_test = 0 AND created_at >= :s) AS sales,
                 (SELECT COALESCE(SUM(price - base_amount), 0) FROM orders
                    WHERE status = 'completed' AND is_test = 0 AND created_at >= :s) AS profit""", {"s": since})
        return {k: int(v or 0) for k, v in r.items()}

    async def category_stats(self) -> list[Row]:
        return await self.all(
            """SELECT category, COUNT(*) AS n, COALESCE(SUM(price), 0) AS sales,
                      COALESCE(SUM(price - base_amount), 0) AS profit
               FROM orders WHERE status = 'completed' AND is_test = 0 GROUP BY category ORDER BY sales DESC""")

    # ---------- پشتیبان (SQLite) ----------
    async def backup_sqlite(self, dest: str) -> None:
        """یک کپی سالم و فشرده (حتی وقتی ربات روشن است)."""
        if not self.is_sqlite:
            raise RuntimeError("use pg_dump for PostgreSQL")
        if os.path.exists(dest):
            os.remove(dest)
        async with self._write_lock:
            async with self.engine.connect() as c:
                # VACUUM نباید داخل تراکنش باشد؛ مستقیم روی اتصال درایور اجرا می‌شود
                raw = await c.get_raw_connection()
                await raw.driver_connection.execute("VACUUM INTO ?", (dest,))

    backup = backup_sqlite  # سازگاری با نسخه‌ی ۲


class OutOfStock(Exception):
    pass


def _sqlite_events(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None  # کنترل تراکنش با خودمان (BEGIN صریح)
        cur = dbapi_conn.cursor()
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON", "busy_timeout=30000"):
            cur.execute(f"PRAGMA {pragma}")
        cur.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE" if conn.info.get("immediate") else "BEGIN")


def _user(row: Row) -> User:
    return User(id=row["id"], username=row["username"], first_name=row["first_name"],
                balance=int(row["balance"]), banned=bool(row["banned"]), created_at=row["created_at"],
                referrer_id=row.get("referrer_id"), language_code=row.get("language_code"),
                last_seen=row.get("last_seen"), risk_score=int(row.get("risk_score") or 0),
                ban_reason=row.get("ban_reason"), blocked=bool(row.get("blocked") or 0))


__all__ = ["Database", "User", "InsufficientBalance", "CouponInvalid", "OutOfStock", "models", "now", "ago",
           "later", "today", "ts", "ACTIVE_STATUSES", "OPEN_STATUSES"]
