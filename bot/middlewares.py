"""میان‌افزارها: ثبت کاربر و معرف، مسدودسازی، تشخیص مدیر، و عضویت اجباری در کانال."""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, Message, TelegramObject

from .admins import Admins
from .db import Database
from .ui import JOIN_CHECK, join_menu

log = logging.getLogger(__name__)


def _ref_from_start(event: TelegramObject) -> int | None:
    """آیدی معرف از /start ref_123"""
    if isinstance(event, Message) and event.text and event.text.startswith("/start "):
        arg = event.text.split(maxsplit=1)[1].strip()
        if arg.startswith("ref_") and arg[4:].isdigit():
            return int(arg[4:])
    return None


class UserMiddleware(BaseMiddleware):
    """فقط چت خصوصی. در گروه‌ها کاری نمی‌کند (روتر گروه جدا است و user لازم ندارد)."""

    def __init__(self, db: Database, admins: Admins):
        self.db = db
        self.admins = admins

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None
        chat = data.get("event_chat")
        if chat is not None and chat.type != "private":
            return await handler(event, data)
        user, created = await self.db.upsert_user(tg_user.id, tg_user.username, tg_user.first_name,
                                                  _ref_from_start(event))
        is_admin = self.admins.is_admin(tg_user.id)
        if user.banned and not is_admin:
            if isinstance(event, Message):
                await event.answer("⛔️ دسترسی شما به ربات مسدود شده است.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔️ دسترسی شما مسدود شده است.", show_alert=True)
            return None
        data["user"] = user
        data["is_admin"] = is_admin
        data["is_owner"] = self.admins.is_owner(tg_user.id)
        data["new_user"] = created
        return await handler(event, data)


class JoinChecker:
    """بررسی عضویت در کانال‌های اجباری، با کش کوتاه‌مدت برای سرعت."""

    OK_TTL = 300  # عضویت تأییدشده تا ۵ دقیقه دوباره پرسیده نمی‌شود

    def __init__(self, db: Database):
        self.db = db
        self._ok: dict[int, float] = {}

    def reset(self) -> None:
        self._ok.clear()

    async def channels(self) -> list[dict]:
        return await self.db.get_json("force_join", [])

    async def active(self) -> bool:
        return (await self.db.get_setting("force_join_on", "0")) == "1" and bool(await self.channels())

    async def missing(self, bot: Bot, uid: int, *, use_cache: bool = True) -> list[dict]:
        """کانال‌هایی که کاربر عضوشان نیست."""
        if use_cache and self._ok.get(uid, 0) > time.monotonic():
            return []
        out = []
        for ch in await self.channels():
            try:
                m = await bot.get_chat_member(ch["chat"], uid)
            except Exception as e:
                # ربات در کانال مدیر نیست یا کانال حذف شده: کاربر را قفل نکن، فقط لاگ کن
                log.warning("force-join check %s failed: %s", ch.get("chat"), e)
                continue
            if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) or \
                    (m.status == ChatMemberStatus.RESTRICTED and not getattr(m, "is_member", True)):
                out.append(ch)
        if not out:
            self._ok[uid] = time.monotonic() + self.OK_TTL
        return out


class JoinMiddleware(BaseMiddleware):
    def __init__(self, checker: JoinChecker):
        self.checker = checker

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        if "user" not in data or data.get("is_admin"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == JOIN_CHECK:
            return await handler(event, data)
        if not await self.checker.active():
            return await handler(event, data)
        missing = await self.checker.missing(data["bot"], data["user"].id)
        if not missing:
            return await handler(event, data)
        text = "📣 برای استفاده از ربات، اول در کانال‌های زیر عضو شوید و بعد «✅ عضو شدم» را بزنید:"
        if isinstance(event, Message):
            await event.answer(text, reply_markup=join_menu(missing))
        elif isinstance(event, CallbackQuery):
            await event.answer("📣 اول در کانال‌ها عضو شوید.", show_alert=True)
            if event.message:
                await event.message.answer(text, reply_markup=join_menu(missing))
        return None
