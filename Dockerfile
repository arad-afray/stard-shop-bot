FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 STARD_DOCKER=1
WORKDIR /app
# pg_dump برای بررسی وابستگی‌ها و curl برای HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY alembic.ini ./
COPY bot ./bot
RUN useradd -m -u 1000 app && mkdir -p /app/data /app/logs /app/backups && chown -R app /app/data /app/logs /app/backups
USER app
EXPOSE 8080
# زنده بودن پردازه (سرور HTTP داخلی)؛ Docker با restart: unless-stopped کانتینر ناسالم را دوباره اجرا می‌کند
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "-m", "bot"]
