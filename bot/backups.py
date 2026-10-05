"""مدیریت پشتیبان: ساخت، فهرست، بررسی سلامت، تست بازگردانی، بازگردانی و حذف.

قالب پشتیبان (مستقل از نوع پایگاه داده):
    stard-backup-<زمان>-<نوع>.zip
      manifest.json      نسخه‌ی ربات، revision مهاجرت، تعداد ردیف و SHA-256 هر فایل
      config.json        تنظیمات غیرمحرمانه (secretها هرگز در پشتیبان نیستند)
      tables/<جدول>.jsonl

- همین قالب روی SQLite و PostgreSQL کار می‌کند؛ پس با پشتیبان‌گیری از SQLite و بازگردانی روی PostgreSQL
  می‌شود داده‌ها را به Production منتقل کرد.
- بازگردانی در یک تراکنش انجام می‌شود (همه یا هیچ) و قبلش یک پشتیبان «pre-restore» گرفته می‌شود.
- پشتیبان از revision جدیدتر از کد فعلی بازگردانی نمی‌شود (بررسی سازگاری).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, insert, select, text

from . import __version__
from .db import Database, now
from .migrations_runner import all_revisions, current_revision
from .models import metadata

log = logging.getLogger(__name__)

# جدول‌های موقتی که پشتیبان نمی‌خواهند (بعد از بازگردانی خودکار ساخته می‌شوند)
SKIP_TABLES = {"locks", "rate_limits", "heartbeats", "jobs", "alembic_version"}
NAME_RE = re.compile(r"^stard-backup-\d{8}-\d{6}-[a-z-]+(?:-\d+)?\.zip$")
KINDS = ("manual", "auto", "pre-update", "pre-migration", "pre-restore")
CHUNK = 1000


@dataclass
class BackupInfo:
    name: str
    path: str
    size: int
    created_at: str
    kind: str
    version: str
    revision: str | None
    rows: int


class BackupError(Exception):
    pass


class BackupManager:
    def __init__(self, db: Database, directory: str, settings: Any = None, locks: Any = None):
        self.db = db
        self.dir = directory
        self.settings = settings
        self.locks = locks

    def _path(self, name: str) -> str:
        if not NAME_RE.match(name):
            raise BackupError("invalid backup name")
        return os.path.join(self.dir, name)

    # ---------- ساخت ----------
    async def create(self, kind: str = "manual", *, admin_id: int | None = None) -> BackupInfo:
        if kind not in KINDS:
            raise BackupError("invalid kind")
        os.makedirs(self.dir, exist_ok=True)
        if self.locks is not None and not await self.locks.acquire("backup", 900):
            raise BackupError("پشتیبان‌گیری دیگری در حال اجراست")
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            name = f"stard-backup-{stamp}-{kind}.zip"
            i = 1
            while os.path.exists(os.path.join(self.dir, name)):
                i += 1
                name = f"stard-backup-{stamp}-{kind}-{i}.zip"
            path = os.path.join(self.dir, name)
            tables, files = await self._export()
            manifest = {
                "format": 1, "app": "stard-shop-bot", "version": __version__,
                "revision": await current_revision(self.db), "dialect": self.db.dialect,
                "created_at": now(), "kind": kind, "tables": tables,
                "files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()},
            }
            files["config.json"] = json.dumps(self._config(), ensure_ascii=False, indent=1).encode()
            manifest["files"]["config.json"] = hashlib.sha256(files["config.json"]).hexdigest()
            tmp = path + ".part"
            await asyncio.to_thread(_write_zip, tmp, manifest, files)
            os.replace(tmp, path)  # نوشتن اتمیک: فایل ناقص هیچ‌وقت در فهرست دیده نمی‌شود
            await self.db.audit(admin_id=admin_id, action="backup_create", ref=name,
                                after={"kind": kind, "rows": sum(tables.values()), "size": os.path.getsize(path)})
            log.info("backup created: %s (%s rows)", name, sum(tables.values()))
            return self._info(path, manifest)
        finally:
            if self.locks is not None:
                await self.locks.release("backup")

    async def _export(self) -> tuple[dict[str, int], dict[str, bytes]]:
        counts: dict[str, int] = {}
        files: dict[str, bytes] = {}
        # تراکنش فقط‌خواندنی؛ در PostgreSQL با REPEATABLE READ یک تصویر سازگار از همه‌ی جدول‌ها
        async with self.db.engine.connect() as c:
            if not self.db.is_sqlite:
                await c.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            for table in metadata.sorted_tables:
                if table.name in SKIP_TABLES:
                    continue
                buf = io.StringIO()
                n = 0
                result = await c.stream(select(table))
                async for row in result:
                    buf.write(json.dumps(dict(row._mapping), ensure_ascii=False, default=_json_default))
                    buf.write("\n")
                    n += 1
                counts[table.name] = n
                files[f"tables/{table.name}.jsonl"] = buf.getvalue().encode("utf-8")
            await c.rollback()
        return counts, files

    def _config(self) -> dict:
        """تنظیمات غیرمحرمانه برای بازسازی سرور. secretها فقط با «set/unset» مشخص می‌شوند."""
        if self.settings is None:
            return {}
        out = {}
        for k, v in self.settings.model_dump().items():
            if hasattr(v, "get_secret_value") or k in ("bot_token", "stard_api_key", "database_url", "redis_url",
                                                       "stard_webhook_secret", "github_token"):
                out[k] = "set" if v else "unset"
            else:
                out[k] = v
        return out

    # ---------- فهرست / حذف ----------
    def list(self) -> list[BackupInfo]:
        if not os.path.isdir(self.dir):
            return []
        out = []
        for name in os.listdir(self.dir):
            if not NAME_RE.match(name):
                continue
            path = os.path.join(self.dir, name)
            try:
                with zipfile.ZipFile(path) as z:
                    manifest = json.loads(z.read("manifest.json"))
                out.append(self._info(path, manifest))
            except (zipfile.BadZipFile, KeyError, ValueError, OSError):
                out.append(BackupInfo(name, path, _size(path), "?", "corrupt", "?", None, 0))
        return sorted(out, key=lambda b: b.name, reverse=True)

    def _info(self, path: str, m: dict) -> BackupInfo:
        return BackupInfo(os.path.basename(path), path, _size(path), m.get("created_at", "?"), m.get("kind", "?"),
                          m.get("version", "?"), m.get("revision"), sum((m.get("tables") or {}).values()))

    async def delete(self, name: str, *, admin_id: int | None = None) -> None:
        path = self._path(name)
        if not os.path.exists(path):
            raise BackupError("پیدا نشد")
        os.remove(path)
        await self.db.audit(admin_id=admin_id, action="backup_delete", ref=name)

    async def rotate(self, keep: int, kinds: tuple[str, ...] = ("auto", "pre-update", "pre-migration",
                                                                 "pre-restore")) -> int:
        """پشتیبان‌های خودکار قدیمی (بیش از keep تا از هر نوع) حذف می‌شوند؛ پشتیبان دستی دست نمی‌خورد."""
        removed = 0
        for kind in kinds:
            items = [b for b in self.list() if b.kind == kind]
            for b in items[keep:]:
                os.remove(b.path)
                removed += 1
        return removed

    # ---------- بررسی ----------
    def verify(self, name: str) -> dict:
        """بررسی سالم بودن: zip، checksum همه‌ی فایل‌ها، JSON معتبر و تعداد ردیف‌ها."""
        path = self._path(name)
        try:
            with zipfile.ZipFile(path) as z:
                bad = z.testzip()
                if bad:
                    return {"ok": False, "error": f"فایل خراب در آرشیو: {bad}"}
                m = json.loads(z.read("manifest.json"))
                for fname, digest in (m.get("files") or {}).items():
                    data = z.read(fname)
                    if hashlib.sha256(data).hexdigest() != digest:
                        return {"ok": False, "error": f"checksum نادرست: {fname}"}
                for table, n in (m.get("tables") or {}).items():
                    lines = z.read(f"tables/{table}.jsonl").decode("utf-8").splitlines()
                    if len(lines) != n:
                        return {"ok": False, "error": f"تعداد ردیف {table}: {len(lines)} ≠ {n}"}
                    for line in lines[:50]:
                        json.loads(line)
        except (zipfile.BadZipFile, KeyError, ValueError, OSError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
        compat = self._compatible(m.get("revision"))
        return {"ok": compat is None, "error": compat, "tables": m.get("tables"), "version": m.get("version"),
                "revision": m.get("revision"), "rows": sum((m.get("tables") or {}).values())}

    def _compatible(self, revision: str | None) -> str | None:
        revs = [r for r, _ in all_revisions()]
        if revision is not None and revision not in revs:
            return f"این پشتیبان از نسخه‌ی جدیدتر ({revision}) است و با کد فعلی سازگار نیست"
        return None

    async def restore_test(self, name: str) -> dict:
        """بازگردانی آزمایشی در یک پایگاه داده‌ی SQLite موقت و مقایسه‌ی تعداد ردیف‌ها (بدون دست زدن به داده‌ی اصلی)."""
        v = self.verify(name)
        if not v["ok"]:
            return v
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="restore-test-")
        os.close(fd)
        os.remove(tmp)
        scratch = Database(tmp)
        try:
            await scratch.connect()
            counts = await _import(scratch, self._path(name))
            mismatch = {t: (counts.get(t), n) for t, n in v["tables"].items() if counts.get(t) != n}
            if mismatch:
                return {"ok": False, "error": f"عدم تطابق ردیف‌ها: {mismatch}"}
            return {"ok": True, "rows": sum(counts.values()), "tables": counts}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            await scratch.close()
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(tmp + suffix):
                    os.remove(tmp + suffix)

    # ---------- بازگردانی ----------
    async def restore(self, name: str, *, admin_id: int | None = None) -> dict:
        v = self.verify(name)
        if not v["ok"]:
            raise BackupError(v["error"] or "پشتیبان معتبر نیست")
        if self.locks is not None and not await self.locks.acquire("restore", 1800):
            raise BackupError("بازگردانی دیگری در حال اجراست")
        try:
            safety = await self.create("pre-restore", admin_id=admin_id)
            counts = await _import(self.db, self._path(name))
            self.db.invalidate_settings()
            await self.db.audit(admin_id=admin_id, action="backup_restore", ref=name,
                                after={"rows": sum(counts.values()), "safety_backup": safety.name})
            log.warning("database restored from %s (safety backup: %s)", name, safety.name)
            return {"ok": True, "rows": sum(counts.values()), "safety_backup": safety.name}
        finally:
            if self.locks is not None:
                await self.locks.release("restore")


async def _import(db: Database, path: str) -> dict[str, int]:
    """همه‌ی جدول‌ها را در یک تراکنش پاک و از پشتیبان پر می‌کند."""
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        data = {t: z.read(f"tables/{t}.jsonl").decode("utf-8") for t in manifest["tables"]}
    tables = [t for t in metadata.sorted_tables if t.name not in SKIP_TABLES]
    counts: dict[str, int] = {}
    async with db.tx() as c:
        for table in reversed(tables):
            await c.execute(delete(table))
        for table in tables:
            raw = data.get(table.name, "")
            cols = {col.name for col in table.columns}
            rows = [{k: v for k, v in json.loads(line).items() if k in cols} for line in raw.splitlines() if line]
            for i in range(0, len(rows), CHUNK):
                await c.execute(insert(table), rows[i:i + CHUNK])
            counts[table.name] = len(rows)
        await c.execute(text("DELETE FROM jobs WHERE status IN ('queued', 'running')"))
        if not db.is_sqlite:
            # شمارنده‌های id بعد از درج صریح شناسه‌ها باید جلو بروند
            for table in tables:
                pk = [col for col in table.primary_key.columns]
                if len(pk) == 1 and pk[0].autoincrement is True:
                    await c.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', '{pk[0].name}'), "
                        f"COALESCE((SELECT MAX({pk[0].name}) FROM {table.name}), 0) + 1, false)"))
    return counts


def _write_zip(path: str, manifest: dict, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        for name, data in files.items():
            z.writestr(name, data)


def _json_default(v: Any):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return str(v)


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ---------- پشتیبان خودکار (کار دوره‌ای؛ فقط یک نمونه) ----------
def _register_periodic() -> None:
    from .worker import periodic

    @periodic("auto_backup", 600)
    async def auto_backup(ctx) -> None:
        s = ctx.settings
        if s is None or not s.backup_interval_hours:
            return
        mgr = BackupManager(ctx.db, s.backup_dir, s, ctx.locks)
        last = next((b for b in mgr.list() if b.kind == "auto"), None)
        if last is not None:
            try:
                made = datetime.strptime(last.created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                made = None
            if made and (datetime.now(timezone.utc) - made).total_seconds() < s.backup_interval_hours * 3600:
                return
        info = await mgr.create("auto")
        check = mgr.verify(info.name)
        if not check["ok"]:
            log.error("automatic backup %s failed verification: %s", info.name, check["error"])
            from . import notify
            await notify.to_admins(ctx.bot, ctx.admins, f"🚨 پشتیبان خودکار {info.name} سالم نیست: {check['error']}")
        await mgr.rotate(s.backup_keep)


_register_periodic()
