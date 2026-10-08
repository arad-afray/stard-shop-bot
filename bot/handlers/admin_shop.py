"""مدیریت فروشگاه و بازاریابی در پنل: دکمه‌ها، Feature Flag، قیمت‌گذاری پویا و Flash Sale، موجودی و محدودیت،
VIP، پاداش روزانه و گردونه، Test Mode، دسته‌های Stard، کاربران غیرفعال، اعلان‌های هوشمند، ریسک و Ban خودکار،
و ابزارهای کارت کاربر (پیشنهاد اختصاصی، VIP، ریسک)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import notify
from ..db import Database, ago, now, today, ts
from ..features import CATEGORY_FLAG, FLAGS, ROLLOUTS, describe_flags
from ..pricing import CATEGORIES, fmt_toman, to_float, to_int
from ..queue import JobQueue
from ..risk import EVENTS, RiskEngine
from ..shop import Shop
from ..stard_api import StardError
from ..ui import Adm, SA, back_to, cancel_menu, edit_or_send, main_menu, section
from ..worker import start_broadcast
from .filters import IsAdmin

log = logging.getLogger(__name__)
router = Router(name="admin_shop")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

MENU, MKT = SA(a="menu"), SA(a="mkt")
NOTIFY_KINDS = {
    "order_success": "✅ سفارش موفق (به کاربر)",
    "order_failed": "❌ سفارش ناموفق (به کاربر)",
    "refund": "↩️ برگشت پول (به کاربر)",
    "low_stock": "📦 موجودی کم (به مدیر)",
    "discount": "🎟 پیشنهاد/تخفیف اختصاصی (به کاربر)",
    "comeback": "💤 دعوت به بازگشت کاربر غیرفعال",
    "offer_ending": "⏰ یادآوری پایان پیشنهاد (به کاربر)",
    "maintenance": "🧹 اطلاع حالت تعمیر (کانال گزارش)",
    "update": "🔄 نتیجه‌ی به‌روزرسانی (به مدیر)",
}
INACTIVE_BUCKETS = (7, 14, 30, 60)


class ShopForm(StatesGroup):
    rule = State()
    flash = State()
    control = State()
    vip_level = State()
    vip_assign = State()
    offer = State()
    reward_amount = State()
    spin = State()
    slug = State()
    campaign = State()
    comeback = State()
    risk = State()


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse_when(token: str | None, base: datetime | None = None) -> str | None:
    """now | +6h | +2d | +30m | YYYY-MM-DD | YYYY-MM-DDTHH:MM  → ISO UTC؛ «-» یا None یعنی نامحدود."""
    if not token or token in ("-", "none"):
        return None
    base = base or _now_dt()
    if token == "now":
        return ts(base)
    m = re.fullmatch(r"\+(\d+)([mhd])", token)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return ts(base + timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: n}))
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return ts(datetime.strptime(token, fmt).replace(tzinfo=timezone.utc))
        except ValueError:
            pass
    raise ValueError(token)


# ---------- منوها ----------
@router.callback_query(SA.filter(F.a == "menu"))
async def shop_menu(cb: CallbackQuery):
    await edit_or_send(cb.message, "🛍 <b>مدیریت فروشگاه</b>", section([
        ("🎛 مدیریت دکمه‌ها", SA(a="btn")), ("🚩 روشن/خاموش قابلیت‌ها", SA(a="flags")),
        ("🏷 قیمت‌گذاری و حراج", SA(a="rules")), ("📦 موجودی و محدودیت خرید", SA(a="ctl")),
        ("👑 VIP", SA(a="vip")), ("🎟 کد تخفیف", Adm(name="coupons")),
        ("🎁 پاداش روزانه و گردونه", SA(a="rew")), ("🧪 حالت آزمایشی", SA(a="tm")),
        ("🔗 دسته‌های Stard", SA(a="slug")), ("💰 درصد سود", Adm(name="profit")),
    ]))
    await cb.answer()


@router.callback_query(SA.filter(F.a == "mkt"))
async def marketing_menu(cb: CallbackQuery):
    await edit_or_send(cb.message, "📣 <b>بازاریابی و کاربران</b>", section([
        ("📢 پیام همگانی", Adm(name="broadcast")), ("💤 کاربران غیرفعال", SA(a="inact")),
        ("🎁 زیرمجموعه‌گیری", Adm(name="ref")), ("📣 جوین اجباری", Adm(name="join")),
        ("💹 قیمت در گروه", Adm(name="group")), ("🔔 اعلان‌های هوشمند", SA(a="ntf")),
        ("🛡 ریسک و مسدودسازی خودکار", SA(a="risk")),
    ]))
    await cb.answer()


# ---------- مدیریت دکمه‌ها ----------
async def buttons_view(cb: CallbackQuery, shop: Shop):
    btns = await shop.features.buttons()
    b = InlineKeyboardBuilder()
    for x in btns:
        k = x["key"]
        b.button(text=CATEGORIES[k][:18], callback_data=SA(a="bt", v=f"{k}|i"))
        b.button(text="🟢" if x["enabled"] else "🔴", callback_data=SA(a="bt", v=f"{k}|e"))
        b.button(text="👁️" if x["visible"] else "🙈", callback_data=SA(a="bt", v=f"{k}|v"))
        b.button(text="⬆️", callback_data=SA(a="bt", v=f"{k}|u"))
        b.button(text="⬇️", callback_data=SA(a="bt", v=f"{k}|d"))
    b.adjust(*([5] * len(btns)))
    await edit_or_send(cb.message, "🎛 <b>مدیریت دکمه‌های فروشگاه</b>\n\n🟢 فعال / 🔴 غیرفعال (دیده می‌شود با 🔒 ولی خریدنی "
                                   "نیست)\n👁️ نمایش / 🙈 مخفی\n⬆️⬇️ ترتیب\n\nبخشی که قابلیتش خاموش باشد، هرگز "
                                   "نمایش داده نمی‌شود (🚩 روشن/خاموش قابلیت‌ها).", back_to(MENU, b))
    await cb.answer()


@router.callback_query(SA.filter(F.a == "btn"))
async def buttons(cb: CallbackQuery, shop: Shop):
    await buttons_view(cb, shop)


@router.callback_query(SA.filter(F.a == "bt"))
async def button_action(cb: CallbackQuery, callback_data: SA, shop: Shop):
    key, op = callback_data.v.split("|", 1)
    if key not in CATEGORY_FLAG:
        await cb.answer()
        return
    f = shop.features
    if op == "e":
        await f.toggle_button(key, "enabled", admin_id=cb.from_user.id)
    elif op == "v":
        await f.toggle_button(key, "visible", admin_id=cb.from_user.id)
    elif op in ("u", "d"):
        await f.move_button(key, -1 if op == "u" else 1, admin_id=cb.from_user.id)
    elif op == "i":
        on, pct = (await f.all())[CATEGORY_FLAG[key]]
        await cb.answer(f"{CATEGORIES[key]}\nقابلیت: {'روشن' if on else 'خاموش'} ({pct}%)", show_alert=True)
        return
    await buttons_view(cb, shop)


# ---------- Feature Flags ----------
async def _flags_view(cb: CallbackQuery, shop: Shop):
    b = InlineKeyboardBuilder()
    lines = ["🚩 <b>روشن/خاموش قابلیت‌ها</b>\n", "روشن/خاموش و درصد کاربرانی که می‌بینند (هر کاربر با hash ثابت همیشه داخل یا بیرون است).\n"]
    for key, label, on, pct in describe_flags(await shop.features.all()):
        lines.append(f"{'🟢' if on else '🔴'} {label} <code>{key}</code> — {pct}%")
        b.button(text=f"{'🟢' if on else '🔴'} {label}", callback_data=SA(a="fl", v=f"{key}|t"))
        b.button(text=f"{pct}%", callback_data=SA(a="fl", v=f"{key}|r"))
    b.adjust(*([2] * len(FLAGS)))
    await edit_or_send(cb.message, "\n".join(lines), back_to(MENU, b))
    await cb.answer()


@router.callback_query(SA.filter(F.a == "flags"))
async def flags(cb: CallbackQuery, shop: Shop):
    await _flags_view(cb, shop)


@router.callback_query(SA.filter(F.a == "fl"))
async def flag_action(cb: CallbackQuery, callback_data: SA, shop: Shop):
    key, op = callback_data.v.split("|", 1)
    if key not in FLAGS:
        await cb.answer()
        return
    on, pct = (await shop.features.all())[key]
    if op == "t":
        await shop.features.set(key, enabled=not on, admin_id=cb.from_user.id)
    else:
        nxt = ROLLOUTS[(ROLLOUTS.index(pct) + 1) % len(ROLLOUTS)] if pct in ROLLOUTS else 100
        await shop.features.set(key, rollout=nxt, admin_id=cb.from_user.id)
    await _flags_view(cb, shop)


# ---------- قیمت‌گذاری ----------
async def _rules_view(shop: Shop) -> tuple[str, object]:
    rules = await shop.commerce.rules()
    t = now()
    lines = ["🏷 <b>قیمت‌گذاری پویا و حراج</b>\n",
             "قیمت = سود بخش ± قانون‌های فعال − تخفیف VIP؛ هرگز کمتر از قیمت خرید از Stard.\n"]
    b = InlineKeyboardBuilder()
    b.button(text="⚡️ حراج سریع", callback_data=SA(a="flash"))
    b.button(text="➕ قانون قیمت", callback_data=SA(a="rule+"))
    for r in rules:
        live = r["active"] and (not r["starts_at"] or r["starts_at"] <= t) and (not r["ends_at"] or r["ends_at"] > t)
        status = "🟢 فعال" if live else ("⏸ خاموش" if not r["active"] else ("⏳ زمان‌بندی‌شده" if r["starts_at"] and
                                                                          r["starts_at"] > t else "⌛️ تمام‌شده"))
        lines.append(f"#{r['id']} <b>{escape(r['name'])}</b> {r['percent']:+g}% | "
                     f"{CATEGORIES.get(r['category'], 'همه')} | {r['segment']} | {status}\n"
                     f"   {(r['starts_at'] or 'از الان')[:16]} → {(r['ends_at'] or 'نامحدود')[:16]}")
        b.button(text=f"{'⏸' if r['active'] else '▶️'} #{r['id']}", callback_data=SA(a="rt", v=str(r["id"])))
        b.button(text=f"🗑 #{r['id']}", callback_data=SA(a="rd", v=str(r["id"])))
    if not rules:
        lines.append("— قانونی تعریف نشده —")
    b.adjust(2)
    return "\n".join(lines)[:4000], back_to(MENU, b)


@router.callback_query(SA.filter(F.a == "rules"))
async def rules(cb: CallbackQuery, shop: Shop):
    text, kb = await _rules_view(shop)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(SA.filter(F.a.in_({"rt", "rd"})))
async def rule_action(cb: CallbackQuery, callback_data: SA, shop: Shop):
    rid = int(callback_data.v)
    if callback_data.a == "rt":
        await shop.commerce.toggle_rule(rid, admin_id=cb.from_user.id)
    else:
        await shop.commerce.delete_rule(rid, admin_id=cb.from_user.id)
    text, kb = await _rules_view(shop)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("انجام شد")


@router.callback_query(SA.filter(F.a == "flash"))
async def flash_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopForm.flash)
    await cb.message.answer("⚡️ <b>حراج</b>: به شکل <code>بخش درصد_تخفیف ساعت</code> بفرستید.\n"
                            "مثال: <code>stars 10 6</code> = ۱۰٪ تخفیف استارز برای ۶ ساعت از همین الان\n"
                            "بخش‌ها: " + ", ".join(f"<code>{k}</code>" for k in CATEGORIES) + ", <code>all</code>",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.flash)
async def flash_set(message: Message, state: FSMContext, shop: Shop):
    p = (message.text or "").split()
    pct, hours = (to_float(p[1]) if len(p) > 1 else None), (to_int(p[2]) if len(p) > 2 else None)
    if len(p) != 3 or (p[0] not in CATEGORIES and p[0] != "all") or pct is None or not 0 < pct <= 90 or not hours:
        await message.answer("❗️ مثال: stars 10 6")
        return
    rid = await shop.commerce.add_rule(name=f"حراج {p[0]} {pct:g}%", percent=-pct,
                                       category=None if p[0] == "all" else p[0], starts_at=now(),
                                       ends_at=parse_when(f"+{hours}h"), admin_id=message.from_user.id)
    await state.set_state(None)
    await message.answer(f"⚡️ حراج #{rid} فعال شد: {pct:g}% تخفیف تا {hours} ساعت.", reply_markup=main_menu(True))


@router.callback_query(SA.filter(F.a == "rule+"))
async def rule_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopForm.rule)
    await cb.message.answer(
        "➕ <b>قانون قیمت</b> (قیمت‌گذاری زمان‌بندی‌شده / گروه کاربری):\n"
        "<code>نام درصد بخش گروه شروع پایان</code>\n\n"
        "• درصد: منفی = تخفیف، مثبت = افزایش (مثلاً -5 یا 3)\n"
        "• بخش: " + ", ".join(CATEGORIES) + " یا all\n"
        "• گروه: all | vip | vip:2 | new (کاربران ۷ روز اخیر)\n"
        "• شروع/پایان: now، +6h، +2d، 2026-10-10، 2026-10-10T18:00 یا - (نامحدود)\n\n"
        "مثال: <code>Yalda -8 star_gift all 2026-12-20 2026-12-22</code>\n"
        "مثال: <code>VIPstars -3 stars vip now -</code>", reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.rule)
async def rule_add(message: Message, state: FSMContext, shop: Shop):
    p = (message.text or "").split()
    try:
        if len(p) < 4:
            raise ValueError
        name, pct, cat, seg = p[0], to_float(p[1]), p[2], p[3]
        if pct is None or (cat not in CATEGORIES and cat != "all"):
            raise ValueError
        start = parse_when(p[4]) if len(p) > 4 else None
        end = parse_when(p[5], datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                         if start else None) if len(p) > 5 else None
        if start and end and end <= start:
            raise ValueError
        rid = await shop.commerce.add_rule(name=name, percent=pct, category=None if cat == "all" else cat, segment=seg,
                                           starts_at=start, ends_at=end, admin_id=message.from_user.id)
    except ValueError:
        await message.answer("❗️ قالب نادرست است. مثال: Yalda -8 star_gift all 2026-12-20 2026-12-22")
        return
    await state.set_state(None)
    await message.answer(f"✅ قانون #{rid} ثبت شد.", reply_markup=main_menu(True))
    text, kb = await _rules_view(shop)
    await message.answer(text, reply_markup=kb)


# ---------- موجودی، سقف خرید، نمایش، زبان ----------
async def _controls_view(db: Database, shop: Shop) -> tuple[str, object]:
    rows = await shop.commerce.controls()
    th = to_int(await db.get_setting("low_stock_threshold")) or 50
    lines = ["📦 <b>موجودی و محدودیت خرید</b>\n",
             "کلید = بخش (مثل <code>stars</code>) یا یک محصول (<code>star_gift:12577</code>).\n"
             f"هشدار موجودی کم: {th:,}\n"]
    b = InlineKeyboardBuilder()
    b.button(text="✏️ تنظیم", callback_data=SA(a="ctl+"))
    for r in rows:
        left = "∞" if r["stock"] is None else f"{r['stock'] - r['sold']:,}/{r['stock']:,}"
        lines.append(f"• <code>{escape(r['key'])}</code> موجودی: {left} | فروخته: {r['sold']:,} | سقف روزانه: "
                     f"{r['daily_limit'] or '—'} | {'🙈 مخفی' if r['hidden'] else '👁️'} | زبان: {r['languages'] or 'همه'}")
        b.button(text=f"🗑 {r['key'][:20]}", callback_data=SA(a="ctld", v=r["key"][:40].replace(":", "|")))
    if not rows:
        lines.append("— محدودیتی تعریف نشده —")
    b.adjust(1)
    return "\n".join(lines)[:4000], back_to(MENU, b)


@router.callback_query(SA.filter(F.a.in_({"ctl", "ctld"})))
async def controls(cb: CallbackQuery, callback_data: SA, db: Database, shop: Shop):
    if callback_data.a == "ctld":
        await shop.commerce.delete_control(callback_data.v.replace("|", ":"), admin_id=cb.from_user.id)
    text, kb = await _controls_view(db, shop)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(SA.filter(F.a == "ctl+"))
async def control_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopForm.control)
    await cb.message.answer(
        "✏️ <code>کلید فیلد مقدار</code>\n\n"
        "• <code>stars stock 100000</code> موجودی (- = نامحدود)\n"
        "• <code>stars daily_limit 5000</code> سقف خرید روزانه‌ی هر کاربر (- = بدون سقف)\n"
        "• <code>premium:4518 hidden 1</code> مخفی کردن محصول (0 = نمایش)\n"
        "• <code>stars languages fa,en</code> فقط کاربران با زبان تلگرام فارسی/انگلیسی (- = همه)\n"
        "• <code>stars sold 0</code> صفر کردن شمارنده‌ی فروش\n"
        "• <code>low_stock 100</code> آستانه‌ی هشدار موجودی کم", reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.control)
async def control_set(message: Message, state: FSMContext, db: Database, shop: Shop):
    p = (message.text or "").split()
    if len(p) == 2 and p[0] == "low_stock" and to_int(p[1]) is not None:
        await db.set_setting("low_stock_threshold", to_int(p[1]))
    else:
        if len(p) != 3 or p[1] not in ("stock", "daily_limit", "hidden", "languages", "sold") or \
                not re.fullmatch(r"[a-z_]+(:\d+)?", p[0]) or p[0].split(":")[0] not in CATEGORIES:
            await message.answer("❗️ مثال: stars stock 100000")
            return
        field, raw = p[1], p[2]
        if field == "languages":
            value = None if raw == "-" else ",".join(x.strip().lower() for x in raw.split(",") if x.strip())[:128]
        else:
            value = None if raw == "-" and field in ("stock", "daily_limit") else to_int(raw)
            if value is None and raw != "-":
                await message.answer("❗️ مقدار باید عدد باشد.")
                return
            if field in ("hidden", "sold"):
                value = value or 0
        await shop.commerce.set_control(p[0], admin_id=message.from_user.id, **{field: value})
        shop.clear_cache()
    await state.set_state(None)
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))
    text, kb = await _controls_view(db, shop)
    await message.answer(text, reply_markup=kb)


# ---------- VIP ----------
async def _vip_view(shop: Shop) -> tuple[str, object]:
    stats = await shop.commerce.vip_stats()
    lines = ["👑 <b>سیستم VIP</b>\n", "سطح دستی (از کارت کاربر، با تاریخ پایان) یا خودکار با «حداقل مجموع خرید».\n"]
    b = InlineKeyboardBuilder()
    b.button(text="➕ / ✏️ سطح", callback_data=SA(a="vip+"))
    for lv in stats:
        lines.append(f"<b>{lv['level']}. {escape(lv['name'])}</b> — تخفیف {lv['discount']:g}% | سقف سفارش روزانه: "
                     f"{lv['daily_limit'] or '—'} | خودکار از: {fmt_toman(lv['min_spent']) if lv['min_spent'] else '—'}\n"
                     f"   کاربران دستی: {lv['users']:,} | فروش: {fmt_toman(lv['revenue'])}")
        b.button(text=f"🗑 سطح {lv['level']}", callback_data=SA(a="vipd", v=str(lv["level"])))
    if not stats:
        lines.append("— سطحی تعریف نشده —")
    b.adjust(1)
    return "\n".join(lines), back_to(MENU, b)


@router.callback_query(SA.filter(F.a.in_({"vip", "vipd"})))
async def vip(cb: CallbackQuery, callback_data: SA, shop: Shop):
    if callback_data.a == "vipd":
        await shop.commerce.delete_level(int(callback_data.v), admin_id=cb.from_user.id)
    text, kb = await _vip_view(shop)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(SA.filter(F.a == "vip+"))
async def vip_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopForm.vip_level)
    await cb.message.answer("👑 <code>شماره نام تخفیف% [سقف_سفارش_روزانه|-] [حداقل_خرید_برای_ارتقای_خودکار|-]</code>\n"
                            "مثال: <code>1 Silver 2 - 20000000</code>\nمثال: <code>2 Gold 5 50 -</code>",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.vip_level)
async def vip_set(message: Message, state: FSMContext, shop: Shop):
    p = (message.text or "").split()
    try:
        level, name, disc = int(p[0]), p[1], to_float(p[2])
        dl = None if len(p) < 4 or p[3] == "-" else int(p[3])
        ms = None if len(p) < 5 or p[4] == "-" else to_int(p[4])
        if disc is None:
            raise ValueError
        await shop.commerce.set_level(level, name, disc, dl, ms, admin_id=message.from_user.id)
    except (ValueError, IndexError):
        await message.answer("❗️ مثال: 1 Silver 2 - 20000000 (شماره ۱ تا ۲۰، تخفیف ۰ تا ۹۰)")
        return
    await state.set_state(None)
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))
    text, kb = await _vip_view(shop)
    await message.answer(text, reply_markup=kb)


# ---------- پاداش روزانه و گردونه ----------
@router.callback_query(SA.filter(F.a == "rew"))
async def rewards(cb: CallbackQuery, db: Database, shop: Shop):
    flags = await shop.features.all()
    amount = to_int(await db.get_setting("daily_reward_amount")) or 0
    prizes = await db.get_json("spin_prizes", None)
    from ..features import DEFAULT_SPIN
    prizes = prizes or DEFAULT_SPIN
    total_w = sum(int(p["weight"]) for p in prizes) or 1
    stats = await db.all("SELECT kind, COUNT(*) AS n, COALESCE(SUM(amount), 0) AS s FROM reward_claims WHERE day = :d "
                         "GROUP BY kind", {"d": today()})
    st = {r["kind"]: r for r in stats}
    lines = ["🎁 <b>پاداش روزانه و گردونه</b>\n",
             f"پاداش روزانه: {'🟢' if flags['daily_reward'][0] else '🔴'} {fmt_toman(amount)} | امروز: "
             f"{st.get('daily', {}).get('n', 0)} نفر، {fmt_toman(st.get('daily', {}).get('s', 0))}",
             f"گردونه: {'🟢' if flags['spin'][0] else '🔴'} | امروز: {st.get('spin', {}).get('n', 0)} نفر، "
             f"{fmt_toman(st.get('spin', {}).get('s', 0))}",
             "جایزه‌ها: " + ", ".join(f"{fmt_toman(int(p['amount']))} ({int(p['weight']) * 100 // total_w}%)" for p in prizes),
             f"میانگین هزینه‌ی هر چرخش: {fmt_toman(sum(int(p['amount']) * int(p['weight']) for p in prizes) // total_w)}",
             "\nروشن/خاموش کردن: 🚩 روشن/خاموش قابلیت‌ها (پاداش روزانه و گردونه). هر کاربر روزی یک بار."]
    b = InlineKeyboardBuilder()
    b.button(text="✏️ مبلغ پاداش روزانه", callback_data=SA(a="rewa"))
    b.button(text="✏️ جایزه‌های گردونه", callback_data=SA(a="spin"))
    b.adjust(1)
    await edit_or_send(cb.message, "\n".join(lines), back_to(MENU, b))
    await cb.answer()


@router.callback_query(SA.filter(F.a.in_({"rewa", "spin"})))
async def reward_ask(cb: CallbackQuery, callback_data: SA, state: FSMContext):
    if callback_data.a == "rewa":
        await state.set_state(ShopForm.reward_amount)
        await cb.message.answer("💵 مبلغ پاداش روزانه (تومان) را بفرستید (0 = غیرفعال):", reply_markup=cancel_menu())
    else:
        await state.set_state(ShopForm.spin)
        await cb.message.answer("🎡 جایزه‌ها به شکل <code>مبلغ:وزن</code> با فاصله:\n<code>0:50 1000:30 5000:15 20000:5</code>",
                                reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.reward_amount)
async def reward_amount(message: Message, state: FSMContext, db: Database):
    v = to_int(message.text)
    if v is None or v > 10_000_000:
        await message.answer("❗️ یک عدد بین ۰ تا ۱۰٬۰۰۰٬۰۰۰ بفرستید.")
        return
    await db.set_setting("daily_reward_amount", v)
    await db.audit(admin_id=message.from_user.id, action="reward_set", after={"daily": v})
    await state.set_state(None)
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))


@router.message(ShopForm.spin)
async def spin_set(message: Message, state: FSMContext, db: Database):
    prizes = []
    for tok in (message.text or "").split():
        a, _, w = tok.partition(":")
        if to_int(a) is None or to_int(w) is None:
            await message.answer("❗️ مثال: 0:50 1000:30 5000:15")
            return
        prizes.append({"amount": to_int(a), "weight": to_int(w)})
    if not prizes or not any(p["weight"] for p in prizes) or max(p["amount"] for p in prizes) > 50_000_000:
        await message.answer("❗️ حداقل یک جایزه با وزن مثبت لازم است.")
        return
    await db.set_json("spin_prizes", prizes)
    await db.audit(admin_id=message.from_user.id, action="reward_set", after={"spin": prizes})
    await state.set_state(None)
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))


# ---------- Test Mode ----------
@router.callback_query(SA.filter(F.a.in_({"tm", "tms"})))
async def test_mode(cb: CallbackQuery, callback_data: SA, db: Database, shop: Shop):
    if callback_data.a == "tms" and callback_data.v in ("off", "admins", "all"):
        before = await shop.test_mode()
        await db.set_setting("test_mode", callback_data.v)
        await db.audit(admin_id=cb.from_user.id, action="test_mode", before={"mode": before},
                       after={"mode": callback_data.v})
    mode = await shop.test_mode()
    tests = await db.scalar("SELECT COUNT(*) FROM orders WHERE is_test = 1")
    b = InlineKeyboardBuilder()
    for v, label in (("off", "⚪️ خاموش"), ("admins", "🧪 فقط مدیرها"), ("all", "🧪 همه‌ی کاربران")):
        b.button(text=("• " if v == mode else "") + label, callback_data=SA(a="tms", v=v))
    b.adjust(1)
    await edit_or_send(cb.message,
                       "🧪 <b>حالت آزمایشی</b>\n\nدر حالت آزمایشی سفارش‌ها به جای Stard API با یک شبیه‌ساز داخلی انجام می‌شوند: "
                       "هیچ درخواستی به API واقعی نمی‌رود و هیچ پول واقعی از کیف پول Stard خرج نمی‌شود. کل جریان "
                       "(کسر موجودی داخلی، صف، پیگیری، تحویل بعد از ۱۵ ثانیه، اعلان) مثل واقعی اجرا می‌شود.\n"
                       "سفارش‌های آزمایشی علامت‌دار و از همه‌ی آمار و گزارش‌های مالی حذف‌اند.\n\n"
                       f"وضعیت: <b>{mode}</b> | سفارش‌های آزمایشی: {tests:,}\n\n"
                       "«همه‌ی کاربران» فقط قبل از راه‌اندازی عمومی: کاربران با موجودی داخلی‌شان سفارش آزمایشی می‌گیرند.",
                       back_to(MENU, b))
    await cb.answer()


# ---------- دسته‌های Stard ----------
@router.callback_query(SA.filter(F.a == "slug"))
async def slugs(cb: CallbackQuery, db: Database, shop: Shop):
    from ..shop import CATALOG_CATEGORIES
    try:
        cats = await shop.api.categories()
        remote = ", ".join(f"<code>{escape(str(c.get('slug')))}</code> ({c.get('available_products', '?')})" for c in cats)
    except StardError as e:
        remote = f"⚠️ {escape(e.code)}"
    lines = ["🔗 <b>دسته‌های Stard</b>\n", f"دسته‌های موجود در Stard: {remote}\n", "نگاشت بخش‌های ربات:"]
    for k, default in CATALOG_CATEGORIES.items():
        lines.append(f"• {CATEGORIES[k]} → <code>{escape(await db.get_setting(f'slug:{k}') or default)}</code>")
    b = InlineKeyboardBuilder()
    b.button(text="✏️ تغییر نگاشت", callback_data=SA(a="slug+"))
    await edit_or_send(cb.message, "\n".join(lines), back_to(MENU, b))
    await cb.answer()


@router.callback_query(SA.filter(F.a == "slug+"))
async def slug_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopForm.slug)
    await cb.message.answer("✏️ <code>بخش slug</code> مثلاً <code>username usernames</code>", reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.slug)
async def slug_set(message: Message, state: FSMContext, db: Database, shop: Shop):
    from ..shop import CATALOG_CATEGORIES
    p = (message.text or "").split()
    if len(p) != 2 or p[0] not in CATALOG_CATEGORIES or not re.fullmatch(r"[a-z0-9_-]{2,32}", p[1]):
        await message.answer("❗️ مثال: username usernames")
        return
    await db.set_setting(f"slug:{p[0]}", p[1])
    await db.audit(admin_id=message.from_user.id, action="setting_set", ref=f"slug:{p[0]}", after={"value": p[1]})
    shop.clear_cache()
    await state.set_state(None)
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))


# ---------- کاربران غیرفعال ----------
@router.callback_query(SA.filter(F.a == "inact"))
async def inactive(cb: CallbackQuery, db: Database):
    lines = ["💤 <b>کاربران غیرفعال</b>\n", "کاربرانی که در این مدت با ربات کار نکرده‌اند (به‌جز بلاک‌کرده‌ها):\n"]
    b = InlineKeyboardBuilder()
    for d in INACTIVE_BUCKETS:
        n = await db.count_users(inactive_since=ago(d))
        lines.append(f"• بیش از {d} روز: <b>{n:,}</b> کاربر")
        b.button(text=f"📢 کمپین {d} روز ({n:,})", callback_data=SA(a="camp", v=str(d)))
    b.button(text="💤 دعوت خودکار به بازگشت", callback_data=SA(a="cb"))
    b.adjust(2, 2, 1)
    await edit_or_send(cb.message, "\n".join(lines), back_to(MKT, b))
    await cb.answer()


@router.callback_query(SA.filter(F.a == "camp"))
async def campaign_ask(cb: CallbackQuery, callback_data: SA, state: FSMContext):
    await state.set_state(ShopForm.campaign)
    await state.update_data(camp_days=int(callback_data.v))
    await cb.message.answer(f"📢 پیام کمپین برای کاربران غیرفعال بیش از {callback_data.v} روز را بفرستید (متن، عکس، …). "
                            "می‌توانید کد تخفیف هم در آن بگذارید:", reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.campaign)
async def campaign_preview(message: Message, state: FSMContext, db: Database):
    days = (await state.get_data()).get("camp_days", 30)
    await state.update_data(camp_chat=message.chat.id, camp_msg=message.message_id)
    n = await db.count_users(inactive_since=ago(days))
    b = InlineKeyboardBuilder()
    b.button(text=f"✅ ارسال برای {n:,} کاربر", callback_data=SA(a="camp!"))
    b.button(text="❌ انصراف", callback_data=MKT)
    b.adjust(1)
    await message.answer("پیام بالا برای کاربران غیرفعال ارسال شود؟", reply_markup=b.as_markup())


@router.callback_query(SA.filter(F.a == "camp!"))
async def campaign_send(cb: CallbackQuery, state: FSMContext, db: Database, queue: JobQueue = None, limiter=None):
    from ..locks import allow
    data = await state.get_data()
    if "camp_msg" not in data or queue is None:
        await cb.answer("پیامی برای ارسال نیست.", show_alert=True)
        return
    if not await allow(limiter, "broadcast", cb.from_user.id):
        await cb.answer("⏳ سقف پیام همگانی (۳ بار در ساعت) پر شده است.", show_alert=True)
        return
    await state.clear()
    bid = await start_broadcast(db, queue, admin_id=cb.from_user.id, from_chat=data["camp_chat"],
                                message_id=data["camp_msg"], segment=f"inactive:{data.get('camp_days', 30)}")
    await cb.answer("در صف قرار گرفت")
    await cb.message.answer(f"📢 کمپین #{bid} در صف ارسال قرار گرفت.", reply_markup=main_menu(True))


@router.callback_query(SA.filter(F.a.in_({"cb", "cbt"})))
async def comeback(cb: CallbackQuery, callback_data: SA, db: Database):
    cfg = await db.get_json("comeback", {}) or {}
    if callback_data.a == "cbt":
        cfg["enabled"] = not cfg.get("enabled")
        await db.set_json("comeback", cfg)
        await db.audit(admin_id=cb.from_user.id, action="comeback", after=cfg)
    b = InlineKeyboardBuilder()
    b.button(text="🔴 خاموش کردن" if cfg.get("enabled") else "🟢 روشن کردن", callback_data=SA(a="cbt"))
    b.button(text="✏️ روز و متن", callback_data=SA(a="cbe"))
    b.adjust(1)
    await edit_or_send(cb.message, "💤 <b>دعوت خودکار به بازگشت</b>\n\n"
                                   f"وضعیت: {'🟢 روشن' if cfg.get('enabled') else '🔴 خاموش'}\n"
                                   f"بعد از {cfg.get('days', 14)} روز غیرفعالی، یک بار (حداکثر هر ۳۰ روز) این پیام "
                                   f"فرستاده می‌شود:\n\n{escape(cfg.get('text') or DEFAULT_COMEBACK)}",
                       back_to(SA(a="inact"), b))
    await cb.answer()


DEFAULT_COMEBACK = "👋 دلمان برایتان تنگ شده! سری به فروشگاه بزنید؛ قیمت‌ها به‌روز است."


@router.callback_query(SA.filter(F.a == "cbe"))
async def comeback_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopForm.comeback)
    await cb.message.answer("✏️ <code>تعداد_روز متن پیام</code>\nمثال: <code>14 سلام! کد BACK10 برای ۱۰٪ تخفیف</code>",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.comeback)
async def comeback_set(message: Message, state: FSMContext, db: Database):
    days, _, text = (message.text or "").partition(" ")
    if to_int(days) is None or not 3 <= to_int(days) <= 365 or not text.strip():
        await message.answer("❗️ مثال: 14 سلام! سری بزنید")
        return
    cfg = await db.get_json("comeback", {}) or {}
    cfg.update(days=to_int(days), text=text.strip()[:1000])
    await db.set_json("comeback", cfg)
    await state.set_state(None)
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))


# ---------- اعلان‌های هوشمند ----------
@router.callback_query(SA.filter(F.a.in_({"ntf", "ntft"})))
async def notifications(cb: CallbackQuery, callback_data: SA, db: Database):
    off = set(await db.get_json("notify_off", []) or [])
    if callback_data.a == "ntft" and callback_data.v in NOTIFY_KINDS:
        off.symmetric_difference_update({callback_data.v})
        await db.set_json("notify_off", sorted(off))
        await db.audit(admin_id=cb.from_user.id, action="notify_toggle", ref=callback_data.v,
                       after={"enabled": callback_data.v not in off})
    b = InlineKeyboardBuilder()
    for k, label in NOTIFY_KINDS.items():
        b.button(text=f"{'🔕' if k in off else '🔔'} {label}", callback_data=SA(a="ntft", v=k))
    b.adjust(1)
    await edit_or_send(cb.message, "🔔 <b>اعلان‌های هوشمند</b>\n\n🔔 روشن / 🔕 خاموش. هشدارهای فنی سیستم در "
                                   "🛠 سیستم ← 🚨 هشدارها تنظیم می‌شوند.", back_to(MKT, b))
    await cb.answer()


# ---------- ریسک و Ban خودکار ----------
@router.callback_query(SA.filter(F.a.in_({"risk", "riskt", "riskr"})))
async def risk_view(cb: CallbackQuery, callback_data: SA, db: Database, risk: RiskEngine = None):
    risk = risk or RiskEngine(db)
    if callback_data.a == "riskt":
        r = await risk.rules()
        await risk.set_rules(admin_id=cb.from_user.id, enabled=not r["enabled"])
    elif callback_data.a == "riskr" and callback_data.v:
        await risk.reset(int(callback_data.v), admin_id=cb.from_user.id)
    r = await risk.rules()
    lines = ["🛡 <b>ریسک و مسدودسازی خودکار</b>\n",
             f"مسدودسازی خودکار: {'🟢 روشن' if r['enabled'] else '🔴 خاموش'} | آستانه: {r['threshold']} | حداقل انواع رفتار: "
             f"{r['min_kinds']} | بازه: {r['window_hours']} ساعت",
             "کاربر فقط وقتی مسدود می‌شود که هم امتیاز از آستانه بگذرد و هم چند نوع رفتار متفاوت داشته باشد؛ مدیرها هرگز. "
             "شواهد قبل از مسدودسازی در گزارش رویدادها ثبت می‌شود.\n", "<b>وزن رفتارها</b>"]
    lines += [f"• {label}: {w}" for w, label in EVENTS.values()]
    top = await risk.top(10)
    b = InlineKeyboardBuilder()
    b.button(text="🔴 خاموش کردن مسدودسازی خودکار" if r["enabled"] else "🟢 روشن کردن مسدودسازی خودکار", callback_data=SA(a="riskt"))
    b.button(text="✏️ آستانه‌ها", callback_data=SA(a="riske"))
    if top:
        lines.append("\n<b>پرریسک‌ترین کاربران</b>")
        for u in top:
            name = f"@{u['username']}" if u["username"] else (u["first_name"] or "")
            lines.append(f"• <code>{u['id']}</code> {escape(name)}: {u['risk_score']} {'⛔️' if u['banned'] else ''}")
            b.button(text=f"♻️ صفر کردن {u['id']}", callback_data=SA(a="riskr", v=str(u["id"])))
    b.adjust(1)
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MKT, b))
    await cb.answer()


@router.callback_query(SA.filter(F.a == "riske"))
async def risk_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ShopForm.risk)
    await cb.message.answer("✏️ <code>آستانه حداقل_انواع ساعت</code>\nمثال: <code>60 2 24</code>", reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.risk)
async def risk_set(message: Message, state: FSMContext, db: Database, risk: RiskEngine = None):
    p = [to_int(x) for x in (message.text or "").split()]
    if len(p) != 3 or None in p or not (10 <= p[0] <= 1000 and 1 <= p[1] <= 9 and 1 <= p[2] <= 168):
        await message.answer("❗️ مثال: 60 2 24")
        return
    await (risk or RiskEngine(db)).set_rules(admin_id=message.from_user.id, threshold=p[0], min_kinds=p[1],
                                              window_hours=p[2])
    await state.set_state(None)
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))


# ---------- کارت کاربر: پیشنهاد اختصاصی، VIP، ریسک ----------
@router.callback_query(SA.filter(F.a == "uoffer"))
async def offer_ask(cb: CallbackQuery, callback_data: SA, state: FSMContext):
    await state.set_state(ShopForm.offer)
    await state.update_data(offer_uid=int(callback_data.v))
    await cb.message.answer("🎁 <b>پیشنهاد اختصاصی</b>: <code>درصد تعداد_استفاده روز [بخش]</code>\n"
                            "مثال: <code>15 1 3</code> = ۱۵٪ تخفیف یک‌باره، ۳ روز مهلت\n"
                            "مثال: <code>10 2 7 stars</code> = فقط برای استارز", reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.offer)
async def offer_create(message: Message, state: FSMContext, db: Database, bot: Bot):
    import secrets
    p = (message.text or "").split()
    uid = (await state.get_data()).get("offer_uid")
    pct = to_float(p[0]) if p else None
    uses, days = (to_int(p[1]) if len(p) > 1 else None), (to_int(p[2]) if len(p) > 2 else None)
    cat = p[3] if len(p) > 3 else None
    if not uid or pct is None or not 0 < pct <= 90 or not uses or not days or (cat and cat not in CATEGORIES):
        await message.answer("❗️ مثال: 15 1 3")
        return
    code = f"U{uid % 100000}{secrets.token_hex(2).upper()}"
    expires = ts(_now_dt() + timedelta(days=days))
    await db.create_coupon(code, pct, uses, user_id=uid, category=cat, expires_at=expires, admin_id=message.from_user.id)
    await state.set_state(None)
    await message.answer(f"✅ پیشنهاد ساخته شد: <code>{code}</code> ({pct:g}%، تا {expires[:10]})", reply_markup=main_menu(True))
    if await notify.enabled(db, "discount"):
        await notify.safe_send(bot, uid, f"🎁 <b>پیشنهاد اختصاصی برای شما!</b>\n\n{pct:g}% تخفیف"
                                         + (f" برای {CATEGORIES[cat]}" if cat else "") +
                                         f"\nکد: <code>{code}</code>\nمهلت: {days} روز\n\nموقع خرید، در پیش‌فاکتور «🎟 کد تخفیف دارم» را بزنید.")


@router.callback_query(SA.filter(F.a == "uvip"))
async def uvip_ask(cb: CallbackQuery, callback_data: SA, state: FSMContext, shop: Shop):
    levels = await shop.commerce.levels()
    if not levels:
        await cb.answer("اول از 🛍 مدیریت فروشگاه ← 👑 VIP یک سطح بسازید.", show_alert=True)
        return
    await state.set_state(ShopForm.vip_assign)
    await state.update_data(vip_uid=int(callback_data.v))
    await cb.message.answer("👑 <code>شماره_سطح تعداد_روز</code> (روز 0 = دائمی، سطح 0 = حذف VIP)\n"
                            "سطح‌ها: " + ", ".join(f"{lv['level']}={escape(lv['name'])}" for lv in levels),
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(ShopForm.vip_assign)
async def uvip_set(message: Message, state: FSMContext, shop: Shop, bot: Bot):
    p = [to_int(x) for x in (message.text or "").split()]
    uid = (await state.get_data()).get("vip_uid")
    if len(p) != 2 or None in p or not uid:
        await message.answer("❗️ مثال: 1 30")
        return
    try:
        if p[0] == 0:
            await shop.commerce.unassign(uid, admin_id=message.from_user.id)
        else:
            await shop.commerce.assign(uid, p[0], p[1] or None, admin_id=message.from_user.id)
    except ValueError:
        await message.answer("❗️ این سطح وجود ندارد.")
        return
    await state.set_state(None)
    await message.answer("✅ انجام شد.", reply_markup=main_menu(True))
    if p[0]:
        lvl = await shop.commerce.user_level(uid)
        if lvl:
            await notify.safe_send(bot, uid, f"👑 تبریک! سطح شما به <b>{escape(lvl['name'])}</b> ارتقا یافت "
                                             f"({lvl['discount']:g}% تخفیف روی همه‌ی خریدها).")


@router.callback_query(SA.filter(F.a == "urisk"))
async def urisk_reset(cb: CallbackQuery, callback_data: SA, db: Database, risk: RiskEngine = None):
    await (risk or RiskEngine(db)).reset(int(callback_data.v), admin_id=cb.from_user.id)
    await cb.answer("امتیاز ریسک صفر شد", show_alert=True)

