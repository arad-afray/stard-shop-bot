"""پشتیبان، بازگردانی، به‌روزرسانی (با Rollback)، مهاجرت و اسکریپت Restart خودکار."""
import asyncio
import json
import os
import subprocess
import sys
import zipfile

import httpx
import pytest

from bot import __version__
from bot.backups import BackupError, BackupManager
from bot.config import Settings
from bot.db import Database
from bot.locks import DistributedLock
from bot.updater import ReleaseInfo, Updater, parse_version
from tests.conftest import TEST_DATABASE_URL, new_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _settings(tmp_path, **kw):
    return Settings(bot_token="1:x", stard_api_key="sk_test_ok", admin_ids=[1], backup_dir=str(tmp_path / "bk"),
                    update_repo="owner/repo", **kw)


async def _seed(db: Database) -> int:
    await db.upsert_user(7, "seven", "Seven")
    await db.credit(7, 500_000, "topup")
    oid = await db.create_order_and_debit(user_id=7, type_="stars", category="stars", product_id=None, title="⭐ 50",
                                          quantity=50, recipient="@a", gift_message=None, quote_id=None,
                                          base_amount=200_000, price=250_000, checkout_id="chk")
    await db.create_coupon("SAVE10", 10, 5)
    await db.set_setting("card_number", "6037-0000-0000-0000")
    return oid


@pytest.fixture
async def db():
    d = await new_db()
    yield d
    await d.close()


# ---------- پشتیبان ----------
async def test_backup_verify_restore_cycle(db, tmp_path):
    oid = await _seed(db)
    mgr = BackupManager(db, str(tmp_path / "bk"), _settings(tmp_path), DistributedLock(db, "t"))
    info = await mgr.create("manual", admin_id=1)
    assert info.rows > 0 and info.kind == "manual"
    with zipfile.ZipFile(info.path) as z:
        cfg = json.loads(z.read("config.json"))
    assert cfg["bot_token"] == "set" and cfg["stard_api_key"] == "set"  # secretها در پشتیبان نیستند
    assert mgr.verify(info.name)["ok"]
    assert (await mgr.restore_test(info.name))["ok"]
    # تغییر داده بعد از پشتیبان
    await db.credit(7, 1, "topup")
    await db.update_order(oid, status="completed")
    await db.write("DELETE FROM coupons")
    res = await mgr.restore(info.name, admin_id=1)
    assert res["ok"] and res["safety_backup"].endswith("pre-restore.zip")
    assert (await db.get_user(7)).balance == 250_000
    assert (await db.get_order(oid))["status"] == "new" and await db.get_coupon("SAVE10")
    assert not await db.ledger_balance_check()
    assert await db.get_setting("card_number") == "6037-0000-0000-0000"
    # شمارنده‌ی id بعد از بازگردانی درست کار می‌کند (به‌خصوص در PostgreSQL)
    await db.credit(7, 1_000_000, "topup")
    oid2 = await db.create_order_and_debit(user_id=7, type_="stars", category="stars", product_id=None, title="t",
                                           quantity=50, recipient="@a", gift_message=None, quote_id=None,
                                           base_amount=1, price=2)
    assert oid2 > oid
    assert {b.kind for b in mgr.list()} == {"manual", "pre-restore"}
    await mgr.delete(info.name, admin_id=1)
    assert [a["action"] for a in await db.audit_entries(limit=50)].count("backup_restore") == 1


async def test_tampered_backup_is_rejected(db, tmp_path):
    await _seed(db)
    mgr = BackupManager(db, str(tmp_path / "bk"))
    info = await mgr.create()
    tampered = info.path.replace(".zip", "-x.zip").replace("manual-x", "manual-2")
    with zipfile.ZipFile(info.path) as src, zipfile.ZipFile(tampered, "w") as dst:
        for item in src.namelist():
            data = src.read(item)
            if item == "tables/users.jsonl":
                data = data.replace(b"500000", b"999999").replace(b"250000", b"999999")
            dst.writestr(item, data)
    name = os.path.basename(tampered)
    v = mgr.verify(name)
    assert not v["ok"] and "checksum" in v["error"]
    with pytest.raises(BackupError):
        await mgr.restore(name)
    assert (await db.get_user(7)).balance == 250_000


async def test_backup_from_newer_schema_is_refused(db, tmp_path):
    mgr = BackupManager(db, str(tmp_path / "bk"))
    info = await mgr.create()
    newer = info.path.replace("manual", "manual-9")
    with zipfile.ZipFile(info.path) as src, zipfile.ZipFile(newer, "w") as dst:
        m = json.loads(src.read("manifest.json"))
        m["revision"] = "9999"
        dst.writestr("manifest.json", json.dumps(m))
        for item in src.namelist():
            if item != "manifest.json":
                dst.writestr(item, src.read(item))
    v = mgr.verify(os.path.basename(newer))
    assert not v["ok"] and "جدیدتر" in v["error"]


async def test_restore_is_atomic(db, tmp_path, monkeypatch):
    await _seed(db)
    mgr = BackupManager(db, str(tmp_path / "bk"))
    info = await mgr.create()
    await db.credit(7, 123, "topup")
    import bot.backups as b

    real_insert = b.insert
    calls = {"n": 0}

    def flaky_insert(table):
        calls["n"] += 1
        if table.name == "orders":
            raise RuntimeError("disk failure mid-restore")
        return real_insert(table)
    monkeypatch.setattr(b, "insert", flaky_insert)
    with pytest.raises(RuntimeError):
        await mgr.restore(info.name)
    assert (await db.get_user(7)).balance == 250_123   # هیچ تغییری اعمال نشد


async def test_rotation_keeps_manual_backups(db, tmp_path):
    mgr = BackupManager(db, str(tmp_path / "bk"))
    for _ in range(4):
        await mgr.create("auto")
    await mgr.create("manual")
    assert await mgr.rotate(2) == 2
    kinds = [b.kind for b in mgr.list()]
    assert kinds.count("auto") == 2 and kinds.count("manual") == 1


async def test_invalid_backup_name_rejected(db, tmp_path):
    mgr = BackupManager(db, str(tmp_path / "bk"))
    with pytest.raises(BackupError):
        mgr.verify("../../etc/passwd")


@pytest.mark.skipif(not os.environ.get("TEST_PG_URL"), reason="set TEST_PG_URL to test SQLite → PostgreSQL migration")
async def test_sqlite_backup_restores_into_postgres(tmp_path):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    src = Database(":memory:")
    await src.connect()
    await _seed(src)
    info = await BackupManager(src, str(tmp_path / "bk")).create()
    await src.close()
    eng = create_async_engine(os.environ["TEST_PG_URL"])
    async with eng.begin() as c:
        await c.execute(text("DROP SCHEMA public CASCADE"))
        await c.execute(text("CREATE SCHEMA public"))
    await eng.dispose()
    dst = Database(url=os.environ["TEST_PG_URL"])
    await dst.connect()
    try:
        await BackupManager(dst, str(tmp_path / "bk")).restore(info.name)
        assert (await dst.get_user(7)).balance == 250_000 and await dst.get_coupon("SAVE10")
    finally:
        await dst.close()


# ---------- مهاجرت ----------
@pytest.mark.skipif(bool(TEST_DATABASE_URL), reason="uses a temporary SQLite file")
def test_migrate_cli_downgrade_and_upgrade(tmp_path):
    env = {**os.environ, "BOT_TOKEN": "1:x", "STARD_API_KEY": "sk_test_x", "DATABASE_PATH": str(tmp_path / "m.db")}
    run = lambda *a: subprocess.run([sys.executable, "-m", "bot.migrate", *a], cwd=ROOT, env=env,  # noqa: E731
                                    capture_output=True, text=True, timeout=120)
    r = run()
    assert r.returncode == 0 and "now at 0004" in r.stdout, r.stdout + r.stderr
    r = run("--to", "0002")
    assert r.returncode == 0 and "now at 0002" in r.stdout
    assert run("--check").returncode == 1
    assert run().returncode == 0 and run("--check").returncode == 0


# ---------- به‌روزرسانی ----------
def _gh(releases: dict | None, commits: dict | None = None):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json=releases) if releases else httpx.Response(404, json={})
        if "/commits/" in req.url.path:
            tag = req.url.path.rsplit("/", 1)[1]
            sha = (commits or {}).get(tag)
            return httpx.Response(200, json={"sha": sha}) if sha else httpx.Response(404, json={})
        return httpx.Response(404, json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.com")


def test_parse_version():
    assert parse_version("v3.0.0") == (3, 0, 0) and parse_version("2.10.1") == (2, 10, 1)
    assert parse_version("v3.0.0-beta") is None and parse_version(None) is None


async def test_check_reports_newer_release_only(db, tmp_path):
    rel = {"tag_name": "v99.0.0", "body": "## Changes\n- big", "published_at": "2026-10-05T10:00:00Z",
           "html_url": "https://x", "draft": False, "prerelease": False, "assets": []}
    up = Updater(_settings(tmp_path), db, None, DistributedLock(db, "t"), http=_gh(rel))
    info = await up.check()
    assert info.newer and info.latest == "99.0.0" and "big" in info.changelog and info.current == __version__
    up._http = _gh({**rel, "tag_name": f"v{__version__}"})
    assert not (await up.check()).newer
    up._http = _gh({**rel, "prerelease": True})
    assert (await up.check()).error
    up._http = _gh(None)
    assert "Release" in (await up.check()).error


def _git(cwd, *args) -> str:
    """یک دستور git اجرا می‌کند و commit فعلی (HEAD) را برمی‌گرداند."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})
    return _head(cwd)


@pytest.fixture
def repo(tmp_path):
    """یک «origin» و یک نصب git؛ نسخه‌ی v99.0.0 روی origin تگ شده است."""
    origin, install = tmp_path / "origin", tmp_path / "install"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "bot").mkdir()
    (origin / "bot" / "migrations" / "versions").mkdir(parents=True)
    for rev in ("0001_a", "0002_b", "0003_c", "0004_d"):
        (origin / "bot" / "migrations" / "versions" / f"{rev}.py").write_text("")
    (origin / "bot" / "__init__.py").write_text(f'__version__ = "{__version__}"\n')
    (origin / "requirements.txt").write_text("")
    _git(origin, "add", "-A")
    old = _git(origin, "commit", "-qm", "old")
    subprocess.run(["git", "clone", "-q", str(origin), str(install)], check=True)
    (origin / "bot" / "__init__.py").write_text('__version__ = "99.0.0"\nMIN_PYTHON = (3, 10)\n')
    (origin / "bot" / "migrations" / "versions" / "0005_e.py").write_text("")
    _git(origin, "add", "-A")
    new = _git(origin, "commit", "-qm", "new")
    _git(origin, "tag", "v99.0.0")
    return install, old, new


def _release():
    return ReleaseInfo(current=__version__, latest="99.0.0", tag="v99.0.0", newer=True)


def _runner(fail_on: str | None = None, log: list | None = None):
    """git واقعی اجرا می‌شود؛ pip/migrate/selfcheck شبیه‌سازی می‌شوند."""
    from bot.updater import run_cmd

    async def run(*args, cwd=ROOT, timeout=900, env=None):
        if log is not None:
            log.append(" ".join(args[1:4]) if args[0] == sys.executable else " ".join(args[:3]))
        if args[0] == "git":
            return await run_cmd(*args, cwd=cwd, timeout=timeout)
        joined = " ".join(args)
        if fail_on and fail_on in joined:
            return 1, f"{fail_on} exploded"
        if "bot.migrate" in joined:
            return 0, "now at 0005"
        return 0, "OK"
    return run


async def _updater(db, tmp_path, install, runner, sha):
    mgr = BackupManager(db, str(tmp_path / "bk"))
    return Updater(_settings(tmp_path), db, mgr, DistributedLock(db, "t"), root=str(install), runner=runner,
                   http=_gh(None, {"v99.0.0": sha}), data_dir=str(tmp_path / "data")), mgr


def _head(install):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=install, capture_output=True, text=True).stdout.strip()


async def test_update_success_backs_up_switches_and_restarts(db, tmp_path, repo, monkeypatch):
    monkeypatch.setattr("bot.updater.install_mode", lambda root: "git")
    install, old, new = repo
    await _seed(db)
    calls = []
    up, mgr = await _updater(db, tmp_path, install, _runner(log=calls), new)
    restarted = []
    state = await up.apply(_release(), admin_id=1, request_restart=lambda: restarted.append(1))
    assert state.status == "restarting", state
    assert _head(install) == new and restarted == [1]
    assert state.migration_required and state.backup and mgr.verify(state.backup)["ok"]
    assert [s[1] for s in state.steps] == ["ok"] * (len(state.steps) - 1) + ["run"]
    assert os.path.exists(tmp_path / "data" / "update-pending.json")
    assert any("pip install" in c for c in calls) and any("bot.migrate" in c for c in calls)
    assert await db.get_json("update:pending")
    assert [a for a in await db.audit_entries(limit=50) if a["action"] == "update_apply"]


async def test_failed_health_check_rolls_back_code_schema_and_data(db, tmp_path, repo, monkeypatch):
    monkeypatch.setattr("bot.updater.install_mode", lambda root: "git")
    install, old, new = repo
    await _seed(db)
    calls = []
    up, mgr = await _updater(db, tmp_path, install, _runner("bot.selfcheck", calls), new)
    await db.credit(7, 1, "topup")  # داده بعد از پشتیبان؛ بازگردانی پشتیبان pre-update آن را برمی‌گرداند
    state = await up.apply(_release(), admin_id=1)
    assert state.status == "rolled_back", state
    assert _head(install) == old
    assert any("--to" in c for c in calls)  # downgrade شِما با کد جدید
    assert not os.path.exists(tmp_path / "data" / "update-pending.json")
    assert [a for a in await db.audit_entries(limit=50) if a["action"] == "update_failed"]


async def test_tag_commit_mismatch_aborts_before_any_change(db, tmp_path, repo, monkeypatch):
    monkeypatch.setattr("bot.updater.install_mode", lambda root: "git")
    install, old, new = repo
    up, mgr = await _updater(db, tmp_path, install, _runner(), "deadbeef" * 5)
    state = await up.apply(_release())
    assert state.status == "rolled_back" and "commit" in state.error
    assert _head(install) == old and mgr.list() == []


async def test_docker_mode_refuses_in_place_update(db, tmp_path, repo, monkeypatch):
    monkeypatch.setattr("bot.updater.install_mode", lambda root: "docker")
    install, old, new = repo
    up, _ = await _updater(db, tmp_path, install, _runner(), new)
    state = await up.apply(_release())
    assert state.status == "rolled_back" and "Docker" in state.error and _head(install) == old


async def test_two_updates_cannot_run_concurrently(db, tmp_path, repo, monkeypatch):
    monkeypatch.setattr("bot.updater.install_mode", lambda root: "git")
    install, old, new = repo
    up1, _ = await _updater(db, tmp_path, install, _runner(), new)
    up2, _ = await _updater(db, tmp_path, install, _runner(), new)
    up2.locks = DistributedLock(db, "other-instance")
    res = await asyncio.gather(up1.apply(_release()), up2.apply(_release()), return_exceptions=True)
    assert sum(isinstance(r, Exception) and "در حال اجراست" in str(r) for r in res) == 1


# ---------- Restart خودکار ----------
def test_run_sh_restarts_and_rolls_back(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "run.sh").write_text(open(os.path.join(ROOT, "run.sh")).read())
    (work / "data").mkdir()
    counter = tmp_path / "n"
    counter.write_text("0")
    # پایتون جعلی: بار اول درخواست Restart (3)، بعد سه کرش سریع، بعد توقف عادی
    fake = tmp_path / "python"
    fake.write_text(f"""#!/usr/bin/env bash
if [ "$1" = "-c" ]; then exec {sys.executable} "$@"; fi
if [ "$2" = "pip" ]; then exit 0; fi
n=$(cat {counter}); echo $((n+1)) > {counter}
case $n in 0) exit 3;; 1|2|3) exit 1;; *) exit 0;; esac
""")
    fake.chmod(0o755)
    (work / "data" / "update-pending.json").write_text('{"from_commit": "abc123"}')
    gitlog = tmp_path / "git.log"
    fakegit = tmp_path / "git"
    fakegit.write_text(f'#!/usr/bin/env bash\necho "$@" >> {gitlog}\n')
    fakegit.chmod(0o755)
    env = {**os.environ, "PYTHON": str(fake), "STARD_RESTART_DELAY": "0", "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", str(work / "run.sh")], cwd=work, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "restart requested" in out and out.count("bot crashed") == 3
    assert "rolling back to abc123" in out and "stopped normally" in out
    assert "checkout --force --detach abc123" in gitlog.read_text()
    assert not (work / "data" / "update-pending.json").exists()


async def test_archive_mode_update_validates_checksum_and_extracts(db, tmp_path, monkeypatch):
    """همان zip که release.yml با git archive می‌سازد: SHA256SUMS بررسی و کد استخراج می‌شود، .env و data دست نمی‌خورند."""
    import hashlib
    monkeypatch.setattr("bot.updater.install_mode", lambda root: "archive")
    src = tmp_path / "src"
    (src / "bot" / "migrations" / "versions").mkdir(parents=True)
    for rev in ("0001_a", "0002_b", "0003_c", "0004_d"):
        (src / "bot" / "migrations" / "versions" / f"{rev}.py").write_text("")
    (src / "bot" / "__init__.py").write_text('__version__ = "99.0.0"\n')
    (src / "requirements.txt").write_text("")
    _git(src, "init", "-q", "-b", "main")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "r")
    zname = "stard-shop-bot-v99.0.0.zip"
    subprocess.run(["git", "archive", "--format=zip", "--prefix=stard-shop-bot-v99.0.0/", "-o", str(tmp_path / zname),
                    "HEAD"], cwd=src, check=True)
    data = (tmp_path / zname).read_bytes()
    sums = f"{hashlib.sha256(data).hexdigest()}  {zname}\n"

    def handler(req):
        if req.url.path.endswith("SHA256SUMS"):
            return httpx.Response(200, text=sums)
        return httpx.Response(200, content=data)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    install = tmp_path / "install"
    (install / "bot").mkdir(parents=True)
    (install / "bot" / "__init__.py").write_text(f'__version__ = "{__version__}"\n')
    (install / ".env").write_text("SECRET=keep")
    (install / "data").mkdir()
    (install / "data" / "shop.db").write_text("db")
    up = Updater(_settings(tmp_path), db, BackupManager(db, str(tmp_path / "bk")), DistributedLock(db, "t"),
                 root=str(install), runner=_runner(), http=http, data_dir=str(install / "data"))
    rel = ReleaseInfo(current=__version__, latest="99.0.0", tag="v99.0.0", newer=True,
                      assets={zname: "https://x/zip", "SHA256SUMS": "https://x/SHA256SUMS"})
    state = await up.apply(rel, request_restart=lambda: None)
    assert state.status == "restarting", state
    assert '99.0.0' in (install / "bot" / "__init__.py").read_text()
    assert (install / ".env").read_text() == "SECRET=keep" and (install / "data" / "shop.db").read_text() == "db"
    assert (install / "bot" / "migrations" / "versions" / "0004_d.py").exists()
    # فایل دست‌کاری‌شده رد می‌شود
    sums = "0" * 64 + f"  {zname}\n"
    (install / "bot" / "__init__.py").write_text(f'__version__ = "{__version__}"\n')
    state = await up.apply(rel)
    assert state.status == "rolled_back" and "SHA-256" in state.error
    assert __version__ in (install / "bot" / "__init__.py").read_text()
