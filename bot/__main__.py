"""نقطه‌ی شروع: python -m bot"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import __version__
from .app import setup
from .config import get_settings
from .db import Database
from .shop import Shop
from .stard_api import StardClient, StardError
from .worker import order_worker

COMMANDS = [
    BotCommand(command="start", description="منوی اصلی"),
    BotCommand(command="shop", description="فروشگاه"),
    BotCommand(command="price", description="قیمت لحظه‌ای دلار، TON و استارز"),
    BotCommand(command="orders", description="سفارش‌های من"),
    BotCommand(command="me", description="حساب کاربری"),
    BotCommand(command="invite", description="دعوت دوستان"),
    BotCommand(command="cancel", description="انصراف"),
]


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("bot")
    log.info("stard-shop-bot v%s", __version__)

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
    await setup(dp, db=db, shop=shop, settings=settings)

    worker = asyncio.create_task(order_worker(shop, bot, settings.poll_interval_seconds))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        with contextlib.suppress(Exception):
            await bot.set_my_commands(COMMANDS)
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
