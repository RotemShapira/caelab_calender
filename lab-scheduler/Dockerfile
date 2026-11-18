FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal on purpose (lightweight stack)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Dedicated unprivileged user (UID/GID 10001) and persistent SQLite data dir
RUN groupadd -g 10001 appuser \
    && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--proxy-headers"]
