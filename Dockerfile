FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN addgroup --system app && \
    adduser --system --ingroup app --home /app app && \
    mkdir -p /app/data /app/docker && \
    chmod 755 /app/docker/entrypoint.sh && \
    chown -R app:app /app

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import os, urllib.parse, urllib.request; port = os.getenv('PORT', os.getenv('TRAINING_HUB_PORT', '8080')); host = (os.getenv('TRAINING_HUB_ALLOWED_HOSTS', '').split(',')[0].strip() or urllib.parse.urlsplit(os.getenv('TRAINING_HUB_PUBLIC_BASE_URL', '')).hostname or '127.0.0.1'); request = urllib.request.Request(f'http://127.0.0.1:{port}/api/v1/health', headers={'Host': host, 'X-Forwarded-Proto': 'https'}); urllib.request.urlopen(request, timeout=3)"

ENTRYPOINT ["/app/docker/entrypoint.sh"]
