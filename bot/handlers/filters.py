"""فیلترهای مشترک هندلرها."""
from __future__ import annotations

from typing import Any

from aiogram.filters import Filter


class IsAdmin(Filter):
    async def __call__(self, event: Any, is_admin: bool = False) -> bool:
        return is_admin
