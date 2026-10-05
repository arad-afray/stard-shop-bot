"""مدیرها: مالک‌ها از ADMIN_IDS در .env، مدیرهای اضافه از پنل (فقط مالک می‌تواند اضافه/حذف کند)."""
from __future__ import annotations

from .db import Database


class Admins:
    def __init__(self, db: Database, owners: list[int]):
        self.db = db
        self.owners = set(owners)
        self.extra: set[int] = set()

    async def load(self) -> None:
        self.extra = {int(x) for x in await self.db.get_json("admins", []) if str(x).lstrip("-").isdigit()}

    def is_owner(self, uid: int) -> bool:
        return uid in self.owners

    def is_admin(self, uid: int) -> bool:
        return uid in self.owners or uid in self.extra

    def all(self) -> list[int]:
        return sorted(self.owners | self.extra)

    async def add(self, uid: int, by: int | None = None) -> bool:
        await self.load()
        if self.is_admin(uid):
            return False
        self.extra.add(uid)
        await self.db.set_json("admins", sorted(self.extra))
        await self.db.audit(admin_id=by, action="admin_add", user_id=uid)
        return True

    async def remove(self, uid: int, by: int | None = None) -> bool:
        await self.load()
        if uid not in self.extra:
            return False
        self.extra.discard(uid)
        await self.db.set_json("admins", sorted(self.extra))
        await self.db.audit(admin_id=by, action="admin_remove", user_id=uid)
        return True

    async def refresh(self) -> None:
        """برای چند نمونه: فهرست مدیرها دوره‌ای از پایگاه داده خوانده می‌شود."""
        await self.load()
