"""شروع، حساب کاربری، سفارش‌ها، پشتیبانی و انصراف."""
from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..db import Database, User
from ..pricing import fmt_toman
from ..ui import (BTN_ACCOUNT, BTN_CANCEL, BTN_ORDERS, BTN_SUPPORT, STATUS_LABEL, main_menu)

router = Router(name="user")

DEFAULT_SUPPORT = "برای پشتیبانی به مدیر ربات پیام دهید."


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, user: User, is_admin: bool):
    await state.clear()
    name = escape(user.first_name or "دوست")
    await message.answer(
        f"سلام {name} 👋\n"
        "به فروشگاه خوش آمدید!\n\n"
        "⭐ استارز، 💎 پریمیوم و 🎁 گیفت تلگرام را با تحویل <b>خودکار</b> بخرید.\n"
        "اول کیف پول را شارژ کنید، بعد از بخش فروشگاه خرید کنید.",
        reply_markup=main_menu(is_admin))


@router.message(F.text == BTN_CANCEL)
@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext, is_admin: bool):
    await state.clear()
    await message.answer("لغو شد. به منوی اصلی برگشتید.", reply_markup=main_menu(is_admin))


@router.message(F.text == BTN_ACCOUNT)
async def account(message: Message, state: FSMContext, user: User, db: Database):
    await state.clear()
    orders = await db.count_user_orders(user.id)
    uname = f"@{escape(user.username)}" if user.username else "—"
    await message.answer(
        "👤 <b>حساب کاربری</b>\n\n"
        f"🆔 شناسه: <code>{user.id}</code>\n"
        f"👤 یوزرنیم: {uname}\n"
        f"💰 موجودی: <b>{fmt_toman(user.balance)}</b>\n"
        f"📦 تعداد سفارش‌ها: {orders}\n"
        f"📅 عضویت: {user.created_at[:10]}")


@router.message(F.text == BTN_ORDERS)
async def my_orders(message: Message, state: FSMContext, user: User, db: Database):
    await state.clear()
    rows = await db.user_orders(user.id, limit=10)
    if not rows:
        await message.answer("هنوز سفارشی ثبت نکرده‌اید. از «🛍 فروشگاه» شروع کنید.")
        return
    lines = ["📦 <b>۱۰ سفارش آخر شما</b>\n"]
    for o in rows:
        to = f" → {escape(o['recipient'])}" if o["recipient"] else ""
        lines.append(
            f"<b>#{o['id']}</b> {escape(o['title'])}{to}\n"
            f"   {STATUS_LABEL.get(o['status'], o['status'])} | {fmt_toman(o['price'])} | {o['created_at'][:16].replace('T', ' ')}")
    await message.answer("\n".join(lines))


@router.message(F.text == BTN_SUPPORT)
async def support(message: Message, state: FSMContext, db: Database):
    await state.clear()
    text = await db.get_setting("support_text", DEFAULT_SUPPORT)
    await message.answer(f"🆘 <b>پشتیبانی</b>\n\n{escape(text)}")
