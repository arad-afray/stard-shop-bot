# معماری Stard Shop Bot نسخه‌ی ۳

## نمای کلی

```
                 ┌──────────────── Telegram ────────────────┐
                 │                                          │
          ┌──────▼──────┐   FSM (Redis)   ┌─────────────┐   │ پیام/اعلان
          │  ROLE=bot   │◄───────────────►│  ROLE=bot   │   │
          │ (aiogram)   │                 │ نمونه‌ی دوم │   │
          └──────┬──────┘                 └──────┬──────┘   │
                 │  تراکنش اتمیک: کسر پول + سفارش + کار صف (outbox)
                 ▼                               ▼          │
          ┌───────────────────────────────────────────────┐ │
          │      PostgreSQL (یا SQLite در Development)     │ │
          │ users · orders · ledger · jobs · locks · audit │ │
          └───────────────────────▲───────────────────────┘ │
                                  │ claim با lease / SKIP LOCKED
          ┌───────────────┐  ┌────┴──────────┐              │
          │ ROLE=worker   │  │ ROLE=worker   │──────────────┘
          │ صف + زمان‌بند  │  │ (هر تعداد)    │──► Stard API ◄── وب‌هوک امضاشده (/webhooks/stard)
          └───────────────┘  └───────────────┘
```

| ماژول | مسئولیت |
|---|---|
| `bot/db.py`, `bot/models.py` | لایه‌ی پایگاه داده روی SQLAlchemy Core (async)؛ شِمای واحد؛ همه‌ی عملیات مالی |
| `bot/migrations/`, `migrations_runner.py`, `migrate.py` | Alembic؛ ارتقای خودکار پایگاه داده‌ی نسخه‌ی ۱ و ۲ |
| `bot/shop.py` | منطق خرید: پیش‌قیمت، قیمت‌گذاری، بررسی‌های خرید، ثبت و پیگیری سفارش |
| `bot/commerce.py` | قانون‌های قیمت، Flash Sale، VIP، موجودی انبار، سقف خرید، نمایش، زبان |
| `bot/features.py` | Feature Flag با Rollout، مدیریت دکمه‌ها، پاداش روزانه و گردونه |
| `bot/queue.py`, `bot/worker.py` | صف کار پایدار، worker، کارهای دوره‌ای با رهبر واحد |
| `bot/locks.py` | قفل توزیع‌شده و محدودیت نرخ (پایگاه داده یا Redis) |
| `bot/monitor.py`, `bot/metrics.py` | heartbeat، Health Check، متریک، هشدار |
| `bot/http_server.py`, `bot/apikeys.py` | `/healthz`، `/readyz`، `/metrics`، `/api/v1/*`، وب‌هوک Stard |
| `bot/backups.py`, `bot/updater.py` | پشتیبان/بازگردانی و به‌روزرسانی از GitHub با Rollback |
| `bot/risk.py`, `bot/finance.py`, `bot/systools.py`, `bot/campaigns.py` | ریسک، گزارش مالی، ابزارهای سیستم، اعلان‌های زمان‌بندی‌شده |
| `bot/simulator.py` | شبیه‌ساز Stard برای Test Mode |
| `bot/logging_setup.py` | لاگ JSON با Correlation ID و حذف secret |
| `bot/handlers/` | تلگرام: `user`, `shop`, `topup`, `prices` (گروه)، `admin`, `ops`, `admin_shop`, `finance` |

## تضمین‌های مالی (چرا هیچ عملیاتی دو بار انجام نمی‌شود)

| خطر | محافظ |
|---|---|
| کسر دوباره‌ی موجودی | `UPDATE users SET balance = balance - p WHERE balance >= p` داخل تراکنش؛ قید `CHECK (balance >= 0)` |
| سفارش تکراری (دو کلیک، دو نمونه، ری‌استارت) | `orders.checkout_id` یکتا برای هر پیش‌فاکتور |
| تحویل دوباره در Stard | `Idempotency-Key: bot-<id>` روی هر ارسال؛ قفل توزیع‌شده‌ی `order:<id>`؛ یک کار فعال در صف برای هر سفارش |
| گم شدن سفارش بعد از پرداخت | الگوی outbox: کار `order.submit` در همان تراکنش کسر پول ثبت می‌شود |
| برگشت دوباره | شرط `refunded = 0` + کلید اصلی `refunds.order_id` + تراکنش واحد؛ سفارش `completed` هرگز برگشت نمی‌خورد |
| هم تحویل هم برگشت | برگشت دستی مدیر اول در Stard لغو می‌کند؛ سفارش «پاسخ‌گم‌شده» قبلش با همان کلید استعلام می‌شود |
| شارژ دوباره | `UPDATE topups … WHERE status = 'pending'`؛ رسید تکراری با `UNIQUE(user_id, file_unique_id)` |
| فروش با ضرر | قیمت نهایی، تخفیف، پاداش معرف همه کف قیمت خرید از Stard دارند |
| کد تخفیف بیش از سقف | `UPDATE coupons SET used = used + 1 WHERE used < max_uses` + `PRIMARY KEY(code, user_id)` |
| موجودی انبار منفی | `UPDATE product_controls SET sold = sold + q WHERE stock - sold >= q` در همان تراکنش |
| ردیابی | هر تغییر موجودی در `ledger`؛ هر کار حساس مدیر و سیستم در `audit_log` (در همان تراکنش) |

## صف و بازیابی

- `jobs`: `queued → running → done`، یا بعد از تمام شدن تلاش‌ها `dead` (Dead Letter؛ از پنل Retry).
- **Lease**: کار در حال اجرا تا `locked_until` مال یک worker است. worker کرش‌کرده → lease منقضی → worker دیگر می‌گیرد.
  هر بار گرفتن یک تلاش حساب می‌شود، پس کاری که worker را کرش می‌دهد بی‌نهایت تکرار نمی‌شود.
- **Fencing**: `complete`/`fail` فقط اگر `locked_by` هنوز همان worker باشد.
- **Backoff**: ۵، ۱۰، ۲۰، … ثانیه با jitter، حداکثر ۱۵ دقیقه. هر کار timeout دارد.
- PostgreSQL: `FOR UPDATE SKIP LOCKED`؛ SQLite: `BEGIN IMMEDIATE`.
- پیگیری سفارش: هر سفارش کار خودش را دارد با فاصله‌ی تطبیقی (۱۰ ثانیه ← ۵ دقیقه)؛ وب‌هوک Stard آن را فوری جلو می‌اندازد.
- کار دوره‌ای `recover_orders` سفارش‌های بدون کار فعال را دوباره در صف می‌گذارد؛ `stuck_orders` هشدار می‌دهد.

| سناریو | نتیجه |
|---|---|
| ربات وسط خرید کرش کند | یا تراکنش کامل نشده (پولی کم نشده)، یا کامل شده و کار ارسال در صف است |
| worker وسط کار کرش کند | lease منقضی می‌شود و worker دیگر با همان Idempotency-Key ادامه می‌دهد |
| API وسط درخواست قطع شود / پاسخ گم شود | سفارش `new` می‌ماند و با همان کلید دوباره فرستاده می‌شود؛ Stard سفارش تکراری نمی‌سازد |
| Timeout / 5xx / 429 | تلاش دوباره با backoff (و `Retry-After` برای 429)؛ پول برنمی‌گردد چون نتیجه معلوم نیست |
| 4xx قطعی | سفارش رد و پول یک بار برمی‌گردد |
| ری‌استارت سرور | صف و وضعیت‌ها در پایگاه داده‌اند؛ FSM خرید در Redis |

## چند نمونه (Multi-Instance)

- `ROLE=bot` چند نمونه + `ROLE=worker` چند نمونه، همه با یک PostgreSQL و یک Redis.
- حالت گفتگو (FSM) در Redis؛ قفل‌ها، محدودیت نرخ، تنظیمات (کش ۳ ثانیه‌ای)، Feature Flagها، هشدارها و heartbeat در پایگاه داده.
- کارهای دوره‌ای با قفل توزیع‌شده فقط روی یک نمونه اجرا می‌شوند. مهاجرت PostgreSQL با `pg_advisory_lock` فقط یک بار.

## قیمت‌گذاری

```
قیمت = ceil( قیمت Stard × (۱ + سود بخش%) × (۱ + Σ قانون‌های فعال%) × (۱ − تخفیف VIP%) ) ≥ قیمت Stard
```
بدون قانون و VIP، دقیقاً همان قیمت نسخه‌ی ۲ است.

## امنیت

- همه‌ی secretها در `.env` (یا Secret Manager) و در کد `SecretStr`؛ هیچ secretی در سورس نیست.
- فیلتر لاگ روی همه‌ی رکوردها (حتی traceback): توکن ربات، `sk_live_/sk_test_`، `whsec_`، رمز داخل URL، توکن GitHub.
- پیام خطای HTTP جزئیات داخلی ندارد؛ پیام خطای پنل از `redact` عبور می‌کند.
- کلیدهای API ربات: فقط SHA-256 ذخیره می‌شود؛ کلید کامل یک بار نمایش داده می‌شود.
- وب‌هوک: HMAC-SHA256، پنجره‌ی ۵ دقیقه‌ای ضد replay، شناسه‌ی رویداد یکتا؛ وضعیت سفارش همیشه از API دوباره خوانده می‌شود.
- عملیات خطرناک پنل فقط برای مالک و با تأیید.
- محدودیت نرخ: خرید، شارژ، کد تخفیف، برگشت پول، کارهای مدیر، پیام همگانی، پاداش، API و تلاش ناموفق احراز هویت.
