FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_ENV=production \
    TRUST_PROXY=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd -m -u 10001 billing

COPY --chown=billing:billing . .

# instance/ is where billing.db lives — mount a volume here so data survives
RUN mkdir -p /app/instance && chown -R billing:billing /app/instance

# SQLite locks the whole DB for each write transaction, so extra gunicorn
# workers buy nothing and produce "database is locked". Switch to Postgres
# (DATABASE_URL) before raising GUNICORN_WORKERS.
ENV GUNICORN_WORKERS=1

EXPOSE 5000
USER billing

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5000/healthz')"

CMD ["sh", "-c", "exec gunicorn -w \"${GUNICORN_WORKERS:-1}\" -k gthread --timeout 60 -b 0.0.0.0:5000 app:app"]
