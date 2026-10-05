"""Feature Flag، مدیریت دکمه‌های فروشگاه، و پاداش روزانه / گردونه.

لایه‌بندی (برای اینکه سه سوییچ هم‌پوشان نداشته باشیم):
  Feature Flag  = سوییچ اصلی + درصد Rollout (برای هر کاربر با hash ثابت: کاربر همیشه یا داخل است یا بیرون)
  دکمه          = نمایش/مخفی، فعال/غیرفعال، ترتیب
  یک بخش برای کاربر دیده می‌شود اگر flag برایش روشن و دکمه «نمایش» باشد؛ و خریدنی است اگر دکمه «فعال» هم باشد.
"""
from __future__ import annotations

import random
import time
import zlib

from sqlalchemy import text

from .db import Database, now, today

# flag → (توضیح، پیش‌فرض روشن؟)
FLAGS: dict[str, tuple[str, bool]] = {
    "stars_shop": ("⭐ فروش استارز", True),
    "gifts_shop": ("🎁 فروش گیفت", True),
    "premium_shop": ("💎 فروش پریمیوم", True),
    "boost_shop": ("🚀 فروش بوست", True),
    "reaction_shop": ("❤️ ریکشن استارزی", True),
    "nft_shop": ("🖼 فروش NFT", False),
    "username_shop": ("👤 فروش یوزرنیم", False),
    "number_shop": ("📱 فروش شماره", False),
    "daily_reward": ("🎁 پاداش روزانه", False),
    "spin": ("🎡 گردونه‌ی شانس", False),
    "referral": ("👥 زیرمجموعه‌گیری", True),
}
ROLLOUTS = (0, 10, 25, 50, 100)

# بخش فروشگاه → flag
CATEGORY_FLAG = {"stars": "stars_shop", "star_gift": "gifts_shop", "premium": "premium_shop", "boost": "boost_shop",
                 "reaction": "reaction_shop", "nft": "nft_shop", "username": "username_shop", "number": "number_shop"}
DEFAULT_ORDER = ["stars", "star_gift", "nft", "username", "number", "premium", "boost", "reaction"]
CACHE_TTL = 3.0


def in_rollout(key: str, user_id: int, percent: int) -> bool:
    if percent >= 100:
        return True
    if percent <= 0:
        return False
    return zlib.crc32(f"{key}:{user_id}".encode()) % 100 < percent


class Features:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, tuple[bool, int]] | None = None
        self._at = 0.0

    async def _flags(self) -> dict[str, tuple[bool, int]]:
        if self._cache is None or time.monotonic() - self._at > CACHE_TTL:
            rows = await self.db.all("SELECT key, enabled, rollout FROM feature_flags")
            stored = {r["key"]: (bool(r["enabled"]), int(r["rollout"])) for r in rows}
            self._cache = {k: stored.get(k, (default, 100)) for k, (_, default) in FLAGS.items()}
            self._cache.update({k: v for k, v in stored.items() if k not in self._cache})
            self._at = time.monotonic()
        return self._cache

    async def all(self) -> dict[str, tuple[bool, int]]:
        return dict(await self._flags())

    async def enabled(self, key: str, user_id: int | None = None) -> bool:
        on, pct = (await self._flags()).get(key, (True, 100))
        if not on:
            return False
        return True if user_id is None else in_rollout(key, user_id, pct)

    async def set(self, key: str, *, enabled: bool | None = None, rollout: int | None = None,
                  admin_id: int | None = None) -> None:
        if key not in FLAGS:
            raise ValueError("unknown flag")
        cur_on, cur_pct = (await self._flags())[key]
        new_on = cur_on if enabled is None else enabled
        new_pct = cur_pct if rollout is None else max(0, min(100, int(rollout)))
        async with self.db.tx() as c:
            await c.execute(text("INSERT INTO feature_flags(key, enabled, rollout) VALUES(:k, :e, :r) "
                                 "ON CONFLICT(key) DO UPDATE SET enabled = excluded.enabled, rollout = excluded.rollout"),
                            {"k": key, "e": int(new_on), "r": new_pct})
            await self.db.audit_in(c, admin_id=admin_id, action="flag_set", ref=key,
                                   before={"enabled": cur_on, "rollout": cur_pct},
                                   after={"enabled": new_on, "rollout": new_pct})
        self._cache = None

    # ---------- دکمه‌ها ----------
    async def buttons(self) -> list[dict]:
        """[{"key", "visible", "enabled"}] به ترتیب نمایش."""
        stored = await self.db.get_json("buttons", None)
        if stored is None:
            stored = []
            for k in DEFAULT_ORDER:
                # سازگاری با نسخه‌ی ۲: cat:<key>=0 یعنی غیرفعال
                legacy_off = (await self.db.get_setting(f"cat:{k}", "1")) == "0"
                stored.append({"key": k, "visible": True, "enabled": not legacy_off})
        keys = [b["key"] for b in stored]
        stored += [{"key": k, "visible": True, "enabled": True} for k in DEFAULT_ORDER if k not in keys]
        return [b for b in stored if b["key"] in CATEGORY_FLAG]

    async def _save_buttons(self, buttons: list[dict], admin_id: int | None, action: str, ref: str) -> None:
        before = await self.db.get_json("buttons", None)
        await self.db.set_json("buttons", buttons)
        await self.db.audit(admin_id=admin_id, action=action, ref=ref, before=before, after=buttons)

    async def toggle_button(self, key: str, field: str, *, admin_id: int | None = None) -> None:
        if field not in ("visible", "enabled"):
            raise ValueError(field)
        btns = await self.buttons()
        for b in btns:
            if b["key"] == key:
                b[field] = not b[field]
        await self._save_buttons(btns, admin_id, f"button_{field}", key)

    async def move_button(self, key: str, delta: int, *, admin_id: int | None = None) -> None:
        btns = await self.buttons()
        i = next((i for i, b in enumerate(btns) if b["key"] == key), None)
        if i is None:
            return
        j = max(0, min(len(btns) - 1, i + delta))
        btns.insert(j, btns.pop(i))
        await self._save_buttons(btns, admin_id, "button_move", key)

    async def category_state(self, category: str, user_id: int | None = None) -> str:
        """'ok' | 'disabled' (دیده می‌شود ولی خریدنی نیست) | 'hidden'"""
        flag = CATEGORY_FLAG.get(category)
        if flag and not await self.enabled(flag, user_id):
            return "hidden"
        b = next((b for b in await self.buttons() if b["key"] == category), None)
        if b is None or not b["visible"]:
            return "hidden"
        return "ok" if b["enabled"] else "disabled"

    async def shop_buttons(self, user_id: int | None) -> list[tuple[str, bool]]:
        """دکمه‌های فروشگاه برای یک کاربر: [(بخش، فعال؟)] به ترتیب."""
        out = []
        for b in await self.buttons():
            st = await self.category_state(b["key"], user_id)
            if st != "hidden":
                out.append((b["key"], st == "ok"))
        return out


# ---------- پاداش روزانه و گردونه ----------
DEFAULT_SPIN = [{"amount": 0, "weight": 50}, {"amount": 1000, "weight": 30}, {"amount": 5000, "weight": 15},
                {"amount": 20000, "weight": 5}]


class RewardError(Exception):
    pass


async def claim_reward(db: Database, user_id: int, kind: str, *, rng: random.Random | None = None) -> int:
    """یک بار در هر روز (UTC). مبلغ را برمی‌گرداند. idempotent: کلید اصلی (کاربر، نوع، روز)."""
    if kind == "daily":
        amount = int(await db.get_setting("daily_reward_amount", 0) or 0)
        if amount <= 0:
            raise RewardError("پاداش روزانه تنظیم نشده است")
    elif kind == "spin":
        prizes = await db.get_json("spin_prizes", DEFAULT_SPIN) or DEFAULT_SPIN
        prizes = [p for p in prizes if int(p.get("weight", 0)) > 0]
        if not prizes:
            raise RewardError("گردونه تنظیم نشده است")
        r = rng or random.SystemRandom()
        amount = int(r.choices(prizes, weights=[int(p["weight"]) for p in prizes])[0]["amount"])
    else:
        raise ValueError(kind)
    day = today()
    async with db.tx() as c:
        r = await c.execute(text("INSERT INTO reward_claims(user_id, kind, day, amount, created_at) "
                                 "VALUES(:u, :k, :d, :a, :t) ON CONFLICT DO NOTHING"),
                            {"u": user_id, "k": kind, "d": day, "a": amount, "t": now()})
        if r.rowcount != 1:
            raise RewardError("امروز قبلاً دریافت کرده‌اید؛ فردا دوباره سر بزنید")
        if amount > 0:
            await c.execute(text("UPDATE users SET balance = balance + :a WHERE id = :u"), {"a": amount, "u": user_id})
            await c.execute(text("INSERT INTO ledger(user_id, amount, kind, ref, created_at) VALUES(:u, :a, 'reward', "
                                 ":r, :t)"), {"u": user_id, "a": amount, "r": f"{kind}:{day}", "t": now()})
    return amount


def describe_flags(flags: dict[str, tuple[bool, int]]) -> list[tuple[str, str, bool, int]]:
    return [(k, FLAGS[k][0], *flags.get(k, (FLAGS[k][1], 100))) for k in FLAGS]

