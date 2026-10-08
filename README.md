# 🛍 Stard Shop Bot — نسخه 3.2.2

> 📌 **نسخه‌ی فعلی: 3.2.2** (۲۰۲۶-۱۰-۰۸) — تغییرات این نسخه در [CHANGELOG.md](CHANGELOG.md)

ربات فروشگاهی حرفه‌ای تلگرام متصل به [Stard Market API](https://stard-market.ir/docs): **استارز**، **پریمیوم**،
**گیفت استارزی**، **بوست**، **ریکشن استارزی**، **گیفت NFT**، **یوزرنیم** و **شماره**، با پنل مدیریت کامل،
آماده‌ی Production، چندنمونه‌ای و قابل مانیتور.

- 📐 معماری و تضمین‌های مالی: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**
- 🚀 راه‌اندازی، Docker، انتقال به PostgreSQL، وب‌هوک: **[docs/DEPLOY.md](docs/DEPLOY.md)**
- 🔌 استفاده از Stard API: **[docs/API.md](docs/API.md)**
- 📝 تغییرات: **[CHANGELOG.md](CHANGELOG.md)** | دستورهای ویندوز: **[COMMANDS.txt](COMMANDS.txt)**

## ✨ امکانات

**کاربر**
- فروشگاه با ترتیب و نمایش قابل تنظیم؛ قیمت مخصوص هر کاربر (Flash Sale، قیمت زمان‌بندی‌شده، تخفیف VIP)
- تحویل خودکار؛ ریکشن استارزی دستی توسط مدیر (Stard برایش API ندارد)
- کد تخفیف و پیشنهاد اختصاصی، دعوت دوستان، پاداش روزانه و گردونه، قیمت لحظه‌ای (در گروه هم)
- کیف پول، شارژ کارت‌به‌کارت، تاریخچه‌ی سفارش، اعلان خودکار

**💹 قیمت در گروه و اینلاین**
- در گروه به «قیمت»، «قیمت تون»، «تون»، «استارز؟» و سؤال با تعداد مثل «10 تون»، «۵۰۰ استارز چنده؟»، «100 دلار»،
  «1k استارز»، «ده هزار استارز»، «۲٫۵ تون به تومان» جواب می‌دهد؛ تبدیل بین تومان، دلار، TON و استارز
- دکمه‌ی «🛒 خرید ۵۰۰ استارز» زیر جواب، مستقیم پیش‌فاکتور همان تعداد را در ربات باز می‌کند
- حالت اینلاین: «@ربات 10 تون» در هر چتی، حتی جایی که ربات عضو نیست
- همه از پنل: روشن/خاموش کل قابلیت و هر نوع سؤال جدا، انتخاب گروه‌ها، فاصله‌ی ضد اسپم، پاک شدن خودکار جواب،
  کلمه‌های دلخواه، و «🧪 امتحان یک پیام» برای دیدن جواب ربات قبل از استفاده

**پنل مدیریت** (`/admin`) — صفحه‌ی اصلی: 🛰 **مرکز فرمان استارد**
- 🛠 **سیستم**: Health Check، منابع، لاگ‌ها (فیلتر/جستجو/دانلود)، Backup Manager، به‌روزرسانی از GitHub، Migration
  Manager، API Diagnostics، API Key Manager، Webhook Monitor، Queue Monitor، Security Scanner، DB Statistics،
  Dependency Checker، Audit Log، هشدارها، Maintenance Mode
- 🛍 **فروشگاه**: مدیریت دکمه‌ها، Feature Flag با Rollout، قیمت‌گذاری پویا و Flash Sale، موجودی و سقف خرید و نمایش و
  زبان، VIP، کد تخفیف، پاداش، Test Mode، درصد سود
- 📣 **بازاریابی**: پیام همگانی (صف)، کاربران غیرفعال و کمپین، دعوت خودکار به بازگشت، اعلان‌های هوشمند، ریسک و Ban خودکار،
  جوین اجباری، زیرمجموعه‌گیری، قیمت در گروه
- 💵 **مالی**: داشبورد درآمد با نمودار، داشبورد سود واقعی، Ledger، جستجوی تراکنش، Refund Center، Failed Payments،
  گزارش CSV/Excel/PDF، ماشین‌حساب کارمزد
- 👥 کارت کاربر با آمار کامل؛ 💳 شارژها؛ 🧾 سفارش‌ها؛ 👮 مدیرها

**Production**
- PostgreSQL (یا SQLite) + Alembic؛ Redis برای چند نمونه؛ صف پایدار با Retry/Backoff/Dead Letter؛ بازیابی بعد از کرش
- هیچ کسر دوباره، تحویل دوباره، برگشت دوباره یا سفارش تکراری — در همه‌ی سناریوهای خرابی (تست‌شده)
- متریک Prometheus، heartbeat، هشدار تلگرامی، لاگ JSON با Correlation ID، حذف secret از لاگ
- پشتیبان خودکار و دستی با بررسی سلامت و تست بازگردانی؛ به‌روزرسانی با پشتیبان، اعتبارسنجی و Rollback
- Restart خودکار (`run.ps1` / `run.sh` / Docker)؛ Audit Log برای همه‌ی کارهای حساس

## 🚀 شروع سریع

**ویندوز:**
```powershell
git clone https://github.com/arad-afray/stard-shop-bot.git
cd stard-shop-bot
python -m pip install -r requirements.txt
copy .env.example .env
notepad .env
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

**سرور (Docker + PostgreSQL + Redis):**
```bash
cp .env.example .env && nano .env   # + POSTGRES_PASSWORD
docker compose up -d --build
```

حداقل تنظیمات `.env`: `BOT_TOKEN`، `ADMIN_IDS`، `STARD_API_KEY`. همه‌ی متغیرها با توضیح در **[.env.example](.env.example)**.

**به‌روزرسانی از نسخه‌ی ۲:** فایل `update.ps1` را در پوشه‌ی پروژه بگذارید و اجرا کنید
(`powershell -ExecutionPolicy Bypass -File .\update.ps1`). داده‌ها حفظ می‌شوند و در صورت خطا همه‌چیز به حالت قبل برمی‌گردد.

## 🧪 تست‌ها

```bash
pip install -r requirements-dev.txt
pytest -q                     # SQLite
TEST_DATABASE_URL=postgresql+asyncpg://…/shop_test pytest -q   # PostgreSQL
```
۱۳۰+ تست واحد، یکپارچه، خرابی و بار: رقابت هم‌زمان، خرید/برگشت/شارژ تکراری، Timeout، 429، 5xx، پاسخ گم‌شده، کرش
worker، خرابی پایگاه داده، ری‌استارت سرور، بازیابی، ۴۰۰ خرید هم‌زمان با ۴ worker، و تست فشار ۵۰۰۰ خرید (`STRESS=1`).
همه‌ی صفحه‌ها و فرم‌های پنل از طریق Dispatcher واقعی aiogram تست می‌شوند و هر خطای مدیریت‌نشده تست را رد می‌کند.

## 📄 مجوز
[MIT](LICENSE)
