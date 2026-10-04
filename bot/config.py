"""تنظیمات ربات از متغیرهای محیطی (فایل .env)."""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(..., description="توکن ربات از @BotFather")
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list, description="آیدی عددی مدیرها، با کاما جدا")

    stard_api_key: str = Field(..., description="کلید sk_test_ یا sk_live_")
    stard_base_url: str = "https://stard-market.ir/api/v1"
    stard_pay_currency: str | None = Field(None, description="IRT یا TON؛ خالی = پیش‌فرض داشبورد")

    database_path: str = "data/shop.db"
    default_profit_percent: float = 10.0
    poll_interval_seconds: int = 20
    log_level: str = "INFO"

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

    @property
    def is_test(self) -> bool:
        return self.stard_api_key.startswith("sk_test_")


@lru_cache
def get_settings() -> Settings:
    return Settings()
