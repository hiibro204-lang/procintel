FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# libpq for psycopg, and the libs PyMuPDF needs at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY tests ./tests

RUN useradd -m -u 1000 appuser && mkdir -p /data/archive && chown -R appuser /data
USER appuser

ENV ARCHIVE_DIR=/data/archive \
    DATABASE_URL=sqlite:////data/procintel.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
