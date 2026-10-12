# Immagine unica per scraper (con pianificatore) e dashboard.
# Base Playwright ufficiale: Chromium e dipendenze già installati, x86-64 e ARM64.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Rome \
    BNB_CONFIG_DIR=/app/config \
    BNB_DATA_DIR=/data

# pg_dump per i backup (stessa versione principale del server PostgreSQL 16).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bnbmonitor ./bnbmonitor
COPY dashboard ./dashboard
COPY tests ./tests

CMD ["python", "-m", "bnbmonitor", "scheduler"]
