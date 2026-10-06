FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt .
RUN pip install --upgrade pip && pip install -r requirements-runtime.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 appuser

COPY --chown=appuser:appuser . .
# Runtime data must remain writable while the source tree stays read-only.
RUN mkdir -p /app/data/experiments && chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os, socket; s=socket.create_connection(('127.0.0.1', int(os.getenv('PORT', '8000'))), 4); s.close()"]

# Production schema changes are owned by Render's pre-deploy migration.
# The image itself must remain safe to start without mutating the database.
CMD ["python", "-c", "import os,uvicorn; uvicorn.run('main:app',host='0.0.0.0',port=int(os.getenv('PORT','8000')))"]
