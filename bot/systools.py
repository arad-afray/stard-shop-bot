"""ابزارهای سیستمی پنل: اسکنر امنیتی، بررسی وابستگی‌ها، آمار پایگاه داده و عیب‌یابی API."""
from __future__ import annotations

import asyncio
import importlib.metadata as md
import json
import os
import re
import shutil
import subprocess
import stat
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .db import Database
from .logging_setup import _PATTERNS, LOG_FILE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEVERITY = {"critical": "🔴 بحرانی", "high": "🟠 زیاد", "medium": "🟡 متوسط", "low": "🔵 کم"}
SEV_ORDER = ["critical", "high", "medium", "low"]


@dataclass
class Finding:
    severity: str
    title: str
    detail: str = ""


# ---------- اسکنر امنیتی ----------
async def security_scan(settings: Any, db: Database, *, run_pip_audit: bool = True) -> list[Finding]:
    f: list[Finding] = []
    s = settings

    # پیکربندی
    if not s.admin_ids:
        f.append(Finding("critical", "ADMIN_IDS خالی است", "هیچ مالکی تعریف نشده؛ پنل مدیریت در دسترس نیست."))
    if not re.match(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$", s.bot_token.get_secret_value()):
        f.append(Finding("high", "قالب BOT_TOKEN نامعتبر است"))
    key = s.stard_api_key.get_secret_value()
    if not key.startswith(("sk_live_", "sk_test_")):
        f.append(Finding("high", "قالب STARD_API_KEY نامعتبر است"))
    if s.log_level.upper() == "DEBUG":
        f.append(Finding("medium", "حالت اشکال‌زدایی (Debug) روشن است", "LOG_LEVEL=DEBUG لاگ زیادی تولید می‌کند؛ در Production INFO بگذارید."))
    if s.role == "bot" and not s.redis_url:
        f.append(Finding("high", "ROLE=bot بدون Redis", "با چند نمونه‌ی ربات، حالت خرید (FSM) بین نمونه‌ها مشترک نیست."))
    if not s.database_url and key.startswith("sk_live_"):
        f.append(Finding("medium", "نسخه‌ی اصلی روی SQLite", "برای مقیاس بالا DATABASE_URL (PostgreSQL) را تنظیم کنید."))
    loopback = s.http_host in ("127.0.0.1", "localhost", "::1")
    if s.http_enabled and not loopback:
        keys = await db.scalar("SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL")
        f.append(Finding("high" if not s.stard_webhook_secret else "medium", "سرور HTTP روی شبکه باز است",
                         f"HTTP_HOST={s.http_host}. فقط پشت reverse proxy با HTTPS قرار دهید؛ /metrics و /api فقط با کلید "
                         f"API ({keys} کلید فعال) در دسترس‌اند."))
    if s.stard_webhook_secret is None:
        f.append(Finding("low", "وب‌هوک Stard غیرفعال است", "STARD_WEBHOOK_SECRET تنظیم نشده؛ وضعیت سفارش‌ها فقط با polling."))
    elif len(s.stard_webhook_secret.get_secret_value()) < 16:
        f.append(Finding("high", "STARD_WEBHOOK_SECRET کوتاه است"))
    if s.database_url and "sslmode" not in s.database_url.get_secret_value() and \
            not re.search(r"@(localhost|127\.0\.0\.1|postgres|db)[:/]", s.database_url.get_secret_value()):
        f.append(Finding("medium", "اتصال PostgreSQL بدون SSL", "برای پایگاه داده‌ی راه‌دور ?ssl=require اضافه کنید."))

    # دسترسی فایل‌ها (فقط POSIX)
    if os.name == "posix":
        for path, label in ((".env", "فایل .env"), (s.backup_dir, "پوشه‌ی پشتیبان"),
                            (os.path.dirname(os.path.abspath(s.database_path)), "پوشه‌ی داده")):
            full = path if os.path.isabs(path) else os.path.join(os.getcwd(), path)
            if os.path.exists(full):
                mode = os.stat(full).st_mode
                if mode & stat.S_IROTH:
                    f.append(Finding("high" if label == "فایل .env" else "medium", f"{label} برای همه قابل خواندن است",
                                     f"chmod {'600' if os.path.isfile(full) else '700'} {path}"))

    # secret در کد
    leaked = await asyncio.to_thread(_scan_tree_for_secrets, ROOT)
    for p in leaked[:5]:
        f.append(Finding("critical", "secret در سورس‌کد", p))

    # secret در لاگ
    log_path = os.path.join(s.log_dir, LOG_FILE)
    if os.path.exists(log_path):
        hits = await asyncio.to_thread(_scan_file, log_path, s.secret_values())
        if hits:
            f.append(Finding("critical", "secret در فایل لاگ", f"{hits} مورد در {log_path}"))

    # وابستگی‌های آسیب‌پذیر
    if run_pip_audit:
        f += await _pip_audit()
    return sorted(f, key=lambda x: SEV_ORDER.index(x.severity))


def _scan_tree_for_secrets(root: str) -> list[str]:
    out = []
    skip_dirs = {".git", "data", "logs", "backups", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if name == ".env" or not name.endswith((".py", ".md", ".txt", ".yml", ".yaml", ".ini", ".json", ".toml",
                                                    ".example", ".ps1", ".sh")):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                continue
            for p in _PATTERNS[:5]:  # الگوهای توکن/کلید (نه الگوهای عمومی مثل Bearer)
                for m in p.finditer(text):
                    val = m.group(0)
                    if any(x in val for x in ("xxxx", "ABC", "fake", "test", "example", "your", "AAH")) or "tests/" in path:
                        continue
                    out.append(f"{os.path.relpath(path, root)}: {val[:6]}…")
    return out


def _scan_file(path: str, secrets: list[str]) -> int:
    n = 0
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if any(sec in line for sec in secrets if len(sec) >= 8):
                    n += 1
    except OSError:
        pass
    return n


async def _pip_audit() -> list[Finding]:
    exe = shutil.which("pip-audit")
    if exe is None:
        return [Finding("low", "بررسی آسیب‌پذیری وابستگی‌ها انجام نشد",
                        "pip-audit نصب نیست: python -m pip install pip-audit")]
    def _run() -> bytes:
        return subprocess.run([exe, "-r", os.path.join(ROOT, "requirements.txt"), "-f", "json"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                              timeout=180).stdout
    try:
        # در thread: روی ویندوز (WindowsSelectorEventLoop) زیرپردازه‌ی asyncio پشتیبانی نمی‌شود
        out = await asyncio.to_thread(_run)
        data = json.loads(out or b"{}")
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        return [Finding("low", "pip-audit اجرا نشد", type(e).__name__)]
    deps = data.get("dependencies", data if isinstance(data, list) else [])
    out_f = []
    for d in deps:
        for v in d.get("vulns", []):
            out_f.append(Finding("high", f"آسیب‌پذیری {d.get('name')} {d.get('version')}",
                                 f"{v.get('id')} — اصلاح در {', '.join(v.get('fix_versions') or ['?'])}"))
    return out_f


# ---------- وابستگی‌ها ----------
PACKAGES = ["aiogram", "aiosqlite", "httpx", "pydantic-settings", "SQLAlchemy", "asyncpg", "alembic", "redis",
            "psutil", "openpyxl", "fpdf2", "aiohttp"]


async def dependency_report(*, check_latest: bool = True) -> dict[str, Any]:
    pkgs = []
    latest: dict[str, str | None] = {}
    if check_latest:
        async with httpx.AsyncClient(timeout=10) as c:
            async def one(name):
                try:
                    r = await c.get(f"https://pypi.org/pypi/{name}/json")
                    latest[name] = r.json()["info"]["version"] if r.status_code == 200 else None
                except (httpx.HTTPError, ValueError, KeyError):
                    latest[name] = None
            await asyncio.gather(*(one(n) for n in PACKAGES))
    for name in PACKAGES:
        try:
            cur = md.version(name)
        except md.PackageNotFoundError:
            cur = None
        lat = latest.get(name)
        pkgs.append({"name": name, "current": cur, "latest": lat,
                     "status": "missing" if cur is None else ("ok" if not lat or lat == cur else "outdated")})
    docker_base = None
    try:
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as fh:
            docker_base = next((ln.split()[1] for ln in fh if ln.upper().startswith("FROM ")), None)
    except OSError:
        pass
    system = {tool: bool(shutil.which(tool)) for tool in ("git", "pg_dump", "docker", "pip-audit")}
    return {"python": sys.version.split()[0], "python_ok": sys.version_info >= (3, 11), "packages": pkgs,
            "docker_base": docker_base, "node": "در این پروژه استفاده نمی‌شود", "system": system}


# ---------- آمار پایگاه داده ----------
async def db_stats(db: Database) -> dict[str, Any]:
    from .models import metadata
    tables = {}
    for t in metadata.sorted_tables:
        try:
            tables[t.name] = {"rows": await db.scalar(f"SELECT COUNT(*) FROM {t.name}")}
        except Exception:
            tables[t.name] = {"rows": None}
    size = None
    if db.is_sqlite:
        try:
            size = sum(os.path.getsize(db.path + s) for s in ("", "-wal") if os.path.exists(db.path + s))
            for r in await db.all("SELECT name, SUM(pgsize) AS bytes FROM dbstat GROUP BY name"):
                if r["name"] in tables:
                    tables[r["name"]]["bytes"] = r["bytes"]
        except Exception:
            pass  # dbstat در همه‌ی نسخه‌های SQLite فعال نیست
    else:
        size = await db.scalar("SELECT pg_database_size(current_database())")
        for r in await db.all("SELECT relname AS name, pg_total_relation_size(relid) AS bytes "
                              "FROM pg_catalog.pg_statio_user_tables"):
            if r["name"] in tables:
                tables[r["name"]]["bytes"] = r["bytes"]
    async with db.engine.connect() as c:
        from sqlalchemy import inspect
        indexes = await c.run_sync(lambda sc: {t: [i["name"] for i in inspect(sc).get_indexes(t)]
                                                for t in inspect(sc).get_table_names()})
    summary = await db.one(
        """SELECT (SELECT COUNT(*) FROM users) AS users, (SELECT COUNT(*) FROM orders) AS orders,
                  (SELECT COUNT(*) FROM ledger) AS ledger, (SELECT COUNT(*) FROM topups) AS topups,
                  (SELECT COUNT(*) FROM orders WHERE refunded = 1) AS failed_orders,
                  (SELECT COUNT(*) FROM orders WHERE status IN ('new', 'pending', 'processing', 'manual')) AS active_orders""")
    return {"dialect": db.dialect, "size": size, "tables": tables, "indexes": indexes, **summary}


# ---------- عیب‌یابی API ----------
async def api_diagnostics(api: Any, metrics: Any = None) -> list[tuple[str, str, str]]:
    """[(بررسی، وضعیت ok|warn|fail، توضیح)]"""
    out: list[tuple[str, str, str]] = []
    t = time.perf_counter()
    try:
        ping = await api.ping()
        ms = (time.perf_counter() - t) * 1000
        out.append(("Connection", "ok", f"{ms:.0f}ms"))
        key = ping.get("key") or {}
        out.append(("Authentication", "ok", f"{ping.get('environment')} | {key.get('display', '')} | "
                                            f"scopes: {', '.join(key.get('scopes', []))}"))
        out.append(("Latency", "ok" if ms < 1500 else "warn", f"{ms:.0f}ms"))
    except Exception as e:
        code = getattr(e, "code", type(e).__name__)
        status = getattr(e, "status", 0)
        out.append(("Connection", "fail" if status in (0,) or status >= 500 else "ok", f"{status} {code}"))
        out.append(("Authentication", "fail" if status in (401, 403) else "warn", f"{status} {code}"))
    if metrics is not None:
        p50, p95 = metrics.api_latency.pct(50), metrics.api_latency.pct(95)
        if p95 is not None:
            out.append(("تأخیر (۵ دقیقه)", "ok" if p95 < 3 else "warn", f"p50 {p50 * 1000:.0f}ms | p95 {p95 * 1000:.0f}ms"))
        rl = metrics.rate_limit
        if rl:
            out.append(("محدودیت درخواست", "ok" if int(rl.get("x-ratelimit-remaining", 1) or 1) > 5 else "warn",
                        f"{rl.get('x-ratelimit-remaining', '?')}/{rl.get('x-ratelimit-limit', '?')} باقی‌مانده"))
        errs = [e for e in metrics.api_errors if time.time() - e[0] < 3600]
        out.append(("خطاها (۱ ساعت)", "ok" if not errs else ("warn" if len(errs) < 10 else "fail"),
                    f"{len(errs)}" + (f" — آخرین: {errs[-1][1]}" if errs else "")))
    try:
        ver = await api.openapi_version()
        out.append(("نسخه‌ی API", "ok" if ver else "warn", ver or "نامشخص"))
    except Exception:
        out.append(("نسخه‌ی API", "warn", "نامشخص"))
    return out
