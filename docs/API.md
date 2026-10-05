# راهنمای Stard API برای این ربات

این سند توضیح می‌دهد ربات از کدام بخش‌های [Stard API](https://stard-market.ir/docs) استفاده می‌کند و چرا. مرجع کامل همه‌ی endpointها در [مستندات رسمی](https://stard-market.ir/docs) و فایل [OpenAPI](https://stard-market.ir/api/v1/openapi.json) است.

## ۱. پایه

| مورد | مقدار |
|---|---|
| آدرس پایه | `https://stard-market.ir/api/v1` |
| قالب | JSON با UTF-8 روی HTTPS |
| احراز هویت | هدر `Authorization: Bearer sk_test_…` یا `sk_live_…` |
| واحد پول | تومان، عدد صحیح (`"currency": "IRT"`) |
| زمان | ISO 8601 به وقت UTC |

> ⛔️ کلید را هیچ‌وقت در URL نگذارید (`?api_key=` با خطای `key_in_url` رد می‌شود) و هیچ‌وقت در کد سمت کاربر قرار ندهید. در این ربات کلید فقط در فایل `.env` روی سرور است.

### محیط test و live

| | `sk_test_` | `sk_live_` |
|---|---|---|
| قیمت و محصولات | واقعی | واقعی |
| سفارش | شبیه‌سازی (`ord_test_…`) | سفارش واقعی |
| پول | ۱۰۰ میلیون تومان آزمایشی | کیف پول واقعی حساب Stard |
| رفتار سفارش | بعد از ۱۵ ثانیه خودکار `completed` | انجام واقعی |

همه‌ی اعتبارسنجی‌های live در test هم اجرا می‌شوند. پس کدی که در test کار کند، در live هم کار می‌کند. برای رفتن به live فقط کلید را عوض کنید.

## ۲. endpointهایی که ربات استفاده می‌کند

| کار در ربات | درخواست | فایل |
|---|---|---|
| بررسی کلید هنگام شروع | `GET /ping` | `bot/__main__.py` |
| فهرست پلن‌های پریمیوم | `GET /premium/prices` | `bot/shop.py` |
| فهرست گیفت‌های استارزی | `GET /gifts` | `bot/shop.py` |
| جزئیات یک محصول | `GET /products/{id}` | `bot/shop.py` |
| پیش‌قیمت قفل‌شده | `POST /orders/quote` | `bot/shop.py` |
| ثبت سفارش | `POST /orders` | `bot/shop.py` |
| پیگیری وضعیت | `GET /orders/{id}` | `bot/shop.py`، `bot/worker.py` |
| موجودی کیف پول API | `GET /wallet` | پنل مدیریت |
| تعیین نتیجه‌ی سفارش test | `POST /test/orders/{id}/simulate` | پنل مدیریت |
| نرخ استارز، TON و دلار (قیمت در گروه، ریکشن) | `GET /prices` | `bot/shop.py` |
| کاتالوگ بوست (مدت‌ها و قیمت هر بوست) | `GET /boosts` | `bot/shop.py` |
| پیش‌قیمت بوست | `POST /orders/quote` با `type=boost` و `duration` | `bot/shop.py` |
| ثبت سفارش بوست | `POST /boosts/orders` | `bot/shop.py` |
| پیگیری سفارش بوست | `GET /boosts/orders/{id}` | `bot/shop.py` |
| لغو سفارش شروع‌نشده (لغو دستی مدیر) | `POST /orders/{id}/cancel` | `bot/shop.py` |
| وضعیت سرویس‌ها و تراکنش‌ها | `GET /status`، `GET /transactions` | پنل مدیریت |

کلاینت کامل در `bot/stard_api.py` است. کاتالوگ‌ها و نرخ‌ها ۶۰ ثانیه کش می‌شوند تا ربات سریع بماند و از سقف ۶۰ درخواست در دقیقه رد نشود. پیش‌قیمت سفارش هیچ‌وقت کش نمی‌شود.

> ❤️ **ریکشن استارزی** endpointی در Stard API ندارد. ربات قیمتش را از `GET /prices` (قیمت یک استارز × تعداد) حساب می‌کند و سفارش را برای انجام دستی به مدیر می‌فرستد.

## ۳. جریان یک خرید، قدم به قدم

### قدم ۱: پیش‌قیمت

```http
POST /api/v1/orders/quote
{"type": "stars", "quantity": 500}
```

```json
{
  "object": "quote",
  "id": "qt_1790963122_2249000_122dd345…",
  "amount": 2249000,
  "unit_price": 4498,
  "currency": "IRT",
  "expires_at": "2026-10-02T17:45:22Z"
}
```

برای محصول (پریمیوم یا گیفت) به جای `quantity` مقدار `"type": "product", "product_id": 4518` فرستاده می‌شود.

`id` یک **quote_id** امضاشده است که ۲ دقیقه اعتبار دارد. ربات آن را همراه سفارش می‌فرستد. اگر قیمت در این فاصله بالا برود، سفارش با `409 price_changed` رد می‌شود و **پولی کسر نمی‌شود**. ربات اگر از گرفتن قیمت بیش از ۱۰۰ ثانیه گذشته باشد، پیش‌قیمت تازه می‌گیرد. اگر قیمت بالا رفته باشد، دوباره از کاربر تأیید می‌خواهد.

### قدم ۲: قیمت فروش با سود

```
قیمت فروش = ceil(amount × (1 + درصد_سود / 100))  ← گرد به بالا تا مضرب ۱٬۰۰۰ تومان
```

مثال: قیمت Stard برابر ۲٬۲۴۹٬۰۰۰ و سود ۱۰٪ است، پس قیمت فروش ۲٬۴۷۳٬۹۰۰ می‌شود که گرد به بالا **۲٬۴۷۴٬۰۰۰ تومان** است.
محاسبه با عدد صحیح انجام می‌شود (`bot/pricing.py`) تا خطای ممیز شناور پیش نیاید. گرد کردن هم همیشه به بالاست، پس قیمت فروش هیچ‌وقت کمتر از قیمت خرید نمی‌شود.

### قدم ۳: کسر موجودی کاربر و ثبت سفارش

ربات اول در **یک تراکنش SQLite** موجودی کاربر را کم می‌کند و سفارش محلی می‌سازد (`create_order_and_debit`). بعد درخواست زیر را می‌فرستد:

```http
POST /api/v1/orders
Idempotency-Key: bot-42
{
  "type": "stars",
  "quantity": 500,
  "recipient": "@username",
  "quote_id": "qt_…",
  "metadata": {"bot_order_id": 42, "user_id": 123456789}
}
```

```json
{
  "object": "order",
  "id": "ord_test_gGToQWxqOw89ssiL",
  "status": "pending",
  "amount": {"amount": 2249000, "currency": "IRT"},
  "failure_reason": null,
  "metadata": {"bot_order_id": 42, "user_id": 123456789}
}
```

- برای پریمیوم و گیفت: `"type": "product", "product_id": …` فرستاده می‌شود. برای گیفت `gift_message` هم اضافه می‌شود.
- **Idempotency-Key** برابر `bot-<شماره سفارش>` است. اگر شبکه قطع شود و پاسخ نرسد، ربات همان درخواست را با همان کلید دوباره می‌فرستد. Stard در این حالت سفارش تازه نمی‌سازد و پاسخ قبلی را برمی‌گرداند، پس خطر **دوبار خرید** وجود ندارد.

### بوست

```http
POST /api/v1/orders/quote
{"type": "boost", "quantity": 10, "duration": 7}

POST /api/v1/boosts/orders
Idempotency-Key: bot-43
{"recipient": "@mychannel", "quantity": 10, "duration": 7, "quote_id": "qt_…", "metadata": {…}}
```

مدت‌های قابل فروش و سقف تعداد از `GET /boosts` خوانده می‌شوند. خطاهای `boost_unavailable` و `boost_disabled` به کاربر پیام مناسب نشان می‌دهند و پولی کسر نمی‌شود.

### قدم ۴: پیگیری خودکار

`bot/worker.py` هر `POLL_INTERVAL_SECONDS` ثانیه سفارش‌های باز را با `GET /orders/{id}` بررسی می‌کند:

| status در Stard | معنی | کار ربات |
|---|---|---|
| `pending` | پرداخت شده، در صف | — |
| `processing` | در حال انجام | اطلاع به کاربر |
| `completed` | تحویل شد | اطلاع به کاربر |
| `cancelled` / `refunded` / `failed` | پول به کیف پول API برگشت | **برگشت پول به کاربر** (فقط یک بار) و اطلاع |

## ۴. خطاها

قالب همه‌ی خطاها یکسان است:

```json
{
  "error": {
    "type": "invalid_request_error",
    "code": "parameter_invalid",
    "message": "حداقل خرید استارز ۵۰ عدد است.",
    "param": "quantity",
    "request_id": "req_XZV2dfm7YHW6ECeAA1bW"
  }
}
```

تصمیم‌گیری همیشه بر اساس `code` است، نه `message`. این جدول رفتار ربات (`StardClient` و `Shop.submit`) را نشان می‌دهد:

| HTTP | نمونه‌ی code | رفتار ربات |
|---|---|---|
| 400 | `parameter_invalid`، `recipient_invalid`، `not_fixed_price` | سفارش رد و پول کاربر برمی‌گردد |
| 401 | `invalid_api_key` | خطا در لاگ؛ کلید را بررسی کنید |
| 402 | `insufficient_funds` | موجودی **کیف پول API** کم است؛ پول کاربر برمی‌گردد. کیف پول Stard را شارژ کنید |
| 403 | `account_not_linked`، `insufficient_scope` | پول کاربر برمی‌گردد؛ تنظیمات کلید را درست کنید |
| 404 | `product_not_found` | پول کاربر برمی‌گردد |
| 409 | `price_changed`، `product_unavailable` | پول کاربر برمی‌گردد |
| 409 | `request_in_progress` | سفارش می‌ماند و دوباره فرستاده می‌شود |
| 429 | `rate_limit_exceeded` | صبر به اندازه‌ی `Retry-After`، بعد تلاش دوباره |
| 5xx / قطعی شبکه | — | تلاش دوباره با فاصله‌ی ۱، ۲، ۴ و ۸ ثانیه. اگر باز هم نشد، worker بعداً با همان Idempotency-Key می‌فرستد |

درخواست POST **فقط** وقتی دوباره فرستاده می‌شود که Idempotency-Key داشته باشد.

## ۵. محدودیت نرخ

| سطح | سقف |
|---|---|
| هر کلید | ۶۰ درخواست در دقیقه |
| کل حساب | ۱۲۰۰ درخواست در دقیقه |
| ثبت سفارش هر کلید | ۲۰ در دقیقه |

worker بین درخواست‌ها ۰٫۳ ثانیه فاصله می‌گذارد و با دیدن 429 آن دور را متوقف می‌کند.

## ۶. تست با محیط test

1. در [داشبورد](https://stard-market.ir/developers/dashboard) یک کلید `sk_test_` بسازید و در `.env` بگذارید.
2. موجودی آزمایشی کیف پول API ۱۰۰ میلیون تومان است.
3. از پنل مدیریت، **👥 مدیریت کاربر** را بزنید و به حساب خودتان موجودی بدهید (یا شارژ کارت‌به‌کارت را تست کنید).
4. خرید کنید. سفارش test بعد از ۱۵ ثانیه خودکار `completed` می‌شود.
5. برای تست شکست، قبل از ۱۵ ثانیه از **🧪 شبیه‌سازی سفارش** گزینه‌ی ❌ را بزنید. ربات باید پول را برگرداند و به کاربر خبر بدهد.

```bash
# تست دستی کلید
curl "https://stard-market.ir/api/v1/ping" -H "Authorization: Bearer $STARD_API_KEY"
```
