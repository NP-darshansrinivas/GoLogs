# GoLogs backend container image.
#
# Per PRD §16: Ollama runs natively on the host, outside Docker — this
# image only contains the FastAPI backend + MCP server subprocess. It
# reaches Ollama via OLLAMA_HOST=http://host.docker.internal:11434 (set in
# docker-compose.yml), not by bundling a model runtime.

FROM python:3.12-slim AS base

# Volatility3 and python-evtx are pure Python, but python-evtx has a small
# native extension surface via some dependency chains on certain platforms;
# build-essential covers that without bloating the final layer excessively.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir --break-system-packages -e .

# Run as a non-root user (PRD §21 least-privilege) — the container never
# needs root once dependencies are installed.
RUN useradd --create-home --shell /bin/bash gologs \
    && mkdir -p /data/cases \
    && chown -R gologs:gologs /app /data
USER gologs

ENV CASE_STORAGE_DIR=/data/cases \
    DB_URL=sqlite:////data/copilot.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
