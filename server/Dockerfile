# SharkByte API. Build from the server/ directory:
#   docker build -t sharkbyte-server server/
#   docker run --rm --env-file server/.env -p 8000:8000 sharkbyte-server
# Built and run locally once (2026-11-07): see the status section of docs/DEPLOYMENT.md for exactly what was checked.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so rebuilds that only change code reuse this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Never run as root. Secrets come from environment variables at run time, never from the image.
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8000'), timeout=4)"

# ONE worker on purpose: live workout state (rep counters, coach, sensor buffer) is in memory, so several worker
# processes would split a workout's frames between them. Platforms that inject $PORT are supported.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
