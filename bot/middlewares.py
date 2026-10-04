"""میان‌افزار: ثبت/به‌روزرسانی کاربر، مسدودسازی و تشخیص مدیر."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .db import Database


class UserMiddleware(BaseMiddleware):
    def __init__(self, db: Database, admin_ids: list[int]):
        self.db = db
        self.admin_ids = set(admin_ids)

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None
        # فقط چت خصوصی؛ ربات فروشگاه در گروه کار نمی‌کند
        chat = data.get("event_chat")
        if chat is not None and chat.type != "private":
            return None
        user = await self.db.upsert_user(tg_user.id, tg_user.username, tg_user.first_name)
        is_admin = tg_user.id in self.admin_ids
        if user.banned and not is_admin:
            if isinstance(event, Message):
                await event.answer("⛔️ دسترسی شما به ربات مسدود شده است.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔️ دسترسی شما مسدود شده است.", show_alert=True)
            return None
        data["user"] = user
        data["is_admin"] = is_admin
        return await handler(event, data)
