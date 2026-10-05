"""نقطه‌ی شروع: python -m bot

ROLE=all     (پیش‌فرض) ربات + worker + زمان‌بند در یک پردازه — مناسب سرور کوچک و ویندوز
ROLE=bot     فقط دریافت پیام‌های تلگرام (می‌شود چند نمونه اجرا کرد؛ Redis لازم است)
ROLE=worker  فقط صف و کارهای پس‌زمینه (می‌شود چند نمونه اجرا کرد)

کد خروج 3 یعنی «لطفاً دوباره اجرا کن» (بعد از به‌روزرسانی یا بازگردانی پشتیبان)؛ run.ps1/run.sh و Docker
این کار را خودکار انجام می‌دهند.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import __version__, monitor  # noqa: F401 — monitor کار دوره‌ای هشدار را ثبت می‌کند
from .app import setup
from .backups import BackupManager
from .updater import Updater, finish_pending_update
from .db import now_ms
from .http_server import start_http
from .metrics import Metrics
from .config import get_settings
from .logging_setup import service_name, setup_logging
from .runtime import Services, Supervisor, build_services
from .stard_api import StardError
from .worker import Context, JobWorker, Scheduler

RESTART_EXIT_CODE = 3

COMMANDS = [
    BotCommand(command="start", description="منوی اصلی"),
    BotCommand(command="shop", description="فروشگاه"),
    BotCommand(command="price", description="قیمت لحظه‌ای دلار، TON و استارز"),
    BotCommand(command="orders", description="سفارش‌های من"),
    BotCommand(command="me", description="حساب کاربری"),
    BotCommand(command="invite", description="دعوت دوستان"),
    BotCommand(command="cancel", description="انصراف"),
]

log = logging.getLogger("bot")


def _storage(services: Services):
    if services.redis is not None:
        from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
        # FSM مشترک بین همه‌ی نمونه‌ها؛ حالت خرید با ری‌استارت یا تعویض نمونه گم نمی‌شود
        return RedisStorage(services.redis, key_builder=DefaultKeyBuilder(prefix="stardfsm", with_destiny=True),
                            state_ttl=86400, data_ttl=86400)
    if services.settings.role == "bot":
        log.warning("ROLE=bot without REDIS_URL: checkout state is per-instance; run a single bot instance")
    return MemoryStorage()


async def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_dir, settings.secret_values())
    service_name.set(settings.role)
    log.info("stard-shop-bot v%s starting (role=%s, instance=%s)", __version__, settings.role, settings.instance_id)

    services = await build_services(settings)
    metrics = Metrics()
    services.api.observer = metrics.observe_api
    services.extra.update(metrics=metrics, redis=services.redis)
    log.info("database: %s | redis: %s", services.db.dialect, "on" if services.redis is not None else "off")
    try:
        ping = await services.api.ping()
        log.info("Stard API OK: environment=%s", ping.get("environment"))
    except StardError as e:
        log.error("Stard API check failed: %s %s — STARD_API_KEY را بررسی کنید", e.status, e.code)

    bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=_storage(services))
    extra = dict(services.extra)
    admins = await setup(dp, db=services.db, shop=services.shop, settings=settings, queue=services.queue,
                         locks=services.locks, limiter=services.limiter, extra={"services": services, **extra})
    ctx = Context(bot=bot, shop=services.shop, db=services.db, queue=services.queue, locks=services.locks,
                  admins=admins, settings=settings, metrics=extra.get("metrics"), extra=extra)

    async def alert(key: str, text: str) -> None:
        from . import notify
        await notify.to_admins(bot, admins, f"🚨 <b>هشدار</b>\n{text}")

    sup = Supervisor(on_alert=alert)
    backups = BackupManager(services.db, settings.backup_dir, settings, services.locks)
    data_dir = os.path.dirname(os.path.abspath(settings.database_path))
    updater = Updater(settings, services.db, backups, services.locks, data_dir=data_dir)
    services.extra.update(supervisor=sup, backups=backups, updater=updater)
    try:
        await finish_pending_update(services.db, bot, admins, data_dir)
    except Exception:
        log.exception("post-update verification failed")
    worker = scheduler = None
    if settings.role in ("all", "worker"):
        worker = JobWorker(ctx, concurrency=settings.worker_concurrency)
        scheduler = Scheduler(ctx)
        services.extra.update(worker=worker, scheduler=scheduler)
        sup.start("worker", worker.run)
        sup.start("scheduler", scheduler.run)
    roles = [r for r in ("bot", "worker", "scheduler") if settings.role == "all" or settings.role == r
             or (settings.role == "worker" and r == "scheduler")]
    sup.start("heartbeat", lambda: monitor.heartbeat_loop(ctx, roles, now_ms()))
    sup.start("watchdog", lambda: _watchdog(sup, worker, scheduler))
    http = await start_http(services)

    stop = asyncio.Event()
    exit_code = {"code": 0}
    services.extra["request_restart"] = lambda: (exit_code.update(code=RESTART_EXIT_CODE), stop.set())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # ویندوز
            pass

    polling = None
    try:
        if settings.role in ("all", "bot"):
            await bot.delete_webhook(drop_pending_updates=False)
            try:
                await bot.set_my_commands(COMMANDS)
            except Exception as e:
                log.warning("set_my_commands failed: %s", type(e).__name__)
            polling = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(),
                                                           handle_signals=False, close_bot_session=False))
            waiters = [polling, asyncio.create_task(stop.wait())]
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if polling.done() and not stop.is_set():
                # polling بدون درخواست توقف تمام شد (خطای شبکه‌ی دائمی، توکن نامعتبر، …): کد خروج غیرصفر
                # تا run.ps1/run.sh/Docker پردازه را دوباره اجرا کنند
                exc = polling.exception() if not polling.cancelled() else None
                log.error("polling stopped unexpectedly: %s", type(exc).__name__ if exc else "no error")
                exit_code["code"] = 1
        else:
            await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("shutting down…")
        if polling is not None and not polling.done():
            await dp.stop_polling()
            polling.cancel()
        if worker is not None:
            worker.stop()
        if scheduler is not None:
            scheduler.stop()
        await sup.stop()
        if http is not None:
            await http.cleanup()
        await services.close()
        await bot.session.close()
    return exit_code["code"]


async def _watchdog(sup: Supervisor, worker, scheduler) -> None:
    """اگر حلقه‌ی worker یا زمان‌بند گیر کند (نه کرش)، آن را Restart می‌کند."""
    import time
    while True:
        await asyncio.sleep(60)
        for name, svc in (("worker", worker), ("scheduler", scheduler)):
            if svc is not None and time.monotonic() - svc.last_loop > 300 and name in sup.tasks:
                log.error("%s loop stalled for %.0fs — restarting", name, time.monotonic() - svc.last_loop)
                sup.restart(name)


def run() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = 0
    if code == RESTART_EXIT_CODE and os.environ.get("STARD_SUPERVISED") != "1":
        # بدون run.ps1/run.sh/Docker: خود پردازه را با کد جدید دوباره اجرا می‌کنیم
        logging.shutdown()
        os.execv(sys.executable, [sys.executable, "-m", "bot"])
    sys.exit(code)


if __name__ == "__main__":
    run()
