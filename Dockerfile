FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EXPORTER_PORT=9402

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py .
COPY fetch_pricing.py .
COPY pricing.json .

# Refresh pricing at build time; falls back silently if the network is unavailable.
RUN python fetch_pricing.py

# Run as a non-root user
RUN useradd --create-home --uid 10001 exporter
USER exporter

EXPOSE 9402

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"EXPORTER_PORT\",\"9402\")}/metrics').read()" || exit 1

CMD ["python", "exporter.py"]
