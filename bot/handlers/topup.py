"""شارژ کیف پول با کارت‌به‌کارت: مبلغ ← رسید ← تأیید مدیر."""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from ..db import Database, User
from ..pricing import fmt_toman
from ..ui import BTN_TOPUP, cancel_menu, main_menu, topup_review_menu

log = logging.getLogger(__name__)
router = Router(name="topup")

DEFAULT_MIN_TOPUP = 10_000
MAX_TOPUP = 500_000_000
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class Topup(StatesGroup):
    amount = State()
    receipt = State()


@router.message(F.text == BTN_TOPUP)
async def topup_start(message: Message, state: FSMContext, db: Database):
    card = await db.get_setting("card_number")
    if not card:
        await message.answer("⚠️ شارژ کیف پول هنوز توسط مدیر فعال نشده است.")
        return
    min_amount = int(await db.get_setting("min_topup", DEFAULT_MIN_TOPUP))
    await state.set_state(Topup.amount)
    await message.answer(f"💳 مبلغ شارژ را به <b>تومان</b> بفرستید (حداقل {fmt_toman(min_amount)}):",
                         reply_markup=cancel_menu())


@router.message(Topup.amount)
async def topup_amount(message: Message, state: FSMContext, db: Database):
    text = (message.text or "").translate(PERSIAN_DIGITS).replace(",", "").replace("٬", "").strip()
    min_amount = int(await db.get_setting("min_topup", DEFAULT_MIN_TOPUP))
    if not text.isdigit() or not min_amount <= int(text) <= MAX_TOPUP:
        await message.answer(f"❗️ یک عدد بین {fmt_toman(min_amount)} و {fmt_toman(MAX_TOPUP)} بفرستید.")
        return
    amount = int(text)
    card = await db.get_setting("card_number", "")
    holder = await db.get_setting("card_holder", "")
    await state.update_data(amount=amount)
    await state.set_state(Topup.receipt)
    await message.answer(
        f"مبلغ <b>{fmt_toman(amount)}</b> را به کارت زیر واریز کنید:\n\n"
        f"💳 <code>{escape(card)}</code>\n"
        f"👤 به نام: {escape(holder)}\n\n"
        "📸 سپس <b>عکس رسید</b> را همین‌جا بفرستید.")


@router.message(Topup.receipt, F.photo)
async def topup_receipt(message: Message, state: FSMContext, db: Database, bot: Bot, user: User,
                        admin_ids: list[int], is_admin: bool):
    amount = (await state.get_data())["amount"]
    await state.clear()
    photo_id = message.photo[-1].file_id
    tid = await db.create_topup(user.id, amount, photo_id)
    await message.answer(
        f"✅ رسید شما (درخواست #{tid}) ثبت شد و پس از بررسی، موجودی شارژ می‌شود.",
        reply_markup=main_menu(is_admin))
    uname = f"@{escape(user.username)}" if user.username else "—"
    caption = (f"💳 <b>درخواست شارژ #{tid}</b>\n"
               f"👤 {escape(user.first_name or '')} {uname}\n🆔 <code>{user.id}</code>\n"
               f"💵 مبلغ: <b>{fmt_toman(amount)}</b>")
    for aid in admin_ids:
        try:
            await bot.send_photo(aid, photo_id, caption=caption, reply_markup=topup_review_menu(tid))
        except Exception as e:  # مدیر ربات را استارت نکرده یا بلاک کرده
            log.warning("cannot notify admin %s: %s", aid, e)


@router.message(Topup.receipt)
async def topup_receipt_invalid(message: Message):
    await message.answer("📸 لطفاً عکس رسید را بفرستید (یا «❌ انصراف»).")
