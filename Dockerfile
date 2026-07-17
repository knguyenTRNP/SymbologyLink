FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SYMBOLOGYLINK_JOBS=/data/jobs.sqlite3 \
    SYMBOLOGYLINK_DATASETS=/data/datasets.sqlite3 \
    SYMBOLOGYLINK_UPLOADS=/data/uploads \
    SYMBOLOGYLINK_CACHE=/data/cache.sqlite3 \
    SYMBOLOGYLINK_RULES=/data/rules.json \
    SYMBOLOGYLINK_OVERRIDES=/data/overrides.jsonl \
    SYMBOLOGYLINK_RELATIONSHIP_MAX_DEPTH=8

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[all]"

RUN adduser --disabled-password --gecos "" --uid 10001 symbologylink && mkdir -p /data && chown -R symbologylink:symbologylink /data /app
USER symbologylink

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "symbologylink.api:app", "--host", "0.0.0.0", "--port", "8000"]
