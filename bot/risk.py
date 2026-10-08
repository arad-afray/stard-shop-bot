"""امتیاز ریسک و Ban خودکار.

- هر رفتار مشکوک یک «رویداد ریسک» با وزن ثبت می‌کند؛ امتیاز کاربر = جمع وزن‌ها در ۲۴ ساعت اخیر
  (رویدادهای قدیمی خودکار بی‌اثر می‌شوند، پس کاربری که یک بار اشتباه کرده برای همیشه علامت‌دار نمی‌ماند).
- Ban خودکار پیش‌فرض خاموش است. وقتی روشن باشد، فقط وقتی اعمال می‌شود که هم امتیاز از آستانه بگذرد و هم
  حداقل چند نوع رویداد متفاوت (پیش‌فرض ۲) ثبت شده باشد؛ یک نوع خطای تکراری (مثلاً اینترنت ضعیف) به Ban
  نمی‌رسد. مدیرها هرگز Ban نمی‌شوند. شواهد قبل از مسدودسازی در گزارش رویدادها ثبت می‌شود.
"""
from __future__ import annotations

import logging
from typing import Any

from .db import Database, ago, now

log = logging.getLogger(__name__)

# نوع رویداد → (وزن، توضیح)
EVENTS: dict[str, tuple[int, str]] = {
    "spam": (5, "ارسال پیام/کلیک خیلی سریع "),
    "rate_limit": (3, "عبور از سقف درخواست "),
    "coupon_fail": (2, "امتحان کد تخفیف نامعتبر"),
    "failed_payment": (1, "تلاش خرید بدون موجودی کافی"),
    "rapid_purchase": (4, "خرید زیاد در زمان کوتاه"),
    "duplicate_request": (2, "درخواست تکراری"),
    "refund_abuse": (6, "برگشت پول غیرعادی"),
    "topup_duplicate": (4, "ارسال رسید تکراری"),
    "api_abuse": (8, "رفتار مشکوک API"),
}
DEFAULT_RULES = {"enabled": False, "threshold": 60, "min_kinds": 2, "window_hours": 24, "notify": True}


class RiskEngine:
    def __init__(self, db: Database, *, on_ban: Any = None, is_admin: Any = None):
        self.db = db
        self.on_ban = on_ban          # async (user_id, score, reasons) → اطلاع به مدیرها
        self.is_admin = is_admin      # (user_id) → bool

    async def rules(self) -> dict:
        return {**DEFAULT_RULES, **(await self.db.get_json("risk_rules", {}) or {})}

    async def set_rules(self, admin_id: int | None = None, **changes: Any) -> dict:
        before = await self.rules()
        new = {**before, **{k: v for k, v in changes.items() if k in DEFAULT_RULES}}
        await self.db.set_json("risk_rules", new)
        await self.db.audit(admin_id=admin_id, action="risk_rules", before=before, after=new)
        return new

    async def score(self, user_id: int, hours: int | None = None) -> tuple[int, dict[str, int]]:
        hours = hours or (await self.rules())["window_hours"]
        rows = await self.db.all("SELECT kind, COALESCE(SUM(weight), 0) AS w, COUNT(*) AS n FROM risk_events "
                                 "WHERE user_id = :u AND created_at >= :t GROUP BY kind",
                                 {"u": user_id, "t": ago(hours / 24)})
        by_kind = {r["kind"]: int(r["n"]) for r in rows}
        return sum(int(r["w"]) for r in rows), by_kind

    async def record(self, user_id: int, kind: str) -> int:
        """ثبت رویداد و به‌روز کردن امتیاز؛ اگر Ban خودکار روشن و شرط‌ها برقرار باشد، Ban می‌کند."""
        weight = EVENTS.get(kind, (1, ""))[0]
        try:
            await self.db.write("INSERT INTO risk_events(user_id, kind, weight, created_at) VALUES(:u, :k, :w, :t)",
                                {"u": user_id, "k": kind, "w": weight, "t": now()})
            score, kinds = await self.score(user_id)
            await self.db.write("UPDATE users SET risk_score = :s WHERE id = :u", {"s": score, "u": user_id})
        except Exception:
            log.exception("risk record failed")  # ریسک هرگز نباید جریان اصلی را بشکند
            return 0
        await self._maybe_ban(user_id, score, kinds)
        return score

    async def _maybe_ban(self, user_id: int, score: int, kinds: dict[str, int]) -> None:
        r = await self.rules()
        if not r["enabled"] or score < r["threshold"] or len(kinds) < r["min_kinds"]:
            return
        if self.is_admin is not None and self.is_admin(user_id):
            return
        u = await self.db.get_user(user_id)
        if u is None or u.banned:
            return
        reasons = ", ".join(f"{EVENTS.get(k, (0, k))[1]}×{n}" for k, n in kinds.items())
        # شواهد قبل از Ban ثبت می‌شود
        await self.db.audit(admin_id=None, action="auto_ban_evidence", user_id=user_id,
                            before={"score": score, "events": kinds}, reason=reasons[:500])
        await self.db.set_banned(user_id, True, admin_id=None, reason=f"auto: risk {score}")
        log.warning("auto-banned user %s (risk %s: %s)", user_id, score, reasons)
        if self.on_ban is not None and r.get("notify"):
            try:
                await self.on_ban(user_id, score, reasons)
            except Exception:
                log.exception("auto-ban notify failed")

    async def top(self, limit: int = 15) -> list[dict]:
        return await self.db.all("SELECT id, username, first_name, risk_score, banned FROM users WHERE risk_score > 0 "
                                 "ORDER BY risk_score DESC LIMIT :l", {"l": limit})

    async def reset(self, user_id: int, *, admin_id: int | None = None) -> None:
        await self.db.write("DELETE FROM risk_events WHERE user_id = :u", {"u": user_id})
        await self.db.write("UPDATE users SET risk_score = 0 WHERE id = :u", {"u": user_id})
        await self.db.audit(admin_id=admin_id, action="risk_reset", user_id=user_id)
