# راه‌اندازی و نگهداری (Production)

## گزینه‌ی ۱: ویندوز (یک سیستم، SQLite)

مناسب شروع کار و ترافیک کم تا متوسط.

```powershell
git clone https://github.com/arad-afray/stard-shop-bot.git
cd stard-shop-bot
python -m pip install -r requirements.txt
copy .env.example .env
notepad .env          # BOT_TOKEN، ADMIN_IDS، STARD_API_KEY
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

`run.ps1` ربات را با Restart خودکار اجرا می‌کند (کرش ← اجرای دوباره با فاصله‌ی افزایشی؛ جلوگیری از Restart Loop؛
برگشت خودکار نسخه‌ای که بعد از به‌روزرسانی بالا نمی‌آید).

به‌روزرسانی: `powershell -ExecutionPolicy Bypass -File .\update.ps1` — یا از داخل ربات: پنل ← 🛠 سیستم ← 🔄 بررسی آپدیت.

## گزینه‌ی ۲: سرور لینوکس با Docker (PostgreSQL + Redis) — پیشنهادی برای Production

```bash
git clone https://github.com/arad-afray/stard-shop-bot.git && cd stard-shop-bot
cp .env.example .env && nano .env      # + POSTGRES_PASSWORD (یک رمز قوی)
docker compose up -d --build
docker compose logs -f bot worker
docker compose up -d --scale worker=3  # worker بیشتر
```

سرویس‌ها: `postgres`، `redis`، `bot` (ROLE=bot)، `worker` (ROLE=worker). همه `restart: unless-stopped` و HEALTHCHECK دارند.
به‌روزرسانی در Docker: `git pull && docker compose up -d --build` (مهاجرت هنگام شروع خودکار اجرا می‌شود).

## گزینه‌ی ۳: لینوکس بدون Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
./run.sh
```
برای systemd: `ExecStart=/path/stard-shop-bot/run.sh` و `Restart=always`.

## تلگرام فیلتر است؟

در `.env` پروکسی بگذارید: `TELEGRAM_PROXY=socks5://127.0.0.1:1080` (یا `http://host:port`). فقط درخواست‌های تلگرام از پروکسی می‌روند.

## انتقال داده از SQLite به PostgreSQL

1. روی نسخه‌ی فعلی (SQLite): پنل ← 🛠 سیستم ← 💾 Backup Manager ← Create Backup، و فایل را دانلود کنید.
2. روی سرور جدید با `DATABASE_URL` (PostgreSQL) ربات را یک بار اجرا کنید تا جدول‌ها ساخته شوند.
3. فایل پشتیبان را در پوشه‌ی `backups/` سرور جدید بگذارید و از پنل ← Backup Manager ← Restore کنید.
   (این مسیر تست خودکار دارد: `tests/test_ops.py::test_sqlite_backup_restores_into_postgres`.)

## وب‌هوک Stard (اختیاری، سریع‌تر از polling)

1. یک reverse proxy با HTTPS (Caddy/Nginx) جلوی پورت 8080 بگذارید و فقط `/webhooks/stard` را عمومی کنید.
2. در داشبورد Stard وب‌هوک بسازید: `https://your-domain/webhooks/stard` و secret (`whsec_…`) را در `STARD_WEBHOOK_SECRET` بگذارید.
3. «ارسال آزمایشی» را بزنید؛ در پنل ← 🪝 Webhook Monitor باید `webhook.test` دیده شود.

## مانیتورینگ

| مسیر | کاربرد |
|---|---|
| `GET /healthz` | زنده بودن پردازه (Docker HEALTHCHECK) |
| `GET /readyz` | آماده بودن (پایگاه داده + worker) |
| `GET /metrics` | Prometheus؛ با کلید `metrics:read` (یا بدون کلید فقط وقتی `HTTP_HOST=127.0.0.1`) |
| `GET /api/v1/health`, `/api/v1/stats` | با کلید API (پنل ← 🔑 API Key Manager) |

هشدارها به مدیرها در تلگرام می‌رسند: ربات/worker/API از کار افتاده، خطای پایگاه داده، دیسک یا RAM بالا، افزایش خطا،
سفارش گیرکرده، افزایش برگشت پول، تأخیر API، عقب‌افتادن صف، کارهای dead، موجودی کم کیف پول Stard.

## پشتیبان

- خودکار هر `BACKUP_INTERVAL_HOURS` ساعت (پیش‌فرض ۲۴)، با بررسی سلامت؛ `BACKUP_KEEP` تا از هر نوع نگه داشته می‌شود.
- قبل از هر به‌روزرسانی، مهاجرت و بازگردانی خودکار.
- پوشه‌ی `backups/` را هم روی دیسک/سرور دیگری کپی کنید (پشتیبان روی همان دیسک در برابر خرابی دیسک محافظت نمی‌کند).

## انتشار نسخه‌ی جدید (برای توسعه‌دهنده)

1. `__version__` در `bot/__init__.py` و بخش جدید در `CHANGELOG.md`.
2. `git tag v3.1.0 && git push origin v3.1.0` ← workflow `release.yml` تست می‌گیرد، zip و `SHA256SUMS` می‌سازد و Release منتشر می‌کند.
3. ربات‌های نصب‌شده در «🔄 بررسی آپدیت» نسخه‌ی جدید را می‌بینند.

## اجرای تست‌ها

```bash
pip install -r requirements-dev.txt
pytest -q                                                      # SQLite
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/shop_test pytest -q   # PostgreSQL
STRESS=1 pytest -q tests/test_resilience.py -k stress -s       # ۵۰۰۰ خرید هم‌زمان
```
