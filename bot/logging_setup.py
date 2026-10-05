"""لاگ ساختاریافته (JSON Lines) با Correlation ID و حذف خودکار secretها.

- هر آپدیت تلگرام، هر کار صف و هر درخواست HTTP یک correlation id می‌گیرد؛ همه‌ی لاگ‌های آن کار
  همین شناسه را دارند و در «📜 لاگ‌های سیستم» با آن قابل جستجو هستند.
- RedactFilter روی همه‌ی رکوردها (از جمله traceback) اجرا می‌شود: secretهای .env، توکن ربات، کلیدهای
  sk_live_/sk_test_، whsec_ و رمز داخل URLها پیش از نوشتن حذف می‌شوند.
"""
from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import re
import sys
import uuid
from datetime import datetime, timezone

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
service_name: contextvars.ContextVar[str] = contextvars.ContextVar("service_name", default="bot")

LOG_FILE = "bot.jsonl"

# الگوهای عمومی secret؛ حتی اگر در .env نباشند
_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),            # توکن ربات تلگرام
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{6,}\b"),        # کلید Stard
    re.compile(r"\bwhsec_[A-Za-z0-9]{6,}\b"),                   # secret وب‌هوک
    re.compile(r"\bsbk_[A-Za-z0-9_-]{10,}\b"),                  # کلید API خود ربات
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),              # توکن GitHub
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{8,}=*"),
    re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)"),                   # رمز داخل URL
]
_secrets: list[str] = []


def set_secrets(values: list[str]) -> None:
    global _secrets
    _secrets = sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)


def redact(text: str) -> str:
    if not text:
        return text
    for s in _secrets:
        if s in text:
            text = text.replace(s, "***")
    for p in _PATTERNS:
        if p.groups == 2:
            text = p.sub(r"\1***\2", text)
        elif p.groups == 1:
            text = p.sub(r"\1***", text)
        else:
            text = p.sub("***", text)
    return text


def new_correlation_id(prefix: str = "") -> str:
    cid = (prefix + uuid.uuid4().hex[:12]) if prefix else uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        record.exc_info = None if record.exc_text else record.exc_info
        record.cid = correlation_id.get()
        record.service = service_name.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "service": getattr(record, "service", "bot"),
            "logger": record.name,
            "cid": getattr(record, "cid", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_text:
            data["exc"] = record.exc_text
        return json.dumps(data, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (f"{datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')} {record.levelname} "
                f"[{getattr(record, 'cid', '-')}] {record.name}: {record.getMessage()}")
        if record.exc_text:
            base += "\n" + record.exc_text
        return base


def setup_logging(level: str = "INFO", log_dir: str | None = "logs", secrets: list[str] | None = None) -> None:
    set_secrets(secrets or [])
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    flt = RedactFilter()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    console.addFilter(flt)
    root.addHandler(console)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(os.path.join(log_dir, LOG_FILE), maxBytes=10 * 1024 * 1024,
                                                  backupCount=10, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        fh.addFilter(flt)
        root.addHandler(fh)
    # لاگ httpx آدرس کامل درخواست‌ها را می‌نویسد؛ در INFO لازم نیست
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.WARNING)


def read_logs(log_dir: str, *, level: str | None = None, search: str | None = None, service: str | None = None,
              since: str | None = None, until: str | None = None, limit: int = 30) -> list[dict]:
    """خواندن لاگ‌ها (جدیدترین اول) از فایل اصلی و فایل‌های چرخیده."""
    levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    min_level = levels.get((level or "").upper(), 0)
    files = [os.path.join(log_dir, LOG_FILE)] + [os.path.join(log_dir, f"{LOG_FILE}.{i}") for i in range(1, 11)]
    out: list[dict] = []
    needle = (search or "").lower()
    for path in files:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if levels.get(e.get("level", ""), 0) < min_level:
                continue
            if service and e.get("service") != service:
                continue
            if since and e.get("ts", "") < since:
                continue
            if until and e.get("ts", "") > until:
                continue
            if needle and needle not in line.lower():
                continue
            out.append(e)
            if len(out) >= limit:
                return out
    return out
