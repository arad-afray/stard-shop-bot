"""کلیدهای API خود ربات (برای /metrics و /api/v1/* سرور HTTP داخلی).

- کلید کامل فقط یک بار، هنگام ساخت، نمایش داده می‌شود؛ در پایگاه داده فقط SHA-256 آن ذخیره می‌شود.
- قالب: sbk_<prefix>_<secret>؛ prefix برای شناسایی در پنل است و secret قابل بازسازی نیست.
- مقایسه‌ی hash با hmac.compare_digest (ایمن در برابر timing attack).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from .db import Database, now

SCOPES = {
    "metrics:read": "خواندن متریک‌ها (/metrics)",
    "health:read": "خواندن وضعیت سلامت (/api/v1/health)",
    "stats:read": "خواندن آمار فروش (/api/v1/stats)",
}


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create_key(db: Database, name: str, scopes: list[str], *, admin_id: int | None = None) -> tuple[int, str]:
    bad = [s for s in scopes if s not in SCOPES]
    if bad or not scopes:
        raise ValueError(f"invalid scopes: {bad}")
    prefix = secrets.token_hex(4)
    key = f"sbk_{prefix}_{secrets.token_urlsafe(32)}"
    async with db.tx() as c:
        from sqlalchemy import text
        kid = (await c.execute(text(
            "INSERT INTO api_keys(name, prefix, hash, scopes, created_at) VALUES(:n, :p, :h, :s, :t) RETURNING id"),
            {"n": name[:64], "p": prefix, "h": _hash(key), "s": ",".join(sorted(scopes)), "t": now()})).scalar_one()
        await db.audit_in(c, admin_id=admin_id, action="api_key_create", ref=f"key:{kid}:{prefix}",
                          after={"name": name, "scopes": scopes})
    return kid, key


async def revoke_key(db: Database, kid: int, *, admin_id: int | None = None) -> bool:
    async with db.tx() as c:
        from sqlalchemy import text
        r = await c.execute(text("UPDATE api_keys SET revoked_at = :t WHERE id = :id AND revoked_at IS NULL"),
                            {"t": now(), "id": kid})
        if r.rowcount == 1:
            await db.audit_in(c, admin_id=admin_id, action="api_key_revoke", ref=f"key:{kid}")
        return r.rowcount == 1


async def rotate_key(db: Database, kid: int, *, admin_id: int | None = None) -> tuple[int, str] | None:
    """کلید تازه با همان نام و دسترسی‌ها؛ کلید قبلی باطل می‌شود."""
    row = await db.one("SELECT * FROM api_keys WHERE id = :id AND revoked_at IS NULL", {"id": kid})
    if row is None:
        return None
    new = await create_key(db, row["name"], row["scopes"].split(","), admin_id=admin_id)
    await revoke_key(db, kid, admin_id=admin_id)
    return new


async def list_keys(db: Database) -> list[dict]:
    return await db.all("SELECT id, name, prefix, scopes, created_at, last_used_at, revoked_at FROM api_keys "
                        "ORDER BY id DESC LIMIT 30")


async def verify(db: Database, key: str | None, scope: str) -> dict | None:
    if not key or not key.startswith("sbk_"):
        return None
    h = _hash(key)
    row = await db.one("SELECT * FROM api_keys WHERE hash = :h AND revoked_at IS NULL", {"h": h})
    if row is None or not hmac.compare_digest(row["hash"], h) or scope not in row["scopes"].split(","):
        return None
    await db.write("UPDATE api_keys SET last_used_at = :t WHERE id = :id", {"t": now(), "id": row["id"]})
    return row
