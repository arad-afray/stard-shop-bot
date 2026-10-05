"""ساخت Dispatcher با همه‌ی روترها و میان‌افزارها (مشترک بین اجرای اصلی و تست‌ها)."""
from __future__ import annotations

import logging

from aiogram import Dispatcher, F, Router
from aiogram.types import ErrorEvent

from .admins import Admins
from .config import Settings
from .db import Database
from .handlers import admin, prices, shop as shop_handlers, topup, user
from .middlewares import JoinChecker, JoinMiddleware, UserMiddleware
from .shop import Shop

log = logging.getLogger(__name__)


def build_routers() -> Router:
    """روتر چت خصوصی (همه‌ی فروشگاه) + روتر گروه (فقط قیمت)."""
    for r in (user.router, admin.router, topup.router, shop_handlers.router, prices.router):
        r._parent_router = None  # روترها ماژول‌سطح‌اند؛ اجازه‌ی اتصال دوباره (مثلاً در تست‌ها)
    private = Router(name="private")
    private.message.filter(F.chat.type == "private")
    private.callback_query.filter(F.message.chat.type == "private")
    # ترتیب مهم است: انصراف و منوی اصلی قبل از فرم‌های چندمرحله‌ای
    private.include_routers(user.router, admin.router, topup.router, shop_handlers.router)
    root = Router(name="root")
    root.include_routers(prices.router, private)
    return root


async def setup(dp: Dispatcher, *, db: Database, shop: Shop, settings: Settings) -> Admins:
    admins = Admins(db, settings.admin_ids)
    await admins.load()
    joins = JoinChecker(db)
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(UserMiddleware(db, admins))
        observer.outer_middleware(JoinMiddleware(joins))
    dp.include_router(build_routers())
    dp.workflow_data.update(db=db, shop=shop, settings=settings, admins=admins, joins=joins)

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("unhandled error: %s", event.exception, exc_info=event.exception)
        cb = event.update.callback_query
        if cb is not None:
            try:
                await cb.answer("⚠️ خطایی پیش آمد؛ دوباره تلاش کنید.", show_alert=True)
            except Exception:
                pass
        return True

    return admins
