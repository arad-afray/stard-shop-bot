"""بخش مالی پنل: داشبورد درآمد (با نمودار)، داشبورد سود، Ledger، جستجوی تراکنش، مرکز بازپرداخت،
پرداخت‌های ناموفق، گزارش CSV/Excel/PDF و ماشین‌حساب کارمزد."""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import finance
from ..db import Database
from ..pricing import CATEGORIES, fmt_toman, to_float, to_int
from ..ui import STATUS_LABEL, Adm, Fn, Op, back_to, cancel_menu, edit_or_send, main_menu, section
from .filters import IsAdmin

log = logging.getLogger(__name__)
router = Router(name="finance")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

MENU = Fn(a="menu")
KIND = {"topup": "💳 شارژ", "order": "🛒 خرید", "refund": "↩️ برگشت", "admin": "👮 مدیر", "referral": "🎁 زیرمجموعه",
        "reward": "🎡 پاداش"}
SEARCH_HELP = ("فیلترها (هر ترکیبی):\n<code>user:123</code> <code>type:refund</code> <code>order:42</code> "
               "<code>id:15</code> <code>min:100000</code> <code>max:500000</code> <code>from:2026-10-01</code> "
               "<code>to:2026-10-05</code> <code>status:completed</code>\nیا فقط یک عدد (آیدی کاربر/سفارش/تراکنش).")


class FinForm(StatesGroup):
    ledger = State()
    tx = State()
    custom = State()
    fees = State()
    calc = State()


@router.callback_query(Fn.filter(F.a == "menu"))
async def fin_menu(cb: CallbackQuery):
    await edit_or_send(cb.message, "💵 <b>مالی</b>\n\nهمه‌ی ارقام بدون سفارش‌های آزمایشی.", section([
        ("📈 داشبورد درآمد", Fn(a="rev", v="daily")), ("💰 داشبورد سود", Fn(a="profit", v="month")),
        ("📒 دفتر کل", Fn(a="ledger")), ("🔎 جستجوی تراکنش", Fn(a="tx")),
        ("↩️ مرکز برگشت پول", Fn(a="ref")), ("⚠️ پرداخت‌های ناموفق", Fn(a="failed")),
        ("📄 گزارش مالی", Fn(a="rep")), ("🧮 ماشین‌حساب کارمزد", Fn(a="fee")),
    ]))
    await cb.answer()


# ---------- درآمد ----------
@router.callback_query(Fn.filter(F.a == "rev"))
async def revenue(cb: CallbackQuery, callback_data: Fn, db: Database):
    gran = callback_data.v if callback_data.v in ("daily", "weekly", "monthly") else "daily"
    d = await finance.revenue_dashboard(db)
    pts = await finance.series(db, gran, {"daily": 14, "weekly": 8, "monthly": 6}[gran])
    title = {"daily": "روزانه (۱۴ روز)", "weekly": "هفتگی (۸ هفته)", "monthly": "ماهانه (۶ ماه)"}[gran]
    lines = ["📈 <b>داشبورد درآمد</b>\n"]
    for key, label in (("today", "امروز"), ("week", "۷ روز"), ("month", "۳۰ روز")):
        cur = d[key]["cur"]
        lines.append(f"<b>{label}</b>: {fmt_toman(cur.revenue)} | {cur.orders:,} سفارش | میانگین {fmt_toman(cur.aov)} | "
                     f"رشد {d[key]['growth']}")
    t = d["total"]
    lines.append(f"<b>کل</b>: {fmt_toman(t.revenue)} | {t.orders:,} سفارش | میانگین سفارش {fmt_toman(t.aov)}")
    lines.append(f"\n<b>نمودار درآمد {title}</b> (تومان)")
    lines.append(finance.text_chart([(lbl, rev) for lbl, rev, _ in pts]))
    lines.append(f"\n<b>نمودار سود ناخالص {title}</b> (تومان)")
    lines.append(finance.text_chart([(lbl, prof) for lbl, _, prof in pts]))
    b = InlineKeyboardBuilder()
    for g, label in (("daily", "روزانه"), ("weekly", "هفتگی"), ("monthly", "ماهانه")):
        b.button(text=("• " if g == gran else "") + label, callback_data=Fn(a="rev", v=g))
    b.adjust(3)
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU, b))
    await cb.answer()


# ---------- سود ----------
@router.callback_query(Fn.filter(F.a == "profit"))
async def profit(cb: CallbackQuery, callback_data: Fn, db: Database):
    period = callback_data.v if callback_data.v in ("day", "week", "month", "total") else "month"
    if period == "total":
        a, b, label = "0000", "9999", "کل"
    else:
        a, b, label = finance.period_range(period)
    s = await finance.summary(db, a, b)
    f = await finance.fees(db)
    text = (f"💰 <b>داشبورد سود</b> — {label}\n\n"
            f"درآمد ناخالص: <b>{fmt_toman(s.revenue)}</b>\n"
            f"هزینه‌ی خرید از Stard: {fmt_toman(s.cost)}\n"
            f"کارمزدها: {fmt_toman(s.fees)}  "
            f"<i>(تلگرام {f['telegram_percent']}% + API {f['api_percent']}% + پرداخت {f['payment_percent']}% + "
            f"{int(f['payment_fixed']):,} ثابت)</i>\n"
            f"پرداختی (زیرمجموعه و پاداش): {fmt_toman(s.payouts)}\n"
            f"برگشتی (جدا از درآمد): {fmt_toman(s.refunds)} در {s.refunded_orders:,} سفارش\n"
            f"──────────\n"
            f"سود ناخالص: {fmt_toman(s.gross_profit)}\n"
            f"<b>سود خالص واقعی: {fmt_toman(s.net_profit)}</b>\n"
            f"حاشیه‌ی سود: {s.margin}%")
    cats = await db.all("SELECT category, COALESCE(SUM(price), 0) AS rev, COALESCE(SUM(price - base_amount), 0) AS p, "
                        "COUNT(*) AS n FROM orders WHERE status = 'completed' AND is_test = 0 AND created_at >= :a "
                        "AND created_at < :b GROUP BY category ORDER BY p DESC", {"a": a, "b": b})
    if cats:
        text += "\n\n<b>سود به تفکیک بخش</b>\n" + "\n".join(
            f"{CATEGORIES.get(c['category'], c['category'])}: {fmt_toman(c['p'])} از {fmt_toman(c['rev'])} ({c['n']:,})"
            for c in cats)
    kb = InlineKeyboardBuilder()
    for p, lbl in (("day", "امروز"), ("week", "۷ روز"), ("month", "۳۰ روز"), ("total", "کل")):
        kb.button(text=("• " if p == period else "") + lbl, callback_data=Fn(a="profit", v=p))
    kb.adjust(4)
    await edit_or_send(cb.message, text, back_to(MENU, kb))
    await cb.answer()


# ---------- Ledger ----------
def _ledger_lines(rows: list[dict]) -> list[str]:
    return [f"<code>#{r['id']}</code> {r['created_at'][5:16].replace('T', ' ')} 🆔{r['user_id']} "
            f"{KIND.get(r['kind'], r['kind'])} {'+' if r['amount'] > 0 else '−'}{fmt_toman(abs(r['amount']))}"
            + (f" | {escape(r['ref'])}" if r["ref"] else "") for r in rows]


async def _ledger_view(state: FSMContext, db: Database, page: int = 0) -> tuple[str, object]:
    q = (await state.get_data()).get("ledger_q", "")
    rows = await finance.ledger_search(db, q, limit=20, offset=page * 20)
    lines = ["📒 <b>دفتر کل</b> — همه‌ی تغییرات موجودی" + (f"\nفیلتر: <code>{escape(q)}</code>" if q else "") + "\n"]
    lines += _ledger_lines(rows) or ["— موردی نیست —"]
    b = InlineKeyboardBuilder()
    b.button(text="🔎 جستجو / فیلتر", callback_data=Fn(a="lq"))
    if q:
        b.button(text="♻️ حذف فیلتر", callback_data=Fn(a="lx"))
    if page > 0:
        b.button(text="◀️ جدیدتر", callback_data=Fn(a="ledger", v=str(page - 1)))
    if len(rows) == 20:
        b.button(text="قدیمی‌تر ▶️", callback_data=Fn(a="ledger", v=str(page + 1)))
    b.adjust(2)
    return "\n".join(lines)[:4000], back_to(MENU, b)


@router.callback_query(Fn.filter(F.a.in_({"ledger", "lx"})))
async def ledger(cb: CallbackQuery, callback_data: Fn, state: FSMContext, db: Database):
    if callback_data.a == "lx":
        await state.update_data(ledger_q="")
    text, kb = await _ledger_view(state, db, int(callback_data.v or 0) if callback_data.a == "ledger" else 0)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Fn.filter(F.a == "lq"))
async def ledger_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(FinForm.ledger)
    await cb.message.answer("🔎 " + SEARCH_HELP, reply_markup=cancel_menu())
    await cb.answer()


@router.message(FinForm.ledger)
async def ledger_query(message: Message, state: FSMContext, db: Database):
    await state.set_state(None)
    await state.update_data(ledger_q=(message.text or "")[:200])
    await message.answer("✅", reply_markup=main_menu(True))
    try:
        text, kb = await _ledger_view(state, db)
    except ValueError:
        await message.answer("❗️ فیلتر نامعتبر است (تاریخ به شکل 2026-10-01).")
        return
    await message.answer(text, reply_markup=kb)


# ---------- جستجوی تراکنش ----------
@router.callback_query(Fn.filter(F.a == "tx"))
async def tx_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(FinForm.tx)
    await cb.message.answer("🔎 <b>جستجوی تراکنش</b> (سفارش‌ها + دفتر کل)\n" + SEARCH_HELP, reply_markup=cancel_menu())
    await cb.answer()


@router.message(FinForm.tx)
async def tx_search(message: Message, state: FSMContext, db: Database):
    q = (message.text or "")[:200]
    try:
        orders = await finance.order_search(db, q)
        led = await finance.ledger_search(db, q, limit=15)
    except ValueError:
        await message.answer("❗️ فیلتر نامعتبر است (تاریخ به شکل 2026-10-01).")
        return
    await state.set_state(None)
    lines = [f"🔎 نتیجه‌ی <code>{escape(q)}</code>\n", "<b>سفارش‌ها</b>"]
    lines += [f"#{o['id']} 🆔{o['user_id']} {escape(o['title'][:30])} | {STATUS_LABEL.get(o['status'], o['status'])} | "
              f"{fmt_toman(o['price'])} | {o['created_at'][:16].replace('T', ' ')}" for o in orders] or ["—"]
    lines += ["", "<b>تراکنش‌ها (دفتر کل)</b>"] + (_ledger_lines(led) or ["—"])
    await message.answer("\n".join(lines)[:4000], reply_markup=main_menu(True))


# ---------- Refund Center ----------
@router.callback_query(Fn.filter(F.a == "ref"))
async def refund_center(cb: CallbackQuery, callback_data: Fn, db: Database):
    status = callback_data.v if callback_data.v in ("pending", "completed", "failed") else None
    counts = await finance.refund_counts(db)
    rows = await finance.refunds(db, status, 15)
    head = " | ".join(f"{lbl}: {counts.get(k, {}).get('n', 0):,} ({fmt_toman(counts.get(k, {}).get('amount', 0))})"
                      for k, lbl in (("pending", "⏳ در انتظار"), ("completed", "✅ انجام‌شده"), ("failed", "❌ ناموفق")))
    lines = ["↩️ <b>مرکز برگشت پول</b>\n", head, "\nهر سفارش حداکثر یک بار برگشت می‌خورد (کلید یکتای سفارش).\n"]
    for r in rows:
        icon = {"pending": "⏳", "completed": "✅", "failed": "❌"}.get(r["status"], "•")
        lines.append(f"{icon} سفارش #{r['order_id']} 🆔{r['user_id']} {fmt_toman(r['amount'])} | "
                     f"{r['updated_at'][:16].replace('T', ' ')}\n   دلیل: {escape(r['reason'] or '—')}"
                     + (f" | {'👮 ' + str(r['admin_id']) if r['admin_id'] else '🤖 خودکار'}")
                     + (f"\n   ⚠️ {escape(r['error'])}" if r["error"] else ""))
    if not rows:
        lines.append("— موردی نیست —")
    b = InlineKeyboardBuilder()
    for k, lbl in ((None, "همه"), ("pending", "⏳"), ("completed", "✅"), ("failed", "❌")):
        b.button(text=("• " if k == status else "") + lbl, callback_data=Fn(a="ref", v=k or ""))
    for r in rows:
        if r["status"] in ("pending", "failed"):
            b.button(text=f"🔁 سفارش #{r['order_id']}", callback_data=Adm(name="ocard", arg=str(r["order_id"])))
    b.adjust(4, 2)
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU, b))
    await cb.answer()


# ---------- Failed Payments ----------
@router.callback_query(Fn.filter(F.a == "failed"))
async def failed_payments(cb: CallbackQuery, db: Database):
    rows = await finance.failed_payments(db)
    lines = ["⚠️ <b>پرداخت‌های ناموفق</b>\n",
             "سفارش‌هایی که Stard رد کرد (پول کاربر برگشته) یا ارسالشان هنوز ناموفق است. «تلاش دوباره» فقط برای کارهای متوقف‌شده "
             "و با همان کلید یکتا انجام می‌شود، پس هرگز دو بار خرید نمی‌شود.\n"]
    b = InlineKeyboardBuilder()
    for o in rows:
        retry = "🔁 در صف تلاش دوباره" if o["status"] == "new" else ("☠️ dead" if o.get("dead_job") else "—")
        lines.append(f"#{o['id']} 🆔{o['user_id']} {escape(o['title'][:28])} | {fmt_toman(o['price'])}\n"
                     f"   علت/خطای API: <code>{escape(o['failure_reason'] or '')}</code> | {o['updated_at'][:16]} | "
                     f"تلاش دوباره: {retry}")
        if o.get("dead_job"):
            b.button(text=f"🔁 تلاش دوباره #{o['id']}", callback_data=Op(a="qr", v=str(o["dead_job"])))
    if not rows:
        lines.append("✅ موردی نیست.")
    b.adjust(2)
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU, b))
    await cb.answer()


# ---------- گزارش ----------
@router.callback_query(Fn.filter(F.a == "rep"))
async def reports(cb: CallbackQuery, callback_data: Fn, state: FSMContext):
    period = callback_data.v or "month"
    b = InlineKeyboardBuilder()
    for p, lbl in (("day", "روزانه"), ("week", "هفتگی"), ("month", "ماهانه")):
        b.button(text=("• " if p == period else "") + lbl, callback_data=Fn(a="rep", v=p))
    b.button(text="📅 بازه‌ی دلخواه", callback_data=Fn(a="repc"))
    for fmt in ("csv", "xlsx", "pdf"):
        b.button(text=f"⬇️ {fmt.upper()}", callback_data=Fn(a="repx", v=f"{period}|{fmt}"))
    b.adjust(3, 1, 3)
    custom = (await state.get_data()).get("rep_custom")
    await edit_or_send(cb.message, "📄 <b>گزارش مالی</b>\n\nبازه را انتخاب کنید و قالب را بزنید.\n"
                                   "CSV: همه‌ی سفارش‌ها و تراکنش‌ها | اکسل: خلاصه + سفارش‌ها + دفتر کل | PDF: خلاصه و روند روزانه"
                                   + (f"\n\nبازه‌ی دلخواه ذخیره‌شده: {custom}" if custom else ""), back_to(MENU, b))
    await cb.answer()


@router.callback_query(Fn.filter(F.a == "repc"))
async def report_custom_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(FinForm.custom)
    await cb.message.answer("📅 بازه را به شکل <code>2026-10-01 2026-10-31</code> بفرستید:", reply_markup=cancel_menu())
    await cb.answer()


@router.message(FinForm.custom)
async def report_custom(message: Message, state: FSMContext):
    p = (message.text or "").split()
    try:
        a, b = (datetime.strptime(x, "%Y-%m-%d").date() for x in (p[0], p[1]))
        if b < a or (b - a).days > 366:
            raise ValueError
    except (ValueError, IndexError):
        await message.answer("❗️ مثال: 2026-10-01 2026-10-31 (حداکثر یک سال)")
        return
    await state.set_state(None)
    await state.update_data(rep_custom=f"{a} {b}")
    kb = InlineKeyboardBuilder()
    for fmt in ("csv", "xlsx", "pdf"):
        kb.button(text=f"⬇️ {fmt.upper()}", callback_data=Fn(a="repx", v=f"custom|{fmt}"))
    await message.answer(f"✅ بازه: {a} تا {b}", reply_markup=main_menu(True))
    await message.answer("قالب را انتخاب کنید:", reply_markup=kb.as_markup())


@router.callback_query(Fn.filter(F.a == "repx"))
async def report_export(cb: CallbackQuery, callback_data: Fn, state: FSMContext, db: Database, bot: Bot):
    period, fmt = callback_data.v.split("|", 1)
    try:
        if period == "custom":
            raw = (await state.get_data()).get("rep_custom", "")
            a_s, b_s = raw.split()
            start, end = (datetime.strptime(x, "%Y-%m-%d").date() for x in (a_s, b_s))
            a, b, label = finance.period_range("custom", start=start, end=end)
        else:
            a, b, label = finance.period_range(period)
    except ValueError:
        await cb.answer("اول بازه‌ی دلخواه را وارد کنید.", show_alert=True)
        return
    await cb.answer("⏳ در حال ساخت گزارش…")
    stamp = label.replace(" … ", "_to_")
    if fmt == "csv":
        data, name = await finance.export_csv(db, a, b), f"stard-report-{stamp}.csv"
    elif fmt == "xlsx":
        data, name = await finance.export_xlsx(db, a, b, label), f"stard-report-{stamp}.xlsx"
    else:
        data, name = await finance.export_pdf(db, a, b, label), f"stard-report-{stamp}.pdf"
    await db.audit(admin_id=cb.from_user.id, action="report_export", ref=f"{fmt}:{label}")
    await bot.send_document(cb.from_user.id, BufferedInputFile(data, filename=name),
                            caption=f"📄 گزارش مالی {label}")


# ---------- ماشین‌حساب کارمزد ----------
@router.callback_query(Fn.filter(F.a == "fee"))
async def fee_view(cb: CallbackQuery, db: Database):
    f = await finance.fees(db)
    b = InlineKeyboardBuilder()
    b.button(text="🧮 محاسبه", callback_data=Fn(a="calc"))
    b.button(text="✏️ تنظیم کارمزدها", callback_data=Fn(a="fees"))
    b.adjust(2)
    await edit_or_send(cb.message, "🧮 <b>ماشین‌حساب کارمزد</b>\n\n"
                                   f"کارمزد تلگرام: {f['telegram_percent']}%\nکارمزد API: {f['api_percent']}%\n"
                                   f"کارمزد درگاه پرداخت: {f['payment_percent']}% + {int(f['payment_fixed']):,} تومان ثابت\n\n"
                                   "این کارمزدها در داشبورد سود و گزارش‌ها هم از سود خالص کم می‌شوند.", back_to(MENU, b))
    await cb.answer()


@router.callback_query(Fn.filter(F.a.in_({"calc", "fees"})))
async def fee_ask(cb: CallbackQuery, callback_data: Fn, state: FSMContext):
    if callback_data.a == "calc":
        await state.set_state(FinForm.calc)
        await cb.message.answer("🧮 <code>مبلغ_فروش قیمت_خرید [درصد_برگشت]</code>\nمثال: <code>2474000 2249000 1</code>",
                                reply_markup=cancel_menu())
    else:
        await state.set_state(FinForm.fees)
        await cb.message.answer("✏️ <code>تلگرام% API% پرداخت% پرداخت_ثابت</code>\nمثال: <code>0 0 1.5 500</code>",
                                reply_markup=cancel_menu())
    await cb.answer()


@router.message(FinForm.fees)
async def fee_set(message: Message, state: FSMContext, db: Database):
    p = [to_float(x) for x in (message.text or "").split()]
    if len(p) != 4 or None in p or any(x < 0 for x in p) or any(x > 50 for x in p[:3]):
        await message.answer("❗️ مثال: 0 0 1.5 500 (درصدها بین ۰ تا ۵۰)")
        return
    before = await finance.fees(db)
    new = {"telegram_percent": p[0], "api_percent": p[1], "payment_percent": p[2], "payment_fixed": int(p[3])}
    await db.set_json("fees", new)
    await db.audit(admin_id=message.from_user.id, action="fees_set", before=before, after=new)
    await state.set_state(None)
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))


@router.message(FinForm.calc)
async def fee_calc(message: Message, state: FSMContext, db: Database):
    p = (message.text or "").split()
    sale, cost = (to_int(p[0]) if p else None), (to_int(p[1]) if len(p) > 1 else None)
    rr = to_float(p[2]) if len(p) > 2 else 0.0
    if sale is None or cost is None or rr is None or not 0 <= rr <= 100:
        await message.answer("❗️ مثال: 2474000 2249000 1")
        return
    r = finance.fee_breakdown(sale, cost, await finance.fees(db), rr)
    await state.set_state(None)
    await message.answer(
        f"🧮 <b>نتیجه</b>\n\nفروش: {fmt_toman(r['sale'])}\nهزینه: {fmt_toman(r['cost'])}\n"
        f"کارمزد تلگرام: {fmt_toman(r['telegram'])}\nکارمزد API: {fmt_toman(r['api'])}\nکارمزد درگاه پرداخت: {fmt_toman(r['payment'])}\n"
        f"برگشتی (ریسک {rr:g}%): {fmt_toman(r['refund'])}\n──────────\n<b>سود خالص: {fmt_toman(r['net'])}</b>\n"
        f"حاشیه‌ی سود: {r['margin']}%", reply_markup=main_menu(True))

