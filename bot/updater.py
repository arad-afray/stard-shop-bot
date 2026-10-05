"""به‌روزرسانی از GitHub Releases با پشتیبان، اعتبارسنجی، مهاجرت، Health Check و Rollback.

حالت‌های نصب:
- git      (git clone): تگ رسمی Release گرفته و اعتبارسنجی می‌شود (commit تگ = commit اعلام‌شده در GitHub)
- archive  (بدون git): فایل zip ضمیمه‌ی Release با SHA256SUMS همان Release بررسی می‌شود
- docker   : به‌روزرسانی درجا ممنوع است (image تغییرناپذیر است)؛ دستور به‌روزرسانی Docker نمایش داده می‌شود

مراحل apply (هر مرحله در وضعیت به‌روزرسانی ثبت و در پنل نمایش داده می‌شود):
  1 قفل  2 پیش‌بررسی و اعتبارسنجی  3 پشتیبان pre-update + verify  4 ذخیره‌ی نقطه‌ی بازگشت کد
  5 تعویض کد  6 نصب وابستگی‌ها  7 مهاجرت پایگاه داده  8 Health Check کد جدید (پردازه‌ی جدا)  9 Restart
اگر هر مرحله‌ای شکست بخورد: downgrade شِما با کد جدید → بازگردانی داده از پشتیبان → برگشت کد → نصب
وابستگی‌های قبلی. نتیجه در Audit Log ثبت می‌شود. بعد از Restart، پردازه‌ی جدید Health Check نهایی را انجام
می‌دهد؛ اگر کد جدید اصلاً بالا نیاید، run.ps1/run.sh با فایل data/update-pending.json به commit قبلی برمی‌گردند.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from . import __version__
from .db import Database, now

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
# فایل‌ها/پوشه‌هایی که هنگام به‌روزرسانی هرگز دست نمی‌خورند
PRESERVE = {".env", "data", "logs", "backups", ".git", ".venv", "venv", "__pycache__"}
PENDING_FILE = "update-pending.json"


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    m = VERSION_RE.match((v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


@dataclass
class ReleaseInfo:
    current: str
    latest: str | None
    tag: str | None
    newer: bool
    changelog: str = ""
    published_at: str = ""
    url: str = ""
    assets: dict[str, str] = field(default_factory=dict)  # نام → آدرس دانلود
    error: str | None = None


@dataclass
class UpdateState:
    status: str = "idle"           # idle | running | restarting | success | failed | rolled_back
    target: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    steps: list[list[str]] = field(default_factory=list)  # [نام، ok|fail|run|skip، توضیح]
    error: str | None = None
    backup: str | None = None
    migration_required: bool | None = None


class UpdateError(Exception):
    pass


Runner = Callable[..., Awaitable[tuple[int, str]]]


async def run_cmd(*args: str, cwd: str = ROOT, timeout: float = 900, env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace")[-4000:]


def install_mode(root: str = ROOT) -> str:
    if os.path.exists("/.dockerenv") or os.environ.get("STARD_DOCKER") == "1":
        return "docker"
    if os.path.isdir(os.path.join(root, ".git")) and shutil.which("git"):
        return "git"
    return "archive"


class Updater:
    def __init__(self, settings: Any, db: Database, backups: Any, locks: Any, *, root: str = ROOT,
                 runner: Runner = run_cmd, http: httpx.AsyncClient | None = None,
                 data_dir: str | None = None):
        self.settings = settings
        self.db = db
        self.backups = backups
        self.locks = locks
        self.root = root
        self.run = runner
        self._http = http
        self.data_dir = data_dir or os.path.join(root, "data")
        self.state = UpdateState()
        self._lock = asyncio.Lock()

    # ---------- GitHub ----------
    def _client(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        headers = {"Accept": "application/vnd.github+json", "User-Agent": f"stard-shop-bot/{__version__}"}
        tok = getattr(self.settings, "github_token", None)
        if tok:
            headers["Authorization"] = f"Bearer {tok.get_secret_value()}"
        return httpx.AsyncClient(base_url="https://api.github.com", headers=headers, timeout=30,
                                 follow_redirects=True)

    async def _gh(self, path: str) -> Any:
        c = self._client()
        try:
            r = await c.get(path)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        finally:
            if c is not self._http:
                await c.aclose()

    async def check(self) -> ReleaseInfo:
        """فقط آخرین Release رسمی (نه draft و نه pre-release) بررسی می‌شود."""
        info = ReleaseInfo(current=__version__, latest=None, tag=None, newer=False)
        try:
            rel = await self._gh(f"/repos/{self.settings.update_repo}/releases/latest")
        except httpx.HTTPError as e:
            info.error = f"GitHub در دسترس نیست: {type(e).__name__}"
            return info
        if not rel:
            info.error = "هنوز هیچ Release رسمی منتشر نشده است"
            return info
        if rel.get("draft") or rel.get("prerelease"):
            info.error = "آخرین Release رسمی نیست"
            return info
        tag = rel.get("tag_name") or ""
        latest = parse_version(tag)
        if latest is None:
            info.error = f"تگ Release نامعتبر است: {tag}"
            return info
        info.tag, info.latest = tag, ".".join(map(str, latest))
        info.newer = latest > (parse_version(__version__) or (0, 0, 0))
        info.changelog = (rel.get("body") or "").strip()
        info.published_at = (rel.get("published_at") or "")[:19].replace("T", " ")
        info.url = rel.get("html_url") or ""
        info.assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets") or []
                       if a.get("name") and a.get("browser_download_url")}
        await self.db.set_json("update:last_check", {**asdict(info), "checked_at": now()})
        return info

    # ---------- وضعیت ----------
    def _step(self, name: str, status: str, detail: str = "") -> None:
        for s in self.state.steps:
            if s[0] == name:
                s[1], s[2] = status, detail[:300]
                break
        else:
            self.state.steps.append([name, status, detail[:300]])

    async def _save(self) -> None:
        try:
            await self.db.set_json("update:state", asdict(self.state))
        except Exception:
            log.warning("could not persist update state")

    async def load_state(self) -> UpdateState:
        raw = await self.db.get_json("update:state")
        if raw:
            self.state = UpdateState(**{k: v for k, v in raw.items() if k in UpdateState.__dataclass_fields__})
        return self.state

    # ---------- اعمال ----------
    async def apply(self, release: ReleaseInfo, *, admin_id: int | None = None,
                    on_progress: Callable[[UpdateState], Awaitable[None]] | None = None,
                    request_restart: Callable[[], Any] | None = None) -> UpdateState:
        if self._lock.locked():
            raise UpdateError("به‌روزرسانی دیگری در همین نمونه در حال اجراست")
        async with self._lock:
            if not await self.locks.acquire("update", 3600):
                raise UpdateError("به‌روزرسانی دیگری (در نمونه‌ای دیگر) در حال اجراست")
            try:
                return await self._apply(release, admin_id, on_progress, request_restart)
            finally:
                await self.locks.release("update")

    async def _apply(self, release, admin_id, on_progress, request_restart) -> UpdateState:
        self.state = UpdateState(status="running", target=release.latest, started_at=now())

        async def progress(name: str, status: str, detail: str = "") -> None:
            self._step(name, status, detail)
            await self._save()
            if on_progress is not None:
                try:
                    await on_progress(self.state)
                except Exception:
                    pass

        mode = install_mode(self.root)
        rollback_point: dict[str, Any] = {"mode": mode}
        migrated = False
        try:
            await progress("پیش‌بررسی", "run")
            if mode == "docker":
                raise UpdateError("در Docker به‌روزرسانی درجا ممکن نیست. روی سرور اجرا کنید: "
                                  "git pull && docker compose up -d --build")
            if not release.newer or not release.tag:
                raise UpdateError("نسخه‌ی جدیدتری وجود ندارد")
            if shutil.disk_usage(self.root).free < 300 * 1024 * 1024:
                raise UpdateError("فضای دیسک کمتر از ۳۰۰ مگابایت است")
            new_revs, min_py = await (self._validate_git(release) if mode == "git" else self._validate_archive(release))
            if min_py and sys.version_info[:2] < min_py:
                raise UpdateError(f"این نسخه Python {'.'.join(map(str, min_py))} یا بالاتر لازم دارد")
            from .migrations_runner import current_revision
            cur = await current_revision(self.db)
            if cur and cur not in new_revs:
                raise UpdateError(f"تاریخچه‌ی مهاجرت نسخه‌ی جدید با پایگاه داده (revision {cur}) سازگار نیست")
            from .migrations_runner import all_revisions
            local = {r for r, _ in all_revisions()}
            self.state.migration_required = bool(set(new_revs) - local)
            await progress("پیش‌بررسی", "ok", f"حالت نصب: {mode} | مهاجرت لازم: "
                                              f"{'بله' if self.state.migration_required else 'خیر'}")

            await progress("پشتیبان", "run")
            b = await self.backups.create("pre-update", admin_id=admin_id)
            v = self.backups.verify(b.name)
            if not v["ok"]:
                raise UpdateError(f"پشتیبان سالم نیست: {v['error']}")
            self.state.backup = b.name
            rollback_point["revision"] = cur
            await progress("پشتیبان", "ok", b.name)

            await progress("تعویض کد", "run")
            if mode == "git":
                rc, out = await self.run("git", "rev-parse", "HEAD", cwd=self.root)
                if rc != 0:
                    raise UpdateError("commit فعلی خوانده نشد")
                rollback_point["commit"] = out.strip().splitlines()[-1]
                rc, out = await self.run("git", "checkout", "--force", "--detach", release.tag, cwd=self.root)
                if rc != 0:
                    raise UpdateError(f"git checkout ناموفق: {out[-200:]}")
            else:
                rollback_point["snapshot"] = await asyncio.to_thread(self._snapshot_code)
                await asyncio.to_thread(self._extract, self._archive)
            self._write_pending({"from_version": __version__, "to_version": release.latest,
                                 "from_commit": rollback_point.get("commit"), "backup": b.name, "at": now()})
            await progress("تعویض کد", "ok", release.tag)

            await progress("نصب وابستگی‌ها", "run")
            rc, out = await self.run(sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                     "-r", "requirements.txt", cwd=self.root, timeout=900)
            if rc != 0:
                raise UpdateError(f"pip install ناموفق: {out[-300:]}")
            await progress("نصب وابستگی‌ها", "ok")

            await progress("مهاجرت پایگاه داده", "run")
            migrated = True
            rc, out = await self.run(sys.executable, "-m", "bot.migrate", cwd=self.root, timeout=1800)
            if rc != 0:
                raise UpdateError(f"مهاجرت ناموفق: {out[-300:]}")
            await progress("مهاجرت پایگاه داده", "ok", out.strip().splitlines()[-1] if out.strip() else "")

            await progress("Health Check", "run")
            rc, out = await self.run(sys.executable, "-m", "bot.selfcheck", cwd=self.root, timeout=300)
            if rc != 0:
                raise UpdateError(f"Health Check کد جدید ناموفق: {out[-300:]}")
            await progress("Health Check", "ok", out.strip().splitlines()[-1] if out.strip() else "")

            self.state.status = "restarting"
            await progress("Restart", "run", "ربات با نسخه‌ی جدید دوباره اجرا می‌شود…")
            await self.db.set_json("update:pending", {"from_version": __version__, "to_version": release.latest,
                                                      "backup": b.name, "admin_id": admin_id, "at": now()})
            await self.db.audit(admin_id=admin_id, action="update_apply", ref=release.tag,
                                before={"version": __version__}, after={"version": release.latest, "backup": b.name})
            if request_restart is not None:
                request_restart()
            return self.state
        except Exception as e:
            err = str(e) if isinstance(e, UpdateError) else f"{type(e).__name__}: {e}"
            log.error("update to %s failed: %s", release.tag, err)
            self.state.error = err[:500]
            for s in self.state.steps:
                if s[1] == "run":
                    s[1], s[2] = "fail", err[:300]
            rolled = await self._rollback(rollback_point, migrated, progress)
            self.state.status = "rolled_back" if rolled else "failed"
            self.state.finished_at = now()
            await self._save()
            await self.db.audit(admin_id=admin_id, action="update_failed", ref=release.tag, reason=err[:500],
                                after={"rolled_back": rolled})
            return self.state

    async def _rollback(self, point: dict, migrated: bool, progress) -> bool:
        if "commit" not in point and "snapshot" not in point:
            self._clear_pending()
            return True  # هنوز چیزی تغییر نکرده بود
        await progress("Rollback", "run")
        ok = True
        if migrated and point.get("revision"):
            # شِما را با کد جدید (که اسکریپت downgrade را دارد) به revision قبلی برگردان
            rc, out = await self.run(sys.executable, "-m", "bot.migrate", "--to", point["revision"], cwd=self.root,
                                     timeout=1800)
            if rc != 0:
                ok = False
                log.error("schema downgrade failed: %s", out[-300:])
        if point["mode"] == "git":
            rc, out = await self.run("git", "checkout", "--force", "--detach", point["commit"], cwd=self.root)
            ok = ok and rc == 0
        else:
            await asyncio.to_thread(self._extract, point["snapshot"])
        rc, _ = await self.run(sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                               "-r", "requirements.txt", cwd=self.root, timeout=900)
        ok = ok and rc == 0
        if migrated and self.state.backup:
            try:
                await self.backups.restore(self.state.backup)
            except Exception as e:
                ok = False
                log.error("restore after failed update failed: %s", e)
        self._clear_pending()
        await progress("Rollback", "ok" if ok else "fail",
                       "به نسخه‌ی قبلی برگشت" if ok else "برگشت کامل نشد؛ پشتیبان را دستی بازگردانی کنید")
        return ok

    # ---------- اعتبارسنجی ----------
    async def _validate_git(self, release: ReleaseInfo) -> tuple[list[str], tuple[int, int] | None]:
        tag = release.tag
        rc, out = await self.run("git", "fetch", "--force", "origin", f"refs/tags/{tag}:refs/tags/{tag}",
                                 cwd=self.root, timeout=300)
        if rc != 0:
            raise UpdateError(f"دریافت تگ {tag} ناموفق: {out[-200:]}")
        rc, out = await self.run("git", "rev-list", "-n", "1", tag, cwd=self.root)
        local_sha = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
        remote = await self._gh(f"/repos/{self.settings.update_repo}/commits/{tag}")
        if not remote or remote.get("sha") != local_sha:
            raise UpdateError("اعتبارسنجی ناموفق: commit تگ با GitHub یکی نیست")
        rc, init = await self.run("git", "show", f"{tag}:bot/__init__.py", cwd=self.root)
        if rc != 0 or f'__version__ = "{release.latest}"' not in init:
            raise UpdateError("اعتبارسنجی ناموفق: نسخه‌ی داخل کد با تگ Release یکی نیست")
        rc, files = await self.run("git", "ls-tree", "--name-only", tag, "bot/migrations/versions/", cwd=self.root)
        return _revisions_from_names(files.splitlines()), _min_python(init)

    async def _validate_archive(self, release: ReleaseInfo) -> tuple[list[str], tuple[int, int] | None]:
        name = f"stard-shop-bot-{release.tag}.zip"
        if name not in release.assets or "SHA256SUMS" not in release.assets:
            raise UpdateError(f"Release فایل {name} و SHA256SUMS ندارد؛ بدون git فقط Release امضاشده نصب می‌شود")
        c = self._client()
        try:
            sums = (await c.get(release.assets["SHA256SUMS"])).text
            fd, path = tempfile.mkstemp(suffix=".zip", prefix="stard-update-")
            os.close(fd)
            h = hashlib.sha256()
            async with c.stream("GET", release.assets[name]) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
                        h.update(chunk)
        finally:
            if c is not self._http:
                await c.aclose()
        expected = next((ln.split()[0] for ln in sums.splitlines() if ln.strip().endswith(name)), None)
        if not expected or expected.lower() != h.hexdigest():
            os.remove(path)
            raise UpdateError("اعتبارسنجی ناموفق: SHA-256 فایل Release نادرست است")
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            init_name = next((n for n in names if n.endswith("bot/__init__.py")), None)
            if init_name is None or any(n.startswith("/") or ".." in n.split("/") for n in names):
                raise UpdateError("ساختار فایل Release نامعتبر است")
            init = z.read(init_name).decode()
        if f'__version__ = "{release.latest}"' not in init:
            raise UpdateError("نسخه‌ی داخل فایل Release با تگ یکی نیست")
        self._archive_path = path
        point_names = [n.rsplit("/", 1)[-1] for n in names if "bot/migrations/versions/" in n]
        return _revisions_from_names(point_names), _min_python(init)

    # ---------- کد (حالت archive) ----------
    def _snapshot_code(self) -> str:
        fd, path = tempfile.mkstemp(suffix=".zip", prefix="stard-code-")
        os.close(fd)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for base, dirs, files in os.walk(self.root):
                rel = os.path.relpath(base, self.root)
                dirs[:] = [d for d in dirs if d not in PRESERVE and not (rel == "." and d in PRESERVE)]
                for f in files:
                    if rel == "." and f in PRESERVE:
                        continue
                    full = os.path.join(base, f)
                    z.write(full, os.path.relpath(full, self.root))
        return path

    def _extract(self, archive: str) -> None:
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            # zip ضمیمه‌ی Release معمولاً یک پوشه‌ی بالایی دارد (stard-shop-bot-v3.0.0/…)؛ آن را حذف می‌کنیم
            tops = {n.split("/", 1)[0] for n in names}
            top = next(iter(tops)) if len(tops) == 1 else ""
            prefix = f"{top}/" if top and top != "bot" and all("/" in n for n in names) else ""
            for n in names:
                rel = n[len(prefix):] if prefix and n.startswith(prefix) else n
                if not rel or rel.endswith("/") or rel.split("/")[0] in PRESERVE:
                    continue
                dest = os.path.join(self.root, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)

    @property
    def _archive(self) -> str:
        return getattr(self, "_archive_path", "")

    def _write_pending(self, data: dict) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        with open(os.path.join(self.data_dir, PENDING_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def _clear_pending(self) -> None:
        try:
            os.remove(os.path.join(self.data_dir, PENDING_FILE))
        except OSError:
            pass


def _revisions_from_names(names: list[str]) -> list[str]:
    out = []
    for n in names:
        base = n.rsplit("/", 1)[-1]
        m = re.match(r"^(\d{4})_.+\.py$", base)
        if m:
            out.append(m.group(1))
    return sorted(out)


def _min_python(init_source: str) -> tuple[int, int] | None:
    m = re.search(r"MIN_PYTHON\s*=\s*\((\d+),\s*(\d+)\)", init_source)
    return (int(m.group(1)), int(m.group(2))) if m else None


async def finish_pending_update(db: Database, bot: Any, admins: Any, data_dir: str) -> str | None:
    """بعد از Restart: اگر به‌روزرسانی در انتظار تأیید است، نتیجه را ثبت و به مدیر خبر می‌دهد."""
    pending = await db.get_json("update:pending")
    if not pending:
        return None
    from . import notify
    ok = __version__ == pending.get("to_version")
    state = await db.get_json("update:state") or {}
    state.update(status="success" if ok else "failed", finished_at=now())
    if not ok:
        state["error"] = f"نسخه‌ی در حال اجرا {__version__} است، نه {pending.get('to_version')}"
    await db.set_json("update:state", state)
    await db.del_setting("update:pending")
    try:
        os.remove(os.path.join(data_dir, PENDING_FILE))
    except OSError:
        pass
    await db.audit(admin_id=pending.get("admin_id"), action="update_result", ref=pending.get("to_version"),
                   after={"ok": ok, "version": __version__})
    if await notify.enabled(db, "update") or not ok:
        await notify.to_admins(bot, admins, (f"✅ به‌روزرسانی به v{__version__} با موفقیت انجام شد." if ok else
                                             f"❌ به‌روزرسانی کامل نشد؛ نسخه‌ی فعلی v{__version__} است."))
    return "success" if ok else "failed"

