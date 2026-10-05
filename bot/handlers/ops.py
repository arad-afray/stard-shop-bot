"""بخش سیستم پنل مدیریت: مرکز فرمان، Health Check، لاگ‌ها، پشتیبان، به‌روزرسانی، مهاجرت، عیب‌یابی API،
کلیدهای API، وب‌هوک‌ها، صف، اسکنر امنیتی، آمار پایگاه داده، وابستگی‌ها، Audit Log، هشدارها و Maintenance.

عملیات خطرناک (بازگردانی پشتیبان، به‌روزرسانی، مهاجرت، کلید API) فقط برای مالک (ADMIN_IDS) و با تأیید.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from html import escape
from typing import Any

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import __version__, apikeys, notify
from ..backups import BackupError, BackupManager
from ..config import Settings
from ..db import Database
from ..logging_setup import LOG_FILE, read_logs, redact
from ..metrics import human_bytes, human_duration, system_snapshot
from ..migrations_runner import current_revision, head_revision, pending_revisions, upgrade
from ..monitor import ALERT_TEXT, DEFAULT_THRESHOLDS, ICON, business_metrics, format_checks, run_health_check, \
    services_status
from ..pricing import fmt_toman
from ..queue import JobQueue
from ..shop import Shop
from ..systools import SEVERITY, api_diagnostics, db_stats, dependency_report, security_scan
from ..ui import Adm, Op, back_to, cancel_menu, drop_markup, edit_or_send, main_menu, section
from ..updater import Updater, install_mode
from ..worker import Context
from .filters import IsAdmin

log = logging.getLogger(__name__)
router = Router(name="ops")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

HOME = Adm(name="home")
MENU = Op(a="menu")
_update_task: asyncio.Task | None = None


class OpsForm(StatesGroup):
    log_search = State()
    log_range = State()
    key_name = State()
    maint_text = State()
    threshold = State()


def _svc(services: Any, key: str) -> Any:
    return (services.extra.get(key) if services is not None else None)


def _backups(db: Database, settings: Settings, locks: Any, services: Any) -> BackupManager:
    return _svc(services, "backups") or BackupManager(db, settings.backup_dir, settings, locks)


def _updater(db: Database, settings: Settings, locks: Any, services: Any) -> Updater:
    up = _svc(services, "updater")
    if up is None:
        up = Updater(settings, db, _backups(db, settings, locks, services), locks)
        if services is not None:
            services.extra["updater"] = up
    return up


def _ctx(bot: Bot, shop: Shop, db: Database, queue: JobQueue, locks: Any, settings: Settings, services: Any,
         admins: Any = None) -> Context:
    extra = services.extra if services is not None else {}
    return Context(bot=bot, shop=shop, db=db, queue=queue, locks=locks, admins=admins, settings=settings,
                   metrics=extra.get("metrics"), extra=extra)


async def _owner_only(cb: CallbackQuery, is_owner: bool) -> bool:
    if not is_owner:
        await cb.answer("⛔️ فقط مالک ربات (ADMIN_IDS) اجازه‌ی این کار را دارد.", show_alert=True)
        return False
    return True


def _confirm(yes: Op, no: Op, yes_text: str = "✅ بله، انجام بده") -> Any:
    b = InlineKeyboardBuilder()
    b.button(text=yes_text, callback_data=yes)
    b.button(text="❌ انصراف", callback_data=no)
    b.adjust(1)
    return b.as_markup()


# ---------- مرکز فرمان ----------
async def command_center_text(db: Database, shop: Shop, queue: JobQueue | None, settings: Settings,
                              services: Any = None) -> str:
    st = await services_status(db)

    def svc_line(name: str, label: str) -> str:
        inst = st.get(name, [])
        alive = [i for i in inst if i["alive"]]
        if not inst:
            return f"⚪️ {label}: گزارشی نیست"
        return f"{ICON['ok'] if alive else ICON['fail']} {label}: {len(alive)}/{len(inst)} فعال"

    health = await db.get_json("health:last", []) or []
    hmap = {c["name"]: c for c in health}

    def hline(name: str, label: str) -> str:
        c = hmap.get(name)
        return f"{ICON.get(c['status'], '⚪️')} {label}" + (f" ({c['latency_ms']:.0f}ms)" if c and c.get('latency_ms')
                                                           else "") if c else f"⚪️ {label}: بررسی نشده"
    s = await db.stats()
    bm = await business_metrics(db)
    from .. import finance
    a, b, _ = finance.period_range("day")
    today = await finance.summary(db, a, b)
    total = await finance.summary(db, "0000", "9999")
    q = await queue.stats() if queue is not None else None
    maint, _ = await shop.maintenance()
    upd = await db.get_json("update:last_check") or {}
    latest = upd.get("latest") or "؟"
    newer = upd.get("newer")
    bad = [c["name"] for c in health if c.get("status") == "fail"]
    warn = [c["name"] for c in health if c.get("status") == "warn"]
    sys_health = (f"{ICON['fail']} مشکل: {', '.join(bad)}" if bad else
                  f"{ICON['warn']} هشدار: {', '.join(warn)}" if warn else
                  f"{ICON['ok']} سالم" if health else "⚪️ هنوز بررسی نشده (🩺 Health Check)")
    lines = [
        "🛰 <b>STARD COMMAND CENTER</b>",
        f"{'🧹 <b>Maintenance روشن</b> | ' if maint else ''}فروشگاه: {'🟢 باز' if await shop.is_open() else '🔴 بسته'}"
        f" | Test Mode: {await shop.test_mode()}",
        "",
        "<b>سرویس‌ها</b>",
        svc_line("bot", "Bot"), hline("API", "API"), svc_line("worker", "Worker"), hline("Database", "Database"),
        hline("Redis", "Redis"),
        (f"{ICON['ok' if q['lag_seconds'] < 120 else 'warn']} Queue: {q['queued']} در صف | {q['running']} در اجرا | "
         f"dead {q['dead']} | تأخیر {human_duration(q['lag_seconds'])}") if q else "⚪️ Queue",
        "",
        "<b>کسب‌وکار</b>",
        f"👥 کاربران: {s['users']:,} | 🧾 سفارش‌ها: {s['done']:,} انجام | ⏳ {s['active']:,} باز",
        f"💵 درآمد امروز: {fmt_toman(today.revenue)} | کل: {fmt_toman(total.revenue)}",
        f"📈 سود خالص امروز: {fmt_toman(today.net_profit)} | کل: {fmt_toman(total.net_profit)}",
        f"❌ سفارش ناموفق ۲۴ ساعت: {bm['failed_24h']:,} | سفارش/دقیقه: {bm['orders_per_min']} | "
        f"برگشت ۱ ساعت: {bm['refund_rate_1h']}%",
        f"💳 شارژ منتظر: {s['pending_topups']:,} | 🧑‍💻 سفارش دستی منتظر: {s['manual']:,}",
        "",
        f"<b>سلامت سیستم</b>: {sys_health}",
        f"<b>نسخه</b>: v{__version__} | آخرین: v{latest} {'🟡 نسخه‌ی جدید موجود است' if newer else ''}",
    ]
    return "\n".join(lines)


@router.callback_query(Op.filter(F.a == "menu"))
async def ops_menu(cb: CallbackQuery):
    await edit_or_send(cb.message, "🛠 <b>سیستم</b>\n\nابزارهای نگهداری، مانیتورینگ و امنیت:", section([
        ("🩺 Health Check", Op(a="hc")), ("📜 لاگ‌های سیستم", Op(a="logs")),
        ("💾 Backup Manager", Op(a="bk")), ("🔄 بررسی آپدیت پروژه", Op(a="upd")),
        ("🗄 Migration Manager", Op(a="mig")), ("🧪 API Diagnostics", Op(a="diag")),
        ("🔑 API Key Manager", Op(a="keys")), ("🪝 Webhook Monitor", Op(a="wh")),
        ("📬 Queue Monitor", Op(a="q")), ("🛡 Security Scanner", Op(a="sec")),
        ("📊 DB Statistics", Op(a="dbs")), ("📦 Dependency Checker", Op(a="deps")),
        ("📋 Audit Log", Op(a="audit")), ("🚨 هشدارها", Op(a="al")),
        ("🖥 منابع سیستم", Op(a="res")), ("🏦 کیف پول Stard", Adm(name="wallet")),
    ], HOME))
    await cb.answer()


# ---------- Health Check ----------
@router.callback_query(Op.filter(F.a == "hc"))
async def health(cb: CallbackQuery, bot: Bot, shop: Shop, db: Database, settings: Settings, queue: JobQueue = None,
                 locks=None, services=None, admins=None):
    await cb.answer("⏳ در حال بررسی…")
    checks = await run_health_check(_ctx(bot, shop, db, queue or JobQueue(db, settings.instance_id), locks, settings,
                                         services, admins))
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Run Health Check", callback_data=Op(a="hc"))
    await edit_or_send(cb.message, "🩺 <b>Health Check</b>\n\n" + format_checks(checks), back_to(MENU, b))


@router.callback_query(Op.filter(F.a == "res"))
async def resources(cb: CallbackQuery, services=None):
    snap = system_snapshot(".")
    m = _svc(services, "metrics")
    sup = _svc(services, "supervisor")
    lines = ["🖥 <b>منابع سیستم</b>\n",
             f"CPU: {snap.get('cpu_percent', '—')}% ({snap.get('cpu_count', '?')} هسته)"
             + (f" | Load: {', '.join(f'{x:.2f}' for x in snap['load_avg'])}" if snap.get("load_avg") else ""),
             f"RAM: {snap.get('mem_percent', '—')}% ({human_bytes(snap.get('mem_used'))} / "
             f"{human_bytes(snap.get('mem_total'))})",
             f"Disk: {snap.get('disk_percent', '—')}% | آزاد {human_bytes(snap.get('disk_free'))}",
             f"Network: ↑ {human_bytes(snap.get('net_sent'))} ↓ {human_bytes(snap.get('net_recv'))}",
             f"پردازه: RAM {human_bytes(snap.get('proc_rss'))} | Threads {snap.get('proc_threads', '—')}"]
    if m is not None:
        lines.append(f"Uptime: {human_duration(m.uptime())} | آپدیت تلگرام: {int(m.counters.get('telegram_updates_total', 0)):,}"
                     f" | خطا: {int(m.counters.get('errors_total', 0)):,}")
        p95 = m.api_latency.pct(95)
        if p95 is not None:
            lines.append(f"تأخیر API (p95، ۵ دقیقه): {p95 * 1000:.0f}ms | نرخ خطای API: "
                         f"{m.rate('api_errors_total')}/5m")
    if sup is not None:
        lines.append("\n<b>سرویس‌های داخلی</b>")
        for name, status in sup.status.items():
            lines.append(f"{'🟢' if status == 'running' else '🔴'} {name}: {status} | Restart: {sup.restarts.get(name, 0)}")
    await edit_or_send(cb.message, "\n".join(lines), back_to(MENU))
    await cb.answer()


# ---------- لاگ‌ها ----------
LEVELS = ("ALL", "ERROR", "WARNING", "INFO", "DEBUG")
SERVICES = ("all", "bot", "worker", "scheduler")


async def _logs_view(state: FSMContext, settings: Settings) -> tuple[str, Any]:
    f = (await state.get_data()).get("logf") or {}
    level, svc = f.get("level", "ALL"), f.get("service", "all")
    entries = read_logs(settings.log_dir, level=None if level == "ALL" else level, search=f.get("q"),
                        service=None if svc == "all" else svc, since=f.get("since"), until=f.get("until"), limit=15)
    head = (f"📜 <b>لاگ‌های سیستم</b>\nسطح: {level} | سرویس: {svc}"
            + (f" | جستجو: «{escape(f['q'])}»" if f.get("q") else "")
            + (f" | از {f['since'][:10]} تا {f['until'][:10]}" if f.get("since") else "") + "\n")
    lines = [head]
    for e in entries:
        icon = {"ERROR": "🔴", "CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵", "DEBUG": "⚪️"}.get(e.get("level"), "⚪️")
        msg = escape(redact(e.get("msg", ""))[:220])
        lines.append(f"{icon} <code>{e.get('ts', '')[5:19]}</code> [{e.get('service')}] <i>{escape(e.get('cid', ''))}</i>\n"
                     f"   {escape(e.get('logger', ''))}: {msg}")
    if not entries:
        lines.append("— موردی پیدا نشد —")
    b = InlineKeyboardBuilder()
    for lv in LEVELS:
        b.button(text=("• " if lv == level else "") + lv, callback_data=Op(a="logl", v=lv))
    for s in SERVICES:
        b.button(text=("• " if s == svc else "") + s, callback_data=Op(a="logs_svc", v=s))
    b.button(text="🔎 جستجو", callback_data=Op(a="logq"))
    b.button(text="📅 بازه‌ی تاریخ", callback_data=Op(a="logr"))
    b.button(text="♻️ پاک کردن فیلترها", callback_data=Op(a="logx"))
    b.button(text="⬇️ دانلود لاگ", callback_data=Op(a="logd"))
    b.adjust(5, 4, 2, 2)
    text = "\n".join(lines)
    return text[:4000], back_to(MENU, b)


async def _set_log_filter(state: FSMContext, **kw) -> None:
    data = await state.get_data()
    f = dict(data.get("logf") or {})
    f.update(kw)
    await state.update_data(logf={k: v for k, v in f.items() if v is not None})


@router.callback_query(Op.filter(F.a.in_({"logs", "logl", "logs_svc", "logx"})))
async def logs(cb: CallbackQuery, callback_data: Op, state: FSMContext, settings: Settings):
    if callback_data.a == "logl":
        await _set_log_filter(state, level=callback_data.v)
    elif callback_data.a == "logs_svc":
        await _set_log_filter(state, service=callback_data.v)
    elif callback_data.a == "logx":
        await state.update_data(logf={})
    text, kb = await _logs_view(state, settings)
    await edit_or_send(cb.message, text, kb)
    await cb.answer()


@router.callback_query(Op.filter(F.a == "logq"))
async def logs_search_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OpsForm.log_search)
    await cb.message.answer("🔎 متن جستجو (یا Correlation ID مثل u12345) را بفرستید:", reply_markup=cancel_menu())
    await cb.answer()


@router.message(OpsForm.log_search)
async def logs_search(message: Message, state: FSMContext, settings: Settings):
    await _set_log_filter(state, q=(message.text or "").strip()[:100] or None)
    await state.set_state(None)
    await message.answer("✅ فیلتر اعمال شد.", reply_markup=main_menu(True))
    text, kb = await _logs_view(state, settings)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Op.filter(F.a == "logr"))
async def logs_range_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OpsForm.log_range)
    await cb.message.answer("📅 بازه را به شکل <code>2026-10-01 2026-10-05</code> بفرستید (UTC):",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(OpsForm.log_range)
async def logs_range(message: Message, state: FSMContext, settings: Settings):
    parts = (message.text or "").split()
    try:
        a = datetime.strptime(parts[0], "%Y-%m-%d")
        b = datetime.strptime(parts[1] if len(parts) > 1 else parts[0], "%Y-%m-%d") + timedelta(days=1)
    except (ValueError, IndexError):
        await message.answer("❗️ قالب نادرست است. مثال: 2026-10-01 2026-10-05")
        return
    await _set_log_filter(state, since=a.strftime("%Y-%m-%dT00:00:00"), until=b.strftime("%Y-%m-%dT00:00:00"))
    await state.set_state(None)
    await message.answer("✅ بازه اعمال شد.", reply_markup=main_menu(True))
    text, kb = await _logs_view(state, settings)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Op.filter(F.a == "logd"))
async def logs_download(cb: CallbackQuery, bot: Bot, settings: Settings):
    path = os.path.join(settings.log_dir, LOG_FILE)
    if not os.path.exists(path):
        await cb.answer("فایل لاگی نیست.", show_alert=True)
        return
    await cb.answer()
    await bot.send_document(cb.from_user.id, FSInputFile(path, filename=f"stard-logs-{datetime.now():%Y%m%d-%H%M}.jsonl"),
                            caption="📜 لاگ ساختاریافته (JSON Lines). secretها حذف شده‌اند.")


# ---------- Backup Manager ----------
@router.callback_query(Op.filter(F.a == "bk"))
async def backups_view(cb: CallbackQuery, db: Database, settings: Settings, locks=None, services=None):
    mgr = _backups(db, settings, locks, services)
    items = mgr.list()
    lines = ["💾 <b>Backup Manager</b>\n",
             f"پشتیبان خودکار: هر {settings.backup_interval_hours} ساعت | نگه‌داری: {settings.backup_keep} از هر نوع | "
             f"مسیر: <code>{escape(settings.backup_dir)}</code>",
             "قبل از هر به‌روزرسانی، مهاجرت و بازگردانی خودکار پشتیبان گرفته می‌شود.\n"]
    b = InlineKeyboardBuilder()
    b.button(text="➕ Create Backup", callback_data=Op(a="bkc"))
    for it in items[:12]:
        lines.append(f"• <code>{it.name}</code>\n   {it.kind} | {human_bytes(it.size)} | {it.created_at[:16].replace('T', ' ')}"
                     f" | v{it.version} | {it.rows:,} ردیف")
        b.button(text=f"📁 {it.created_at[5:16].replace('T', ' ')} ({it.kind})", callback_data=Op(a="bki", v=it.name[13:]))
    if not items:
        lines.append("— هنوز پشتیبانی ساخته نشده —")
    b.adjust(1)
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU, b))
    await cb.answer()


def _bk_name(v: str) -> str:
    return "stard-backup-" + v


@router.callback_query(Op.filter(F.a == "bkc"))
async def backup_create(cb: CallbackQuery, db: Database, settings: Settings, locks=None, services=None):
    await cb.answer("⏳ در حال ساخت پشتیبان…")
    mgr = _backups(db, settings, locks, services)
    try:
        info = await mgr.create("manual", admin_id=cb.from_user.id)
    except BackupError as e:
        await cb.message.answer(f"❗️ {escape(str(e))}")
        return
    v = mgr.verify(info.name)
    await cb.message.answer(f"✅ پشتیبان ساخته شد: <code>{info.name}</code>\n{human_bytes(info.size)} | {info.rows:,} ردیف"
                            f" | بررسی: {'✅ سالم' if v['ok'] else '❌ ' + escape(v['error'] or '')}")


@router.callback_query(Op.filter(F.a == "bki"))
async def backup_item(cb: CallbackQuery, callback_data: Op, db: Database, settings: Settings, locks=None, services=None):
    mgr = _backups(db, settings, locks, services)
    name = _bk_name(callback_data.v)
    info = next((b for b in mgr.list() if b.name == name), None)
    if info is None:
        await cb.answer("پیدا نشد.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for text, a in (("🔍 Verify", "bkv"), ("🧪 Restore Test", "bkt"), ("⬇️ دانلود", "bkdl"),
                    ("♻️ Restore", "bkr"), ("🗑 Delete", "bkdel")):
        b.button(text=text, callback_data=Op(a=a, v=callback_data.v))
    b.adjust(3, 2)
    await edit_or_send(cb.message,
                       f"📁 <b>{info.name}</b>\n\nنوع: {info.kind}\nتاریخ: {info.created_at}\nحجم: {human_bytes(info.size)}\n"
                       f"نسخه‌ی ربات: v{info.version} | revision: {info.revision}\nردیف‌ها: {info.rows:,}",
                       back_to(Op(a="bk"), b))
    await cb.answer()


@router.callback_query(Op.filter(F.a.in_({"bkv", "bkt"})))
async def backup_verify(cb: CallbackQuery, callback_data: Op, db: Database, settings: Settings, locks=None,
                        services=None):
    mgr = _backups(db, settings, locks, services)
    name = _bk_name(callback_data.v)
    await cb.answer("⏳ …")
    try:
        r = mgr.verify(name) if callback_data.a == "bkv" else await mgr.restore_test(name)
    except BackupError as e:
        r = {"ok": False, "error": str(e)}
    label = "بررسی سلامت" if callback_data.a == "bkv" else "تست بازگردانی (در پایگاه داده‌ی موقت)"
    await cb.message.answer(f"{'✅' if r['ok'] else '❌'} {label}: <code>{name}</code>\n"
                            + (f"ردیف‌ها: {r.get('rows', 0):,}" if r["ok"] else escape(r.get("error") or "")))


@router.callback_query(Op.filter(F.a == "bkdl"))
async def backup_download(cb: CallbackQuery, callback_data: Op, bot: Bot, db: Database, settings: Settings, locks=None,
                          services=None):
    mgr = _backups(db, settings, locks, services)
    name = _bk_name(callback_data.v)
    info = next((b for b in mgr.list() if b.name == name), None)
    if info is None:
        await cb.answer("پیدا نشد.", show_alert=True)
        return
    if info.size > 49 * 1024 * 1024:
        await cb.answer("فایل از ۵۰ مگابایت بزرگ‌تر است؛ از روی سرور بردارید.", show_alert=True)
        return
    await cb.answer()
    await bot.send_document(cb.from_user.id, FSInputFile(info.path, filename=name),
                            caption="💾 این فایل اطلاعات مالی کاربران را دارد؛ جای امن نگه دارید.")


@router.callback_query(Op.filter(F.a.in_({"bkr", "bkdel"})))
async def backup_danger_ask(cb: CallbackQuery, callback_data: Op, is_owner: bool = False):
    if not await _owner_only(cb, is_owner):
        return
    name = _bk_name(callback_data.v)
    if callback_data.a == "bkr":
        text = (f"♻️ <b>بازگردانی</b> <code>{name}</code>\n\nهمه‌ی داده‌های فعلی با این پشتیبان جایگزین می‌شوند. قبل از آن "
                "یک پشتیبان pre-restore خودکار گرفته می‌شود. بازگردانی در یک تراکنش است (همه یا هیچ) و بعد از آن ربات "
                "Restart می‌شود.\n\nمطمئن هستید؟")
        yes = Op(a="bkr!", v=callback_data.v)
    else:
        text = f"🗑 حذف <code>{name}</code>؟ این کار برگشت‌پذیر نیست."
        yes = Op(a="bkdel!", v=callback_data.v)
    await edit_or_send(cb.message, text, _confirm(yes, Op(a="bki", v=callback_data.v)))
    await cb.answer()


@router.callback_query(Op.filter(F.a.in_({"bkr!", "bkdel!"})))
async def backup_danger(cb: CallbackQuery, callback_data: Op, db: Database, shop: Shop, settings: Settings, locks=None,
                        services=None, is_owner: bool = False):
    if not await _owner_only(cb, is_owner):
        return
    mgr = _backups(db, settings, locks, services)
    name = _bk_name(callback_data.v)
    await drop_markup(cb.message)
    if callback_data.a == "bkdel!":
        try:
            await mgr.delete(name, admin_id=cb.from_user.id)
        except BackupError as e:
            await cb.answer(str(e), show_alert=True)
            return
        await cb.answer("حذف شد")
        await cb.message.answer(f"🗑 <code>{name}</code> حذف شد.")
        return
    await cb.answer("⏳ در حال بازگردانی…")
    try:
        r = await mgr.restore(name, admin_id=cb.from_user.id)
    except BackupError as e:
        await cb.message.answer(f"❌ بازگردانی انجام نشد: {escape(str(e))}")
        return
    except Exception as e:
        log.exception("restore failed")
        await cb.message.answer(f"❌ بازگردانی انجام نشد ({escape(type(e).__name__)}). داده‌ها تغییری نکرده‌اند.")
        return
    shop.clear_cache()
    await cb.message.answer(f"✅ بازگردانی کامل شد ({r['rows']:,} ردیف). پشتیبان قبلی: <code>{r['safety_backup']}</code>\n"
                            "ربات برای پاک شدن کش‌ها Restart می‌شود…")
    restart = _svc(services, "request_restart")
    if restart is not None:
        restart()


# ---------- به‌روزرسانی ----------
def _state_text(st: Any) -> str:
    icons = {"ok": "✅", "fail": "❌", "run": "⏳", "skip": "⏭"}
    lines = [f"{icons.get(s[1], '•')} {escape(s[0])}" + (f" — {escape(s[2])}" if s[2] else "") for s in st.steps]
    result = {"running": "⏳ در حال اجرا", "restarting": "🔄 در حال Restart", "success": "✅ موفق",
              "failed": "❌ ناموفق", "rolled_back": "↩️ ناموفق — به نسخه‌ی قبلی برگشت", "idle": "—"}.get(st.status, st.status)
    return ("<b>Update Progress</b>\n" + ("\n".join(lines) or "—") + f"\n\n<b>Update Result</b>: {result}"
            + (f"\n⚠️ {escape(st.error)}" if st.error else ""))


@router.callback_query(Op.filter(F.a == "upd"))
async def update_view(cb: CallbackQuery, db: Database, settings: Settings, locks=None, services=None):
    await cb.answer("⏳ بررسی GitHub…")
    up = _updater(db, settings, locks, services)
    info = await up.check()
    st = await up.load_state()
    mode = install_mode()
    status = ("🟡 نسخه‌ی جدید موجود است" if info.newer else "🟢 آخرین نسخه") if not info.error else f"⚠️ {escape(info.error)}"
    last_backup = next((b for b in _backups(db, settings, locks, services).list() if b.kind == "pre-update"), None)
    text = (f"🔄 <b>بررسی آپدیت پروژه</b>\n\nنسخه فعلی:\n<b>v{info.current}</b>\n\nآخرین نسخه:\n"
            f"<b>{('v' + info.latest) if info.latest else '—'}</b>\n\nوضعیت:\n{status}\n\n"
            f"Release Date: {info.published_at or '—'}\n"
            f"Migration Required: {'—' if st.migration_required is None else ('بله' if st.migration_required else 'خیر')}\n"
            f"Backup Status: {last_backup.name if last_backup else 'قبل از آپدیت خودکار ساخته می‌شود'}\n"
            f"حالت نصب: {mode}\n\n" + (_state_text(st) if st.steps else ""))
    b = InlineKeyboardBuilder()
    b.button(text="🔄 بررسی دوباره", callback_data=Op(a="upd"))
    if info.changelog:
        b.button(text="📝 مشاهده تغییرات", callback_data=Op(a="updlog"))
    if info.newer and not info.error:
        b.button(text=f"🚀 آپدیت به v{info.latest}", callback_data=Op(a="upd?"))
    b.adjust(1)
    await edit_or_send(cb.message, text[:4000], back_to(MENU, b))


@router.callback_query(Op.filter(F.a == "updlog"))
async def update_changelog(cb: CallbackQuery, db: Database):
    info = await db.get_json("update:last_check") or {}
    await cb.answer()
    await cb.message.answer(f"📝 <b>تغییرات v{escape(str(info.get('latest')))}</b>\n\n"
                            f"{escape((info.get('changelog') or '—')[:3500])}", disable_web_page_preview=True)


@router.callback_query(Op.filter(F.a == "upd?"))
async def update_confirm(cb: CallbackQuery, db: Database, is_owner: bool = False):
    if not await _owner_only(cb, is_owner):
        return
    info = await db.get_json("update:last_check") or {}
    await edit_or_send(cb.message, f"🚀 به‌روزرسانی به <b>v{escape(str(info.get('latest')))}</b>؟\n\n"
                                   "مراحل: پیش‌بررسی و اعتبارسنجی Release ← پشتیبان ← تعویض کد ← نصب وابستگی‌ها ← "
                                   "مهاجرت ← Health Check ← Restart.\nاگر هر مرحله شکست بخورد، خودکار به نسخه‌ی فعلی "
                                   "برمی‌گردد.", _confirm(Op(a="upd!"), Op(a="upd")))
    await cb.answer()


@router.callback_query(Op.filter(F.a == "upd!"))
async def update_apply(cb: CallbackQuery, db: Database, settings: Settings, locks=None, services=None,
                       is_owner: bool = False):
    global _update_task
    if not await _owner_only(cb, is_owner):
        return
    if _update_task is not None and not _update_task.done():
        await cb.answer("به‌روزرسانی در حال اجراست.", show_alert=True)
        return
    up = _updater(db, settings, locks, services)
    info = await up.check()
    if not info.newer:
        await cb.answer("نسخه‌ی جدیدتری نیست.", show_alert=True)
        return
    await cb.answer("شروع شد")
    status = await cb.message.answer("⏳ به‌روزرسانی شروع شد…")

    async def progress(st):
        try:
            await status.edit_text(f"🔄 <b>به‌روزرسانی به v{info.latest}</b>\n\n" + _state_text(st))
        except Exception:
            pass

    async def run():
        try:
            st = await up.apply(info, admin_id=cb.from_user.id, on_progress=progress,
                                request_restart=_svc(services, "request_restart"))
            await progress(st)
        except Exception as e:
            await status.edit_text(f"❌ به‌روزرسانی شروع نشد: {escape(str(e))}")
    _update_task = asyncio.create_task(run())


# ---------- Migration Manager ----------
@router.callback_query(Op.filter(F.a == "mig"))
async def migrations_view(cb: CallbackQuery, db: Database):
    cur, head, pending = await current_revision(db), head_revision(), await pending_revisions(db)
    b = InlineKeyboardBuilder()
    if pending:
        b.button(text="▶️ Run Migration", callback_data=Op(a="mig?"))
    await edit_or_send(cb.message, f"🗄 <b>Migration Manager</b>\n\nCurrent Migration: <code>{cur}</code>\n"
                                   f"Latest Migration: <code>{head}</code>\nPending Migrations: "
                                   f"{', '.join(pending) if pending else '— (به‌روز است)'}\nپایگاه داده: {db.dialect}",
                       back_to(MENU, b))
    await cb.answer()


@router.callback_query(Op.filter(F.a == "mig?"))
async def migration_confirm(cb: CallbackQuery, is_owner: bool = False):
    if not await _owner_only(cb, is_owner):
        return
    await edit_or_send(cb.message, "▶️ اجرای مهاجرت؟ قبل از آن پشتیبان pre-migration ساخته و بررسی می‌شود.",
                       _confirm(Op(a="mig!"), Op(a="mig")))
    await cb.answer()


@router.callback_query(Op.filter(F.a == "mig!"))
async def migration_run(cb: CallbackQuery, db: Database, settings: Settings, locks=None, services=None,
                        is_owner: bool = False):
    if not await _owner_only(cb, is_owner):
        return
    await cb.answer("⏳ …")
    mgr = _backups(db, settings, locks, services)
    try:
        info = await mgr.create("pre-migration", admin_id=cb.from_user.id)
        if not mgr.verify(info.name)["ok"]:
            raise BackupError("پشتیبان سالم نیست")
        before = await current_revision(db)
        await upgrade(db)
        after = await current_revision(db)
        await db.audit(admin_id=cb.from_user.id, action="migration_run", before={"revision": before},
                       after={"revision": after, "backup": info.name})
        await cb.message.answer(f"✅ مهاجرت انجام شد: {before} → {after}\nپشتیبان: <code>{info.name}</code>")
    except Exception as e:
        log.exception("migration failed")
        await cb.message.answer(f"❌ مهاجرت ناموفق: {escape(redact(str(e)))[:300]}\nپشتیبان قبل از مهاجرت موجود است.")


# ---------- API Diagnostics ----------
@router.callback_query(Op.filter(F.a == "diag"))
async def diagnostics(cb: CallbackQuery, shop: Shop, services=None):
    await cb.answer("⏳ …")
    rows = await api_diagnostics(shop.api, _svc(services, "metrics"))
    icon = {"ok": "🟢", "warn": "🟡", "fail": "🔴"}
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Run Diagnostics", callback_data=Op(a="diag"))
    await edit_or_send(cb.message, "🧪 <b>API Diagnostics</b>\n\n" + "\n".join(
        f"{icon[s]} <b>{n}</b>: {escape(d)}" for n, s, d in rows), back_to(MENU, b))


# ---------- API Key Manager ----------
@router.callback_query(Op.filter(F.a == "keys"))
async def keys_view(cb: CallbackQuery, db: Database, settings: Settings):
    rows = await apikeys.list_keys(db)
    lines = ["🔑 <b>API Key Manager</b>\n",
             f"کلیدهای API خود ربات برای /metrics و /api سرور داخلی ({escape(settings.http_host)}:{settings.http_port}). "
             "کلید کامل فقط یک بار نمایش داده می‌شود.\n"]
    b = InlineKeyboardBuilder()
    b.button(text="➕ Create", callback_data=Op(a="keyc"))
    for k in rows:
        state = "⛔️ باطل" if k["revoked_at"] else "🟢 فعال"
        lines.append(f"• <b>{escape(k['name'])}</b> <code>sbk_{k['prefix']}_…</code> {state}\n   دسترسی: {k['scopes']} | "
                     f"ساخت: {k['created_at'][:10]} | آخرین استفاده: {(k['last_used_at'] or '—')[:16]}")
        if not k["revoked_at"]:
            b.button(text=f"🔁 Rotate {k['name'][:12]}", callback_data=Op(a="keyrot", v=str(k["id"])))
            b.button(text=f"⛔️ Revoke {k['name'][:12]}", callback_data=Op(a="keyrev", v=str(k["id"])))
    b.adjust(1, 2)
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU, b))
    await cb.answer()


@router.callback_query(Op.filter(F.a == "keyc"))
async def key_create_ask(cb: CallbackQuery, state: FSMContext, is_owner: bool = False):
    if not await _owner_only(cb, is_owner):
        return
    await state.set_state(OpsForm.key_name)
    await cb.message.answer("🔑 نام کلید و دسترسی‌ها را بفرستید، مثلاً:\n<code>grafana metrics:read health:read</code>\n\n"
                            "دسترسی‌ها: " + ", ".join(f"<code>{s}</code>" for s in apikeys.SCOPES),
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(OpsForm.key_name)
async def key_create(message: Message, state: FSMContext, db: Database, is_owner: bool = False):
    parts = (message.text or "").split()
    if not is_owner or len(parts) < 2:
        await message.answer("❗️ مثال: grafana metrics:read")
        return
    try:
        kid, key = await apikeys.create_key(db, parts[0], parts[1:], admin_id=message.from_user.id)
    except ValueError:
        await message.answer("❗️ دسترسی نامعتبر. مجاز: " + ", ".join(apikeys.SCOPES))
        return
    await state.set_state(None)
    await message.answer(f"✅ کلید «{escape(parts[0])}» ساخته شد. <b>همین الان کپی کنید؛ دوباره نمایش داده نمی‌شود:</b>\n\n"
                         f"<code>{key}</code>", reply_markup=main_menu(True))


@router.callback_query(Op.filter(F.a.in_({"keyrev", "keyrot"})))
async def key_change(cb: CallbackQuery, callback_data: Op, db: Database, settings: Settings, is_owner: bool = False):
    if not await _owner_only(cb, is_owner):
        return
    kid = int(callback_data.v)
    if callback_data.a == "keyrev":
        ok = await apikeys.revoke_key(db, kid, admin_id=cb.from_user.id)
        await cb.answer("باطل شد" if ok else "قبلاً باطل شده", show_alert=not ok)
    else:
        new = await apikeys.rotate_key(db, kid, admin_id=cb.from_user.id)
        await cb.answer()
        if new:
            await cb.message.answer(f"🔁 کلید تازه (قبلی باطل شد). <b>فقط همین یک بار:</b>\n<code>{new[1]}</code>")
    await keys_view(cb, db, settings)


# ---------- Webhook Monitor ----------
@router.callback_query(Op.filter(F.a == "wh"))
async def webhooks_view(cb: CallbackQuery, db: Database, settings: Settings):
    counts = {r["status"]: r["n"] for r in await db.all("SELECT status, COUNT(*) AS n FROM webhook_events GROUP BY status")}
    retried = await db.scalar("SELECT COUNT(*) FROM webhook_events WHERE attempts > 1")
    rows = await db.all("SELECT * FROM webhook_events ORDER BY received_at DESC LIMIT 15")
    lines = ["🪝 <b>Webhook Monitor</b>\n",
             f"وضعیت: {'🟢 فعال' if settings.stard_webhook_secret else '⚪️ غیرفعال (STARD_WEBHOOK_SECRET تنظیم نشده)'} | "
             f"آدرس: <code>/webhooks/stard</code>",
             f"Received: {sum(counts.values()):,} | Processed: {counts.get('processed', 0):,} | "
             f"Failed: {counts.get('failed', 0):,} | Rejected: {counts.get('rejected', 0):,} | Retried: {retried:,}\n"]
    icon = {"processed": "✅", "failed": "❌", "rejected": "⛔️", "received": "⏳"}
    for r in rows:
        lines.append(f"{icon.get(r['status'], '•')} <code>{r['received_at'][5:16]}</code> {escape(r['type'])} "
                     f"×{r['attempts']}\n   {escape((r['response'] or '')[:80])}")
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU))
    await cb.answer()


# ---------- Queue Monitor ----------
@router.callback_query(Op.filter(F.a == "q"))
async def queue_view(cb: CallbackQuery, db: Database, queue: JobQueue = None, settings: Settings = None):
    queue = queue or JobQueue(db, settings.instance_id)
    q = await queue.stats()
    workers = [i for i in (await services_status(db)).get("worker", []) if i["alive"]]
    lines = ["📬 <b>Queue Monitor</b>\n",
             f"Queue Length: {q['queued']:,} (آماده: {q['ready']:,})", f"Processing: {q['running']:,}",
             f"Completed: {q['done']:,}", f"Failed (dead): {q['dead']:,}", f"Retry: {q['retrying']:,}",
             f"Worker Count: {len(workers)}", f"Worker Lag: {human_duration(q['lag_seconds'])}",
             f"Lease منقضی (worker کرش‌کرده): {q['stale']:,}\n"]
    if q["by_kind"]:
        lines.append("<b>به تفکیک نوع</b>")
        lines += [f"• {r['kind']}: {r['status']} × {r['n']}" for r in q["by_kind"]]
    b = InlineKeyboardBuilder()
    dead = await queue.dead_jobs(8)
    if dead:
        lines.append("\n<b>آخرین کارهای dead</b>")
        for j in dead:
            lines.append(f"☠️ #{j['id']} {j['kind']} ({j['attempts']} تلاش): {escape(redact(j['last_error'] or '')[:90])}")
            b.button(text=f"🔁 Retry #{j['id']}", callback_data=Op(a="qr", v=str(j["id"])))
    bc = await db.all("SELECT * FROM broadcasts ORDER BY id DESC LIMIT 3")
    if bc:
        lines.append("\n<b>📢 پیام‌های همگانی</b>")
        lines += [f"#{x['id']} {x['status']} | ✅ {x['sent']:,} ⛔️ {x['blocked']:,} ❌ {x['failed']:,} از {x['total']:,}"
                  for x in bc]
    b.button(text="🔄 به‌روزرسانی", callback_data=Op(a="q"))
    b.adjust(2)
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU, b))
    await cb.answer()


@router.callback_query(Op.filter(F.a == "qr"))
async def queue_retry(cb: CallbackQuery, callback_data: Op, db: Database, queue: JobQueue = None,
                      settings: Settings = None):
    queue = queue or JobQueue(db, settings.instance_id)
    ok = await queue.retry_dead(int(callback_data.v))
    if ok:
        await db.audit(admin_id=cb.from_user.id, action="job_retry", ref=f"job:{callback_data.v}")
    await cb.answer("در صف قرار گرفت (با همان کلید؛ عملیات تکراری نمی‌شود)" if ok else
                    "ممکن نشد (کار فعال دیگری برای همین مورد هست)", show_alert=True)


# ---------- Security / DB / Deps ----------
@router.callback_query(Op.filter(F.a == "sec"))
async def security_view(cb: CallbackQuery, db: Database, settings: Settings):
    await cb.answer("⏳ در حال اسکن…")
    findings = await security_scan(settings, db)
    counts = {s: sum(f.severity == s for f in findings) for s in SEVERITY}
    lines = ["🛡 <b>Security Scanner</b>\n", " | ".join(f"{SEVERITY[s]}: {n}" for s, n in counts.items()), ""]
    for f in findings:
        lines.append(f"{SEVERITY[f.severity]} — <b>{escape(f.title)}</b>" + (f"\n   {escape(f.detail)}" if f.detail else ""))
    if not findings:
        lines.append("✅ مشکلی پیدا نشد.")
    b = InlineKeyboardBuilder()
    b.button(text="🔄 اسکن دوباره", callback_data=Op(a="sec"))
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU, b))


@router.callback_query(Op.filter(F.a == "dbs"))
async def db_view(cb: CallbackQuery, db: Database):
    st = await db_stats(db)
    lines = ["📊 <b>DB Statistics</b>\n", f"نوع: {st['dialect']} | Database Size: {human_bytes(st['size'])}",
             f"Users: {st['users']:,} | Orders: {st['orders']:,} | Transactions (topups): {st['topups']:,}",
             f"Ledger Entries: {st['ledger']:,} | Failed Orders: {st['failed_orders']:,} | "
             f"Active Orders: {st['active_orders']:,}", "", "<b>Table Sizes</b>"]
    for name, t in sorted(st["tables"].items(), key=lambda x: -(x[1].get("rows") or 0)):
        lines.append(f"• {name}: {t.get('rows') if t.get('rows') is not None else '?'} ردیف"
                     + (f" | {human_bytes(t['bytes'])}" if t.get("bytes") else ""))
    lines.append(f"\n<b>Indexes</b>: {sum(len(v) for v in st['indexes'].values())}")
    lines += [f"• {t}: {', '.join(v)}" for t, v in st["indexes"].items() if v][:15]
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU))
    await cb.answer()


@router.callback_query(Op.filter(F.a == "deps"))
async def deps_view(cb: CallbackQuery):
    await cb.answer("⏳ بررسی PyPI…")
    r = await dependency_report()
    icon = {"ok": "🟢", "outdated": "🟡", "missing": "🔴"}
    lines = ["📦 <b>Dependency Checker</b>\n", f"Python: {r['python']} {'🟢' if r['python_ok'] else '🔴 (حداقل 3.11)'}",
             f"Node: {r['node']}", f"Docker Image: {escape(r['docker_base'] or '—')}", "",
             "<b>Packages</b> (فعلی → آخرین)"]
    for p in r["packages"]:
        lines.append(f"{icon[p['status']]} {p['name']}: {p['current'] or '—'} → {p['latest'] or '?'}")
    lines.append("\n<b>System Dependencies</b>")
    lines += [f"{'🟢' if ok else '⚪️'} {tool}" for tool, ok in r["system"].items()]
    lines.append("\nوضعیت امنیتی وابستگی‌ها: 🛡 Security Scanner (با pip-audit)")
    await edit_or_send(cb.message, "\n".join(lines), back_to(MENU))


# ---------- Audit Log ----------
@router.callback_query(Op.filter(F.a == "audit"))
async def audit_view(cb: CallbackQuery, callback_data: Op, db: Database):
    page = int(callback_data.v or 0)
    rows = await db.audit_entries(limit=15, offset=page * 15)
    lines = ["📋 <b>Audit Log</b> — همه‌ی کارهای حساس مدیرها و سیستم\n"]
    for r in rows:
        who = f"👮 {r['admin_id']}" if r["admin_id"] else "🤖 سیستم"
        tail = " | ".join(x for x in (
            f"کاربر {r['user_id']}" if r["user_id"] else "", f"سفارش #{r['order_id']}" if r["order_id"] else "",
            fmt_toman(r["amount"]) if r["amount"] else "", escape(r["ref"] or ""),
            f"قبل: {escape((r['before'] or '')[:60])}" if r["before"] else "",
            f"بعد: {escape((r['after'] or '')[:60])}" if r["after"] else "",
            f"دلیل: {escape((r['reason'] or '')[:60])}" if r["reason"] else "") if x)
        lines.append(f"<code>{r['created_at'][5:16]}</code> {who} <b>{escape(r['action'])}</b>\n   {tail}")
    b = InlineKeyboardBuilder()
    if page > 0:
        b.button(text="◀️ جدیدتر", callback_data=Op(a="audit", v=str(page - 1)))
    if len(rows) == 15:
        b.button(text="قدیمی‌تر ▶️", callback_data=Op(a="audit", v=str(page + 1)))
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU, b))
    await cb.answer()


# ---------- هشدارها ----------
@router.callback_query(Op.filter(F.a.in_({"al", "alt"})))
async def alerts_view(cb: CallbackQuery, callback_data: Op, db: Database):
    off = set(await db.get_json("alerts_off", []) or [])
    if callback_data.a == "alt" and callback_data.v in ALERT_TEXT:
        off.symmetric_difference_update({callback_data.v})
        await db.set_json("alerts_off", sorted(off))
        await db.audit(admin_id=cb.from_user.id, action="alert_toggle", ref=callback_data.v,
                       after={"enabled": callback_data.v not in off})
    active = {r["key"] for r in await db.all("SELECT key FROM alert_state WHERE active = 1")}
    th = {**DEFAULT_THRESHOLDS, **(await db.get_json("alert_thresholds", {}) or {})}
    lines = ["🚨 <b>هشدارها</b>\n", "هشدار وقتی مشکل شروع می‌شود، هر ۳۰ دقیقه تا وقتی ادامه دارد، و وقتی برطرف شد ارسال می‌شود.\n"]
    b = InlineKeyboardBuilder()
    for key, label in ALERT_TEXT.items():
        lines.append(f"{'🔴 فعال' if key in active else '🟢'} {label}")
        b.button(text=f"{'🔕' if key in off else '🔔'} {label[:22]}", callback_data=Op(a="alt", v=key))
    lines.append("\n<b>آستانه‌ها</b>: " + ", ".join(f"{k}={v}" for k, v in th.items()))
    b.button(text="✏️ تغییر آستانه", callback_data=Op(a="alth"))
    b.adjust(2)
    await edit_or_send(cb.message, "\n".join(lines)[:4000], back_to(MENU, b))
    await cb.answer()


@router.callback_query(Op.filter(F.a == "alth"))
async def threshold_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OpsForm.threshold)
    await cb.message.answer("✏️ به شکل <code>نام مقدار</code> بفرستید، مثلاً <code>disk_percent 85</code> یا "
                            "<code>wallet_min 50000000</code>\nنام‌ها: " + ", ".join(DEFAULT_THRESHOLDS),
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(OpsForm.threshold)
async def threshold_set(message: Message, state: FSMContext, db: Database):
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[0] not in DEFAULT_THRESHOLDS:
        await message.answer("❗️ مثال: disk_percent 85")
        return
    try:
        value = float(parts[1])
    except ValueError:
        await message.answer("❗️ مقدار باید عدد باشد.")
        return
    th = await db.get_json("alert_thresholds", {}) or {}
    before = dict(th)
    th[parts[0]] = value
    await db.set_json("alert_thresholds", th)
    await db.audit(admin_id=message.from_user.id, action="alert_threshold", ref=parts[0], before=before, after=th)
    await state.set_state(None)
    await message.answer(f"✅ {parts[0]} = {value:g}", reply_markup=main_menu(True))


# ---------- Maintenance ----------
@router.callback_query(Op.filter(F.a == "maint"))
async def maintenance_view(cb: CallbackQuery, shop: Shop):
    on, msg = await shop.maintenance()
    b = InlineKeyboardBuilder()
    b.button(text="🟢 خاموش کردن Maintenance" if on else "🧹 روشن کردن Maintenance", callback_data=Op(a="maint!"))
    b.button(text="✏️ متن پیام Maintenance", callback_data=Op(a="mainttxt"))
    b.adjust(1)
    await edit_or_send(cb.message, f"🧹 <b>Maintenance Mode</b>\n\nوضعیت: {'🔴 روشن' if on else '🟢 خاموش'}\n\n"
                                   "در این حالت فروش متوقف می‌شود و کاربران فقط پیام زیر را می‌بینند؛ مدیرها همه‌چیز را "
                                   "می‌بینند و مدیریت می‌کنند. سفارش‌های در جریان همچنان پیگیری و تحویل می‌شوند.\n\n"
                                   f"پیام:\n{escape(msg)}", back_to(HOME, b))
    await cb.answer()


@router.callback_query(Op.filter(F.a == "maint!"))
async def maintenance_toggle(cb: CallbackQuery, db: Database, shop: Shop):
    on, _ = await shop.maintenance()
    await db.set_setting("maintenance", "0" if on else "1")
    await db.audit(admin_id=cb.from_user.id, action="maintenance", after={"on": not on})
    if await notify.enabled(db, "maintenance"):
        await notify.to_log_channel(cb.bot, db, "🧹 Maintenance روشن شد؛ فروش متوقف است." if not on else
                                    "🟢 Maintenance خاموش شد؛ فروش ادامه دارد.")
    await maintenance_view(cb, shop)


@router.callback_query(Op.filter(F.a == "mainttxt"))
async def maintenance_text_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(OpsForm.maint_text)
    await cb.message.answer("✏️ متن پیام Maintenance را بفرستید:", reply_markup=cancel_menu())
    await cb.answer()


@router.message(OpsForm.maint_text, F.text)
async def maintenance_text(message: Message, state: FSMContext, db: Database):
    await db.set_setting("maintenance_text", message.text[:1000])
    await db.audit(admin_id=message.from_user.id, action="setting_set", ref="maintenance_text")
    await state.set_state(None)
    await message.answer("✅ ذخیره شد.", reply_markup=main_menu(True))


# ---------- ابزار برای سایر ماژول‌ها ----------
async def send_bytes(bot: Bot, chat_id: int, data: bytes, filename: str, caption: str = "") -> None:
    await bot.send_document(chat_id, BufferedInputFile(data, filename=filename), caption=caption)

