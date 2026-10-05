"""قیمت‌گذاری پویا، Flash Sale، VIP، موجودی انبار، سقف خرید، نمایش محصول و دسترسی بر اساس زبان.

فرمول قیمت (به ترتیب):
  1. قیمت پایه = قیمت Stard × (۱ + درصد سود بخش)                  ← همان فرمول نسخه‌ی ۲
  2. قانون‌های قیمت فعال الان (Scheduled / Flash Sale / Segment): جمع درصدها اعمال می‌شود
  3. تخفیف سطح VIP کاربر
  4. کف قیمت: هرگز کمتر از قیمت خرید از Stard (فروش با ضرر ممکن نیست)
  5. گرد کردن به بالا تا واحد تنظیم‌شده
اگر هیچ قانون و VIPی فعال نباشد، قیمت دقیقاً همان قیمت نسخه‌ی ۲ است.

«دسترسی کشوری»: تلگرام کشور کاربر را به ربات نمی‌دهد؛ تنها نشانه‌ی قابل اتکا language_code است.
پس محدودیت بر اساس زبان تلگرام کاربر اعمال می‌شود (مثلاً fa,en).
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text

from .db import Database, User, ago, now, ts

SEGMENTS = ("all", "vip", "new")  # + vip:<level>
RULE_MIN, RULE_MAX = -90.0, 500.0


class PurchaseBlocked(Exception):
    """دلیلی که برای نمایش به کاربر مناسب است."""


def ceil_to(price: float, unit: int) -> int:
    p = int(-(-price // 1))
    return -(-p // unit) * unit if unit > 1 else p


class Commerce:
    def __init__(self, db: Database):
        self.db = db
        self._rules_cache: tuple[float, list[dict]] | None = None

    # ---------- VIP ----------
    async def levels(self) -> list[dict]:
        return await self.db.all("SELECT * FROM vip_levels ORDER BY level")

    async def set_level(self, level: int, name: str, discount: float, daily_limit: int | None = None,
                        min_spent: int | None = None, *, admin_id: int | None = None) -> None:
        if not 1 <= level <= 20 or not 0 <= discount <= 90:
            raise ValueError("invalid level")
        async with self.db.tx() as c:
            await c.execute(text(
                "INSERT INTO vip_levels(level, name, discount, daily_limit, min_spent) VALUES(:l, :n, :d, :dl, :m) "
                "ON CONFLICT(level) DO UPDATE SET name = excluded.name, discount = excluded.discount, "
                "daily_limit = excluded.daily_limit, min_spent = excluded.min_spent"),
                {"l": level, "n": name[:64], "d": discount, "dl": daily_limit, "m": min_spent})
            await self.db.audit_in(c, admin_id=admin_id, action="vip_level_set", ref=f"vip:{level}",
                                   after={"name": name, "discount": discount, "daily_limit": daily_limit,
                                          "min_spent": min_spent})

    async def delete_level(self, level: int, *, admin_id: int | None = None) -> None:
        async with self.db.tx() as c:
            await c.execute(text("DELETE FROM vip_levels WHERE level = :l"), {"l": level})
            await c.execute(text("DELETE FROM user_vip WHERE level = :l"), {"l": level})
            await self.db.audit_in(c, admin_id=admin_id, action="vip_level_delete", ref=f"vip:{level}")

    async def assign(self, user_id: int, level: int, days: int | None, *, admin_id: int | None = None) -> None:
        if await self.db.one("SELECT 1 AS x FROM vip_levels WHERE level = :l", {"l": level}) is None:
            raise ValueError("unknown level")
        from datetime import datetime, timedelta, timezone
        ends = ts(datetime.now(timezone.utc) + timedelta(days=days)) if days else None
        async with self.db.tx() as c:
            before = (await c.execute(text("SELECT level, ends_at FROM user_vip WHERE user_id = :u"),
                                      {"u": user_id})).first()
            await c.execute(text("INSERT INTO user_vip(user_id, level, starts_at, ends_at) VALUES(:u, :l, :s, :e) "
                                 "ON CONFLICT(user_id) DO UPDATE SET level = excluded.level, starts_at = excluded.starts_at, "
                                 "ends_at = excluded.ends_at"), {"u": user_id, "l": level, "s": now(), "e": ends})
            await self.db.audit_in(c, admin_id=admin_id, action="vip_assign", user_id=user_id,
                                   before=dict(before._mapping) if before else None,
                                   after={"level": level, "ends_at": ends})

    async def unassign(self, user_id: int, *, admin_id: int | None = None) -> None:
        async with self.db.tx() as c:
            await c.execute(text("DELETE FROM user_vip WHERE user_id = :u"), {"u": user_id})
            await self.db.audit_in(c, admin_id=admin_id, action="vip_unassign", user_id=user_id)

    async def user_level(self, user_id: int) -> dict | None:
        """سطح دستی فعال، وگرنه سطح خودکار بر اساس مجموع خرید."""
        row = await self.db.one(
            "SELECT v.*, u.starts_at, u.ends_at FROM user_vip u JOIN vip_levels v ON v.level = u.level "
            "WHERE u.user_id = :u AND u.starts_at <= :t AND (u.ends_at IS NULL OR u.ends_at > :t)",
            {"u": user_id, "t": now()})
        if row:
            return {**row, "source": "manual"}
        auto = await self.db.all("SELECT * FROM vip_levels WHERE min_spent IS NOT NULL AND min_spent > 0 "
                                 "ORDER BY min_spent DESC")
        if not auto:
            return None
        spent = await self.db.user_spent(user_id)
        lvl = next((lv for lv in auto if spent >= lv["min_spent"]), None)
        return {**lvl, "source": "auto", "ends_at": None} if lvl else None

    async def vip_stats(self) -> list[dict]:
        out = []
        for lv in await self.levels():
            manual = await self.db.scalar("SELECT COUNT(*) FROM user_vip WHERE level = :l AND "
                                          "(ends_at IS NULL OR ends_at > :t)", {"l": lv["level"], "t": now()})
            revenue = await self.db.scalar(
                "SELECT COALESCE(SUM(o.price), 0) FROM orders o JOIN user_vip u ON u.user_id = o.user_id "
                "WHERE u.level = :l AND o.status = 'completed' AND o.created_at >= u.starts_at", {"l": lv["level"]})
            out.append({**lv, "users": manual, "revenue": revenue})
        return out

    # ---------- قانون‌های قیمت ----------
    async def rules(self) -> list[dict]:
        return await self.db.all("SELECT * FROM price_rules ORDER BY id DESC LIMIT 50")

    async def add_rule(self, *, name: str, percent: float, category: str | None = None, product_id: int | None = None,
                       segment: str = "all", starts_at: str | None = None, ends_at: str | None = None,
                       admin_id: int | None = None) -> int:
        if not RULE_MIN <= percent <= RULE_MAX:
            raise ValueError("percent out of range")
        if segment not in SEGMENTS and not (segment.startswith("vip:") and segment[4:].isdigit()):
            raise ValueError("invalid segment")
        async with self.db.tx() as c:
            rid = (await c.execute(text(
                "INSERT INTO price_rules(name, category, product_id, percent, segment, starts_at, ends_at, active, "
                "created_at) VALUES(:n, :c, :p, :pc, :s, :st, :en, 1, :t) RETURNING id"),
                {"n": name[:128], "c": category, "p": product_id, "pc": percent, "s": segment, "st": starts_at,
                 "en": ends_at, "t": now()})).scalar_one()
            await self.db.audit_in(c, admin_id=admin_id, action="price_rule_add", ref=f"rule:{rid}",
                                   after={"name": name, "percent": percent, "category": category, "segment": segment,
                                          "starts_at": starts_at, "ends_at": ends_at})
        self._rules_cache = None
        return rid

    async def toggle_rule(self, rid: int, *, admin_id: int | None = None) -> None:
        async with self.db.tx() as c:
            await c.execute(text("UPDATE price_rules SET active = 1 - active WHERE id = :i"), {"i": rid})
            await self.db.audit_in(c, admin_id=admin_id, action="price_rule_toggle", ref=f"rule:{rid}")
        self._rules_cache = None

    async def delete_rule(self, rid: int, *, admin_id: int | None = None) -> None:
        async with self.db.tx() as c:
            await c.execute(text("DELETE FROM price_rules WHERE id = :i"), {"i": rid})
            await self.db.audit_in(c, admin_id=admin_id, action="price_rule_delete", ref=f"rule:{rid}")
        self._rules_cache = None

    async def _active_rules(self) -> list[dict]:
        if self._rules_cache is None or time.monotonic() - self._rules_cache[0] > 5:
            t = now()
            rows = await self.db.all("SELECT * FROM price_rules WHERE active = 1 AND (starts_at IS NULL OR "
                                     "starts_at <= :t) AND (ends_at IS NULL OR ends_at > :t)", {"t": t})
            self._rules_cache = (time.monotonic(), rows)
        return self._rules_cache[1]

    async def matching_rules(self, category: str, product_id: int | None, user: User | None,
                             level: dict | None) -> list[dict]:
        out = []
        for r in await self._active_rules():
            if r["category"] and r["category"] != category:
                continue
            if r["product_id"] and r["product_id"] != product_id:
                continue
            seg = r["segment"]
            if seg == "vip" and not level:
                continue
            if seg.startswith("vip:") and (not level or str(level["level"]) != seg[4:]):
                continue
            if seg == "new" and (user is None or (user.created_at or "") < ago(7)):
                continue
            out.append(r)
        return out

    async def adjust(self, price: int, base: int, category: str, *, user: User | None = None,
                     product_id: int | None = None, round_to: int = 1000) -> tuple[int, list[str]]:
        """مراحل ۲ تا ۵ فرمول. (قیمت نهایی، توضیح تغییرها)."""
        level = await self.user_level(user.id) if user is not None else None
        rules = await self.matching_rules(category, product_id, user, level)
        notes: list[str] = []
        if not rules and not (level and level["discount"]):
            return price, notes
        pct = max(RULE_MIN, min(RULE_MAX, sum(float(r["percent"]) for r in rules)))
        p = price * (1 + pct / 100)
        for r in rules:
            notes.append(f"{'🔥' if r['percent'] < 0 else '📈'} {r['name']} ({r['percent']:+g}%)")
        if level and level["discount"]:
            p *= 1 - float(level["discount"]) / 100
            notes.append(f"👑 {level['name']} (−{level['discount']:g}%)")
        return max(ceil_to(p, round_to), base), notes

    # ---------- کنترل محصول (انبار، سقف، نمایش، زبان) ----------
    async def control(self, key: str) -> dict | None:
        return await self.db.one("SELECT * FROM product_controls WHERE key = :k", {"k": key})

    async def controls(self) -> list[dict]:
        return await self.db.all("SELECT * FROM product_controls ORDER BY key")

    async def set_control(self, key: str, *, admin_id: int | None = None, **fields: Any) -> None:
        allowed = {"stock", "daily_limit", "hidden", "languages", "sold"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        before = await self.control(key)
        async with self.db.tx() as c:
            await c.execute(text("INSERT INTO product_controls(key) VALUES(:k) ON CONFLICT(key) DO NOTHING"), {"k": key})
            if fields:
                cols = ", ".join(f"{k} = :{k}" for k in fields)
                await c.execute(text(f"UPDATE product_controls SET {cols} WHERE key = :__k"), {**fields, "__k": key})
            await self.db.audit_in(c, admin_id=admin_id, action="product_control", ref=key, before=before, after=fields)

    async def delete_control(self, key: str, *, admin_id: int | None = None) -> None:
        async with self.db.tx() as c:
            await c.execute(text("DELETE FROM product_controls WHERE key = :k"), {"k": key})
            await self.db.audit_in(c, admin_id=admin_id, action="product_control_delete", ref=key)

    async def hidden_products(self, category: str) -> set[int]:
        rows = await self.db.all("SELECT key FROM product_controls WHERE hidden = 1 AND key LIKE :p",
                                 {"p": f"{category}:%"})
        out = set()
        for r in rows:
            try:
                out.add(int(r["key"].split(":", 1)[1]))
            except ValueError:
                pass
        return out

    async def stock_key(self, category: str, product_id: int | None) -> str | None:
        """کلید انباری که باید کسر شود: اول محصول، بعد کل بخش (اگر موجودی تعریف شده باشد)."""
        for key in ((f"{category}:{product_id}" if product_id else None), category):
            if key:
                c = await self.control(key)
                if c is not None and c["stock"] is not None:
                    return key
        return None

    async def check_purchase(self, user: User, category: str, product_id: int | None, quantity: int,
                             *, is_admin: bool = False) -> None:
        keys = [k for k in ((f"{category}:{product_id}" if product_id else None), category) if k]
        for key in keys:
            c = await self.control(key)
            if c is None:
                continue
            if c["hidden"] and not is_admin:
                raise PurchaseBlocked("این محصول فعلاً در دسترس نیست.")
            if c["languages"] and not is_admin:
                allowed = [x.strip().lower() for x in c["languages"].split(",") if x.strip()]
                lang = (user.language_code or "").lower()
                if allowed and not any(lang.startswith(a) for a in allowed):
                    raise PurchaseBlocked("این محصول در منطقه‌ی شما قابل خرید نیست.")
            if c["stock"] is not None and c["stock"] - c["sold"] < quantity:
                raise PurchaseBlocked("موجودی این محصول تمام شده است.")
            if c["daily_limit"] and not is_admin:
                used = await self.db.user_orders_since(user.id, ago(1), category=category,
                                                       product_id=product_id if ":" in key else None)
                unit = quantity if category in ("stars", "reaction", "boost") else 1
                if used + unit > c["daily_limit"]:
                    raise PurchaseBlocked(f"سقف خرید روزانه‌ی این محصول ({c['daily_limit']:,}) پر شده است.")
        level = await self.user_level(user.id)
        if level and level.get("daily_limit") and not is_admin:
            if await self.db.count_orders_since(user.id, ago(1)) >= level["daily_limit"]:
                raise PurchaseBlocked(f"سقف سفارش روزانه‌ی سطح شما ({level['daily_limit']}) پر شده است.")

    async def low_stock(self, key: str, threshold: int) -> int | None:
        c = await self.control(key)
        if c is None or c["stock"] is None:
            return None
        left = c["stock"] - c["sold"]
        return left if left <= threshold else None
