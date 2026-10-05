"""ساخت Dispatcher با همه‌ی روترها و میان‌افزارها (مشترک بین اجرای اصلی و تست‌ها)."""
from __future__ import annotations

import logging
from typing import Any

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.types import ErrorEvent

from .admins import Admins
from .config import Settings
from .db import Database
from .handlers import admin, prices, shop as shop_handlers, topup, user
from .logging_setup import correlation_id, redact
from .middlewares import JoinChecker, JoinMiddleware, ThrottleMiddleware, UserMiddleware
from .shop import Shop

log = logging.getLogger(__name__)


class CorrelationMiddleware(BaseMiddleware):
    """هر آپدیت تلگرام یک correlation id می‌گیرد (u<update_id>) تا لاگ‌هایش با هم پیدا شوند."""

    async def __call__(self, handler, event, data):
        upd = data.get("event_update")
        token = correlation_id.set(f"u{upd.update_id}" if upd is not None else "u-")
        m = data.get("metrics")
        if m is not None:
            m.inc("telegram_updates_total")
        try:
            return await handler(event, data)
        finally:
            correlation_id.reset(token)


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


async def setup(dp: Dispatcher, *, db: Database, shop: Shop, settings: Settings, queue: Any = None,
                locks: Any = None, limiter: Any = None, extra: dict | None = None) -> Admins:
    admins = Admins(db, settings.admin_ids)
    await admins.load()
    joins = JoinChecker(db)
    risk = (extra or {}).get("risk")
    dp.update.outer_middleware(CorrelationMiddleware())
    for observer in (dp.message, dp.callback_query):
        observer.outer_middleware(UserMiddleware(db, admins))
        observer.outer_middleware(ThrottleMiddleware(limiter=limiter, risk=risk))
        observer.outer_middleware(JoinMiddleware(joins))
    dp.include_router(build_routers())
    dp.workflow_data.update(db=db, shop=shop, settings=settings, admins=admins, joins=joins, queue=queue,
                            locks=locks, limiter=limiter, **(extra or {}))

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.error("unhandled error: %s", redact(f"{type(event.exception).__name__}: {event.exception}"),
                  exc_info=event.exception)
        metrics = dp.workflow_data.get("metrics")
        if metrics is not None:
            metrics.inc("errors_total")
        cb = event.update.callback_query
        if cb is not None:
            try:
                await cb.answer("⚠️ خطایی پیش آمد؛ دوباره تلاش کنید.", show_alert=True)
            except Exception:
                pass
        return True

    return admins
