"""صف کار پایدار روی پایگاه داده (بدون زیرساخت اضافه)، امن برای چند نمونه‌ی هم‌زمان.

چرخه‌ی هر کار:
    queued ──claim──► running ──ok──► done
       ▲                 │
       └──── خطا (backoff) ┤
                          └── تلاش‌ها تمام شد ──► dead   (Dead Letter Queue؛ از پنل Retry می‌شود)

- Lease: کار در حال اجرا تا locked_until مال یک worker است. اگر worker کرش کند، lease منقضی و کار
  توسط worker دیگری دوباره گرفته می‌شود (Crash Recovery). هر بار گرفتن یک تلاش حساب می‌شود، پس کاری
  که worker را کرش می‌دهد بی‌نهایت تکرار نمی‌شود.
- Fencing: complete/fail فقط اگر locked_by هنوز همین worker باشد اثر دارد؛ worker کندی که lease را از
  دست داده نمی‌تواند نتیجه‌ی worker جدید را خراب کند.
- dedupe_key: برای هر کلید حداکثر یک کار فعال (مثلاً order:42)؛ وقتی کار تمام یا dead شد آزاد می‌شود.
- PostgreSQL: SELECT … FOR UPDATE SKIP LOCKED (چند worker بدون تداخل). SQLite: BEGIN IMMEDIATE.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .db import Database, later_ms as later, now_ms as now

LEASE_SECONDS = 120
MAX_BACKOFF = 900


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int
    max_attempts: int
    dedupe_key: str | None
    created_at: str
    run_at: str


class RetryLater(Exception):
    """کار سالم است ولی باید بعداً دوباره اجرا شود (بدون شمردن به عنوان شکست)."""

    def __init__(self, delay: float, reason: str = ""):
        super().__init__(reason or f"retry in {delay}s")
        self.delay = delay


class PermanentError(Exception):
    """تلاش دوباره فایده ندارد؛ کار مستقیم به dead می‌رود."""


def backoff(attempts: int, base: float = 5.0) -> float:
    """فاصله‌ی افزایشی با jitter: 5، 10، 20، 40 ثانیه، … حداکثر ۱۵ دقیقه."""
    d = min(base * (2 ** max(attempts - 1, 0)), MAX_BACKOFF)
    return d * random.uniform(0.8, 1.2)


class JobQueue:
    def __init__(self, db: Database, instance_id: str):
        self.db = db
        self.me = instance_id[:64]

    async def enqueue(self, kind: str, payload: dict | None = None, *, dedupe_key: str | None = None,
                      delay: float = 0, max_attempts: int = 8, c: AsyncConnection | None = None) -> bool:
        """True اگر کار تازه ساخته شد؛ False اگر کار فعال با همین dedupe_key از قبل هست."""
        params = {"k": kind, "p": json.dumps(payload or {}, ensure_ascii=False), "d": dedupe_key,
                  "m": max_attempts, "r": later(delay) if delay else now(), "t": now()}
        sql = ("INSERT INTO jobs(kind, payload, dedupe_key, status, attempts, max_attempts, run_at, created_at, "
               "updated_at) VALUES(:k, :p, :d, 'queued', 0, :m, :r, :t, :t)")
        if dedupe_key:
            sql += " ON CONFLICT(dedupe_key) DO NOTHING"
        if c is not None:
            return (await c.execute(text(sql), params)).rowcount == 1
        return await self.db.write(sql, params) == 1

    async def claim(self, limit: int, lease: float = LEASE_SECONDS) -> list[Job]:
        if limit <= 0:
            return []
        t = now()
        skip = "" if self.db.is_sqlite else " FOR UPDATE SKIP LOCKED"
        sql = (f"UPDATE jobs SET status = 'running', locked_by = :me, locked_until = :lease, "
               f"attempts = attempts + 1, updated_at = :t WHERE id IN (SELECT id FROM jobs WHERE "
               f"(status = 'queued' AND run_at <= :t) OR (status = 'running' AND locked_until < :t) "
               f"ORDER BY run_at LIMIT :n{skip}) RETURNING id, kind, payload, attempts, max_attempts, dedupe_key, "
               f"created_at, run_at")
        async with self.db.tx() as c:
            rows = (await c.execute(text(sql), {"me": self.me, "lease": later(lease), "t": t, "n": limit})).all()
        jobs = []
        for r in rows:
            try:
                payload = json.loads(r.payload or "{}")
            except ValueError:
                payload = {}
            jobs.append(Job(r.id, r.kind, payload, r.attempts, r.max_attempts, r.dedupe_key, r.created_at, r.run_at))
        return sorted(jobs, key=lambda j: j.run_at)

    async def extend(self, job: Job, lease: float = LEASE_SECONDS) -> bool:
        return await self.db.write("UPDATE jobs SET locked_until = :l, updated_at = :t WHERE id = :id "
                                   "AND locked_by = :me AND status = 'running'",
                                   {"l": later(lease), "t": now(), "id": job.id, "me": self.me}) == 1

    async def complete(self, job: Job) -> bool:
        return await self.db.write("UPDATE jobs SET status = 'done', dedupe_key = NULL, locked_by = NULL, "
                                   "locked_until = NULL, last_error = NULL, updated_at = :t "
                                   "WHERE id = :id AND locked_by = :me AND status = 'running'",
                                   {"t": now(), "id": job.id, "me": self.me}) == 1

    async def fail(self, job: Job, error: str, *, permanent: bool = False) -> str:
        """'queued' (تلاش دوباره با backoff) یا 'dead'."""
        dead = permanent or job.attempts >= job.max_attempts
        if dead:
            await self.db.write("UPDATE jobs SET status = 'dead', dedupe_key = NULL, locked_by = NULL, "
                                "locked_until = NULL, last_error = :e, updated_at = :t "
                                "WHERE id = :id AND locked_by = :me AND status = 'running'",
                                {"e": error[:2000], "t": now(), "id": job.id, "me": self.me})
            return "dead"
        await self.db.write("UPDATE jobs SET status = 'queued', run_at = :r, locked_by = NULL, locked_until = NULL, "
                            "last_error = :e, updated_at = :t WHERE id = :id AND locked_by = :me AND status = 'running'",
                            {"r": later(backoff(job.attempts)), "e": error[:2000], "t": now(), "id": job.id,
                             "me": self.me})
        return "queued"

    async def requeue(self, job: Job, delay: float, *, kind: str | None = None, payload: dict | None = None,
                      reset_attempts: bool = True) -> bool:
        """اجرای دوباره بدون شمردن شکست (مثلاً سفارش هنوز در جریان است). می‌تواند نوع کار را عوض کند."""
        return await self.db.write(
            "UPDATE jobs SET status = 'queued', run_at = :r, kind = :k, payload = :p, locked_by = NULL, "
            "locked_until = NULL, attempts = CASE WHEN :reset = 1 THEN 0 ELSE attempts END, updated_at = :t "
            "WHERE id = :id AND locked_by = :me AND status = 'running'",
            {"r": later(delay), "k": kind or job.kind, "p": json.dumps(payload if payload is not None else job.payload,
                                                                         ensure_ascii=False),
             "reset": int(reset_attempts), "t": now(), "id": job.id, "me": self.me}) == 1

    # ---------- مدیریت ----------
    async def retry_dead(self, job_id: int) -> bool:
        """اجرای دوباره‌ی کار dead از پنل. کلید dedupe دوباره گرفته می‌شود تا با کار فعال دیگری تداخل نکند."""
        row = await self.db.one("SELECT kind, payload FROM jobs WHERE id = :id AND status = 'dead'", {"id": job_id})
        if row is None:
            return False
        dedupe = _dedupe_for(row["kind"], row["payload"])
        try:
            n = await self.db.write("UPDATE jobs SET status = 'queued', attempts = 0, run_at = :t, dedupe_key = :d, "
                                    "last_error = NULL, updated_at = :t WHERE id = :id AND status = 'dead'",
                                    {"t": now(), "d": dedupe, "id": job_id})
        except Exception:
            return False  # کار فعال دیگری برای همین سفارش در صف است
        return n == 1

    async def stats(self) -> dict[str, Any]:
        t = now()
        r = await self.db.one(
            """SELECT
                 (SELECT COUNT(*) FROM jobs WHERE status = 'queued') AS queued,
                 (SELECT COUNT(*) FROM jobs WHERE status = 'queued' AND run_at <= :t) AS ready,
                 (SELECT COUNT(*) FROM jobs WHERE status = 'running') AS running,
                 (SELECT COUNT(*) FROM jobs WHERE status = 'running' AND locked_until < :t) AS stale,
                 (SELECT COUNT(*) FROM jobs WHERE status = 'done') AS done,
                 (SELECT COUNT(*) FROM jobs WHERE status = 'dead') AS dead,
                 (SELECT COUNT(*) FROM jobs WHERE status = 'queued' AND attempts > 0) AS retrying,
                 (SELECT MIN(run_at) FROM jobs WHERE status = 'queued' AND run_at <= :t) AS oldest_ready""",
            {"t": t})
        lag = 0.0
        if r["oldest_ready"]:
            lag = max((_parse(t) - _parse(r["oldest_ready"])).total_seconds(), 0.0)
        by_kind = await self.db.all("SELECT kind, status, COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running', "
                                    "'dead') GROUP BY kind, status ORDER BY kind")
        return {**{k: int(v or 0) for k, v in r.items() if k != "oldest_ready"}, "lag_seconds": lag,
                "by_kind": by_kind}

    async def dead_jobs(self, limit: int = 20, kind_prefix: str | None = None) -> list[dict]:
        if kind_prefix:
            return await self.db.all("SELECT * FROM jobs WHERE status = 'dead' AND kind LIKE :k ORDER BY id DESC "
                                     "LIMIT :l", {"k": kind_prefix + "%", "l": limit})
        return await self.db.all("SELECT * FROM jobs WHERE status = 'dead' ORDER BY id DESC LIMIT :l", {"l": limit})

    async def has_active(self, dedupe_key: str) -> bool:
        return await self.db.one("SELECT 1 AS x FROM jobs WHERE dedupe_key = :d", {"d": dedupe_key}) is not None

    async def purge(self, days: int = 7) -> int:
        from .db import ago_ms
        return await self.db.write("DELETE FROM jobs WHERE status = 'done' AND updated_at < :t",
                                   {"t": ago_ms(days * 86400)})


def _dedupe_for(kind: str, payload_raw: str) -> str | None:
    try:
        p = json.loads(payload_raw or "{}")
    except ValueError:
        return None
    if kind.startswith("order.") and "oid" in p:
        return f"order:{p['oid']}"
    if kind.startswith("broadcast") and "bid" in p:
        return f"broadcast:{p['bid']}"
    return None


def _parse(v: str):
    from datetime import datetime
    return datetime.strptime(v.rstrip("Z")[:23], "%Y-%m-%dT%H:%M:%S.%f" if "." in v else "%Y-%m-%dT%H:%M:%S")
