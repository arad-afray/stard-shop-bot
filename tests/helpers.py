"""ابزار مشترک تست‌ها: جلسه‌ی ضبط‌کننده‌ی تلگرام (بدون اینترنت)."""
import datetime as dt
import itertools

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TgUser

ids = itertools.count(1)
ADMIN, CUSTOMER = 100, 200


def tg_user(uid):
    return TgUser(id=uid, is_bot=False, first_name=f"U{uid}", username=f"user{uid}")


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[TelegramMethod] = []
        self.members: dict[int, str] = {}  # وضعیت عضویت کاربران در کانال اجباری
        self.blocked: set[int] = set()     # کاربرانی که ربات را بلاک کرده‌اند

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        if getattr(method, "chat_id", None) in self.blocked and name in ("SendMessage", "CopyMessage"):
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        self.sent.append(method)
        if name == "GetMe":
            return TgUser(id=42, is_bot=True, first_name="Shop", username="shop_bot")
        if name == "GetChatMember":
            from aiogram.types import ChatMemberLeft, ChatMemberMember
            if self.members.get(method.user_id, "left") == "member":
                return ChatMemberMember(user=tg_user(method.user_id))
            return ChatMemberLeft(user=tg_user(method.user_id))
        if name in ("SendMessage", "SendPhoto", "EditMessageText", "EditMessageCaption", "CopyMessage"):
            if name == "CopyMessage":
                return method.__returning__(message_id=next(ids))
            return Message(message_id=next(ids), date=dt.datetime.now(),
                           chat=Chat(id=getattr(method, "chat_id", None) or 1, type="private"),
                           text=getattr(method, "text", None) or "")
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    def texts(self) -> list[str]:
        return [getattr(m, "text", None) or getattr(m, "caption", None) or "" for m in self.sent
                if type(m).__name__ != "AnswerCallbackQuery"]

    def alerts(self) -> list[str]:
        return [m.text or "" for m in self.sent if type(m).__name__ == "AnswerCallbackQuery"]




def make_bot(session=None) -> tuple[Bot, "RecordingSession"]:
    session = session or RecordingSession()
    return Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML")), session


async def send(dp, bot, uid, text=None, photo=None, chat=None):
    msg = Message(message_id=next(ids), date=dt.datetime.now(), chat=chat or Chat(id=uid, type="private"),
                  from_user=tg_user(uid), text=text, photo=photo)
    await dp.feed_update(bot, Update(update_id=next(ids), message=msg))


async def click(dp, bot, uid, data):
    msg = Message(message_id=next(ids), date=dt.datetime.now(), chat=Chat(id=uid, type="private"), text="x",
                  from_user=tg_user(42))
    cb = CallbackQuery(id=str(next(ids)), from_user=tg_user(uid), chat_instance="c", message=msg,
                       data=data.pack())
    await dp.feed_update(bot, Update(update_id=next(ids), callback_query=cb))


async def click_raw(dp, bot, uid, data: str):
    msg = Message(message_id=next(ids), date=dt.datetime.now(), chat=Chat(id=uid, type="private"), text="x",
                  from_user=tg_user(42))
    cb = CallbackQuery(id=str(next(ids)), from_user=tg_user(uid), chat_instance="c", message=msg, data=data)
    await dp.feed_update(bot, Update(update_id=next(ids), callback_query=cb))
