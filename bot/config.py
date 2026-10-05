"""تنظیمات ربات از متغیرهای محیطی (فایل .env). هیچ secretی در کد نیست.

Secretها (توکن ربات، کلید Stard، secret وب‌هوک، رمز پایگاه داده در DATABASE_URL) با SecretStr نگه داشته
می‌شوند تا در repr، لاگ و پیام خطا نمایش داده نشوند.
"""
from __future__ import annotations

import os
import socket
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = Field(..., description="توکن ربات از @BotFather")
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list, description="آیدی عددی مالک‌ها، با کاما")

    stard_api_key: SecretStr = Field(..., description="کلید sk_test_ یا sk_live_")
    stard_base_url: str = "https://stard-market.ir/api/v1"
    stard_pay_currency: str | None = Field(None, description="IRT یا TON؛ خالی = پیش‌فرض داشبورد")
    stard_webhook_secret: SecretStr | None = Field(None, description="whsec_… برای وب‌هوک Stard (اختیاری)")
    stard_timeout: float = 20.0

    # پایگاه داده: در Production، DATABASE_URL (PostgreSQL). اگر خالی باشد SQLite در DATABASE_PATH.
    database_url: SecretStr | None = None
    database_path: str = "data/shop.db"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: float = 60.0
    max_concurrent_purchases: int = 64

    # پروکسی تلگرام (اختیاری): http://user:pass@host:port یا socks5://host:port
    telegram_proxy: SecretStr | None = None

    # Redis (اختیاری ولی برای چند نمونه لازم): FSM مشترک، قفل و محدودیت نرخ
    redis_url: SecretStr | None = None

    default_profit_percent: float = 10.0
    poll_interval_seconds: int = 20
    log_level: str = "INFO"
    log_dir: str = "logs"
    backup_dir: str = "backups"
    backup_keep: int = 14
    backup_interval_hours: int = 24

    # نقش این پردازه: all (ربات + worker)، bot، یا worker — برای جدا کردن کار سنگین از ربات اصلی
    role: str = "all"
    worker_concurrency: int = 8
    instance_id: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")

    # سرور HTTP داخلی: /healthz، /readyz، /metrics، /webhooks/stard
    http_enabled: bool = True
    http_host: str = "127.0.0.1"
    http_port: int = 8080

    # به‌روزرسانی از GitHub Releases
    update_repo: str = "arad-afray/stard-shop-bot"
    github_token: SecretStr | None = None

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("stard_pay_currency", mode="before")
    @classmethod
    def _empty_currency(cls, v):
        if isinstance(v, str):
            v = v.strip().upper()
            return v or None
        return v

    @field_validator("database_url", "redis_url", "stard_webhook_secret", "github_token",
                     "telegram_proxy", mode="before")
    @classmethod
    def _empty_secret(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("all", "bot", "worker"):
            raise ValueError("ROLE must be all, bot or worker")
        return v

    @property
    def is_test(self) -> bool:
        return self.stard_api_key.get_secret_value().startswith("sk_test_")

    def secret_values(self) -> list[str]:
        """همه‌ی secretها برای فیلتر لاگ (redaction)."""
        out = []
        for v in (self.bot_token, self.stard_api_key, self.stard_webhook_secret, self.database_url, self.redis_url,
                  self.github_token, self.telegram_proxy):
            if v is not None:
                s = v.get_secret_value()
                if s:
                    out.append(s)
                    # رمز داخل URL جداگانه هم پنهان شود
                    if "://" in s and "@" in s:
                        cred = s.split("://", 1)[1].split("@", 1)[0]
                        if ":" in cred:
                            out.append(cred.split(":", 1)[1])
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
