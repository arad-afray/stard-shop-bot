"""جواب قیمت در گروه‌ها: تشخیص سؤال، تنظیمات قابل تغییر از پنل، و ساخت متن جواب.

نمونه سؤال‌هایی که فهمیده می‌شوند:
    «قیمت» ، «قیمت دلار» ، «تون» ، «استارز؟» ، «10 تون» ، «۱۰ تون چنده» ، «قیمت ۵۰۰ استارز» ،
    «1k استارز» ، «۲٫۵ تون به تومان» ، «100 دلار چند تومنه» ، «ده هزار استارز»
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .db import Database
from .pricing import fmt_toman

KINDS = ("usd", "ton", "stars")
KIND_LABELS = {"usd": "💵 دلار", "ton": "💎 تون (TON)", "stars": "⭐ استارز", "all": "💹 همه‌ی قیمت‌ها («قیمت»)"}

KEYWORDS = {
    "usd": ("دلار", "دلاری", "usd", "dollar", "dollars"),
    "ton": ("تون", "تن", "ton", "toncoin", "تونکوین"),
    "stars": ("استارز", "استار", "استارزی", "stars", "star", "ستاره"),
}
TRIGGERS = ("قیمت", "نرخ", "price", "rate")
# کلمه‌هایی که در سؤال قیمت معمول‌اند؛ اگر کلمه‌ای غیر از این‌ها باشد، پیام گفتگوی عادی حساب می‌شود
FILLERS = {
    "چند", "چنده", "چقدر", "چقد", "چنتا", "چندتا", "چنده؟", "میشه", "می", "شه", "میشود", "می‌شود", "شود", "تا", "عدد",
    "به", "تومان", "تومن", "تومنه", "تومانه", "ریال", "هست", "است", "الان", "امروز", "لطفا", "لطفاً", "و", "یا", "رو",
    "را", "برای", "how", "much", "is", "the", "in", "to", "irt", "toman", "now", "هر", "یک", "یه", "دونه", "عددی",
    "حدودا", "حدوداً", "بفرمایید", "بگید", "بگو", "داری", "دارید",
}
MULTIPLIERS = {"k": 1000, "هزار": 1000, "هزارتا": 1000, "m": 1_000_000, "میلیون": 1_000_000, "تومن": 1, "تومان": 1}
WORD_NUMBERS = {"یک": 1, "یه": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "شیش": 6, "هفت": 7, "هشت": 8,
                "نه": 9, "ده": 10, "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "صد": 100, "دویست": 200,
                "سیصد": 300, "پانصد": 500, "پونصد": 500, "هزار": 1000}
MAX_AMOUNT = {"usd": Decimal(10_000_000), "ton": Decimal(1_000_000), "stars": Decimal(10_000_000)}
STARS_BUY_MIN, STARS_BUY_MAX = 50, 1_000_000
MAX_LEN = 60

DEFAULT_CFG: dict = {
    "enabled": True,
    "kinds": {"usd": True, "ton": True, "stars": True, "all": True},
    "bare_word": True,      # جواب به یک کلمه‌ی تنها مثل «تون»
    "amounts": True,        # جواب به سؤال با تعداد مثل «10 تون»
    "mode": "all",          # all: همه‌ی گروه‌ها | allow: فقط گروه‌های فهرست | deny: همه به جز فهرست
    "chats": [],            # فهرست گروه‌ها برای allow/deny
    "cooldown": 3,          # ثانیه بین دو جواب یکسان در یک گروه
    "buy_button": True,     # دکمه‌ی خرید زیر جواب
    "delete_after": 0,      # دقیقه؛ پاک شدن خودکار جواب (۰ = هرگز)
    "words": {"usd": [], "ton": [], "stars": []},   # کلمه‌های اضافه‌ی مدیر
    "inline": True,         # حالت اینلاین: «@ربات 10 تون» در هر چتی
}
MODE_LABELS = {"all": "همه‌ی گروه‌ها", "allow": "فقط گروه‌های انتخاب‌شده", "deny": "همه به جز گروه‌های انتخاب‌شده"}


@dataclass(frozen=True)
class Query:
    kind: str                       # usd | ton | stars | all
    amount: Decimal | None = None   # تعداد (مثلاً 10 برای «10 تون»)


async def get_config(db: Database) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULT_CFG.items()}
    saved = await db.get_json("group_prices_cfg", {}) or {}
    for k, v in saved.items():
        if isinstance(cfg.get(k), dict) and isinstance(v, dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    # سازگاری با نسخه‌ی قبل: کلید ساده‌ی group_prices
    if not saved and (await db.get_setting("group_prices", "1")) == "0":
        cfg["enabled"] = False
    return cfg


async def save_config(db: Database, cfg: dict) -> None:
    await db.set_json("group_prices_cfg", cfg)
    await db.set_setting("group_prices", "1" if cfg["enabled"] else "0")


def chat_allowed(cfg: dict, chat_id: int) -> bool:
    listed = chat_id in set(cfg.get("chats") or [])
    return {"allow": listed, "deny": not listed}.get(cfg.get("mode", "all"), True)


# ---------- تشخیص ----------
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize(text: str) -> str:
    text = text.translate(_DIGITS).replace("ي", "ی").replace("ك", "ک").replace("‌", " ").lower()
    text = text.replace("٫", ".")
    text = re.sub(r"(?<=\d)[,٬،](?=\d{3})", "", text)       # 10,000 → 10000
    text = re.sub(r"(?<=\d)\s*(k|m)\b", r" \1", text)       # 1k → 1 k
    return text


def _tokens(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?|[^\W\d_]+", text)


def _keywords(custom: dict | None) -> dict[str, set[str]]:
    out = {k: set(v) for k, v in KEYWORDS.items()}
    for k, words in (custom or {}).items():
        if k in out:
            out[k] |= {normalize(w).strip() for w in words if w and w.strip()}
    return out


def parse(text: str | None, cfg: dict | None = None) -> Query | None:
    """سؤال قیمت را تشخیص می‌دهد؛ برای گفتگوی عادی None."""
    cfg = cfg or DEFAULT_CFG
    if not text or len(text) > MAX_LEN or text.lstrip().startswith("/"):
        return None
    words = _tokens(normalize(text))
    if not words:
        return None
    kw = _keywords(cfg.get("words"))
    asked: list[str] = []
    numbers: list[Decimal] = []
    trigger = False
    other = False
    i = 0
    while i < len(words):
        w = words[i]
        kind = next((k for k, s in kw.items() if w in s), None)
        if kind:
            if kind not in asked:
                asked.append(kind)
        elif w[0].isdigit():
            try:
                n = Decimal(w)
            except InvalidOperation:
                return None
            if i + 1 < len(words) and words[i + 1] in MULTIPLIERS:
                n *= MULTIPLIERS[words[i + 1]]
                i += 1
            numbers.append(n)
        elif w in WORD_NUMBERS and i + 1 < len(words) and (
                words[i + 1] in ("هزار", "میلیون") or any(words[i + 1] in s for s in kw.values())):
            n = Decimal(WORD_NUMBERS[w])      # «ده هزار استارز»، «یه تون»
            if words[i + 1] in ("هزار", "میلیون"):
                n *= MULTIPLIERS[words[i + 1]]
                i += 1
            numbers.append(n)
        elif w in TRIGGERS or w.startswith("قیمت"):
            trigger = True
        elif w not in FILLERS:
            other = True
        i += 1

    if other and not trigger:
        return None
    if other and trigger and len(words) > 4:
        return None     # جمله‌ی طولانی که فقط کلمه‌ی «قیمت» دارد
    # سؤال با تعداد: «10 تون»
    if numbers and len(asked) == 1:
        if not cfg.get("amounts", True) or len(numbers) != 1:
            return None
        n = numbers[0]
        if n <= 0 or n > MAX_AMOUNT[asked[0]]:
            return None
        return Query(asked[0], n)
    if numbers:
        return None
    if trigger:
        return Query(asked[0] if len(asked) == 1 else "all")
    if len(words) == 1 and len(asked) == 1:
        return Query(asked[0]) if cfg.get("bare_word", True) else None
    if len(asked) == 1 and not other and len(words) <= 3:
        return Query(asked[0]) if cfg.get("bare_word", True) else None   # «تون چنده؟»
    return None


def kind_enabled(cfg: dict, q: Query) -> bool:
    return bool(cfg.get("kinds", {}).get(q.kind, True))


# ---------- متن جواب ----------
def fmt_num(x: Decimal | float, places: int = 2) -> str:
    d = Decimal(str(x)).quantize(Decimal(1) / (10 ** places)) if places else Decimal(str(x)).quantize(Decimal(1))
    s = f"{d:,.{places}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def amount_text(q: Query, rates: dict, star_sell: int, stars_total: int | None) -> tuple[str, int | None]:
    """متن جواب برای سؤال با تعداد؛ و تعداد استارز برای دکمه‌ی خرید (اگر قابل خرید باشد)."""
    n = q.amount or Decimal(1)
    usd = Decimal(str((rates.get("usd") or {}).get("amount") or 0))
    ton = rates.get("ton") or {}
    ton_toman = Decimal(str(ton.get("amount") or 0))
    ton_usd = Decimal(str(ton.get("usd") or 0))
    lines = []
    buy = None
    if q.kind == "stars":
        count = int(n)
        total = Decimal(stars_total if stars_total is not None else star_sell * count)
        lines.append(f"⭐ <b>{count:,} استارز</b> = <b>{fmt_toman(int(total))}</b>")
        if usd:
            lines.append(f"≈ ${fmt_num(total / usd)}")
        if ton_toman:
            lines.append(f"≈ {fmt_num(total / ton_toman, 3)} TON")
        if count < STARS_BUY_MIN:
            lines.append(f"\nℹ️ حداقل خرید {STARS_BUY_MIN} استارز است.")
        elif count <= STARS_BUY_MAX:
            buy = count
    elif q.kind == "ton":
        if not ton_toman:
            return "⚠️ قیمت TON الان در دسترس نیست.", None
        total = n * ton_toman
        lines.append(f"💎 <b>{fmt_num(n, 4)} TON</b> = <b>{fmt_toman(int(total))}</b>")
        if ton_usd:
            lines.append(f"≈ ${fmt_num(n * ton_usd)}")
        if star_sell:
            lines.append(f"≈ {int(total / star_sell):,} استارز")
    else:
        if not usd:
            return "⚠️ قیمت دلار الان در دسترس نیست.", None
        total = n * usd
        lines.append(f"💵 <b>${fmt_num(n)}</b> = <b>{fmt_toman(int(total))}</b>")
        if ton_toman:
            lines.append(f"≈ {fmt_num(total / ton_toman, 3)} TON")
        if star_sell:
            lines.append(f"≈ {int(total / star_sell):,} استارز")
    return "💹 <b>محاسبه با نرخ لحظه‌ای</b>\n\n" + "\n".join(lines), buy
