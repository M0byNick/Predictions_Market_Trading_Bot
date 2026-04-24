FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

COPY src ./src
COPY scripts ./scripts
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

RUN mkdir -p /app/data /app/secrets

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD test -f /app/data/.heartbeat && \
        test $(($(date +%s) - $(stat -c %Y /app/data/.heartbeat))) -lt 300 || exit 1

CMD ["bash", "/app/docker-entrypoint.sh"]
