FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/Zolta-n/travel-manager" \
      org.opencontainers.image.description="Travel Manager NAS - private travel itinerary app"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 TMPDIR=/run/travel-reader \
    TRAVEL_DATA_DIR=/data TRAVEL_WORK_DIR=/run/travel-reader \
    TRAVEL_SEED_PATH=/data/seed.json

RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 travel \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent travel \
 && mkdir -p /app /data /seed /run/travel-reader \
 && chown 10001:10001 /data /run/travel-reader

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt
COPY ai_reader.py app.py store.py extractor.py reader_worker.py server.py initialize_data.py easy_data.py ./
COPY web/ ./web/
# Host file modes must never matter: Synology ACL shares extract files as 0700 owner-only,
# which Docker COPY preserves. Normalise ownership and modes so UID 10001 can read the code.
RUN chown -R 0:0 /app \
 && find /app -type d -exec chmod 0755 {} + \
 && find /app -type f -exec chmod 0644 {} +
USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-c", "import http.client,os; c=http.client.HTTPConnection('127.0.0.1',8787,timeout=3); c.request('GET','/api/session',headers={'Host':os.environ['TRAVEL_ALLOWED_HOSTS'].split(',')[0].strip()}); r=c.getresponse(); raise SystemExit(0 if r.status==200 else 1)"]
CMD ["python", "server.py"]
