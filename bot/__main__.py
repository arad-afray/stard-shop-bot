"""نقطه‌ی شروع: python -m bot"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .config import get_settings
from .db import Database
from .handlers import admin, shop as shop_handlers, topup, user
from .middlewares import UserMiddleware
from .shop import Shop
from .stard_api import StardClient, StardError
from .worker import order_worker


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("bot")

    db = Database(settings.database_path)
    await db.connect()
    api = StardClient(settings.stard_api_key, settings.stard_base_url)
    shop = Shop(db, api, default_profit=settings.default_profit_percent, pay_currency=settings.stard_pay_currency)

    try:
        ping = await api.ping()
        log.info("Stard API OK: environment=%s scopes=%s", ping["environment"], ping["key"]["scopes"])
    except StardError as e:
        log.error("Stard API check failed: %s — کلید STARD_API_KEY را بررسی کنید", e)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    mw = UserMiddleware(db, settings.admin_ids)
    dp.message.outer_middleware(mw)
    dp.callback_query.outer_middleware(mw)
    # ترتیب مهم است: انصراف و منوی اصلی قبل از فرم‌های چندمرحله‌ای
    dp.include_routers(user.router, admin.router, topup.router, shop_handlers.router)
    dp.workflow_data.update(db=db, shop=shop, settings=settings, admin_ids=settings.admin_ids)

    worker = asyncio.create_task(order_worker(shop, bot, settings.poll_interval_seconds))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        await api.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
