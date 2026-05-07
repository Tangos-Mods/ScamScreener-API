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

EXPOSE 8080 8081 8082

ENTRYPOINT ["/app/docker/entrypoint.sh"]
