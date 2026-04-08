FROM python:3.12-slim

WORKDIR /app

# Install build deps
RUN pip install --no-cache-dir hatchling

# Install tokpress (from vendored copy)
COPY vendor/tokpress /tmp/tokpress
RUN pip install --no-cache-dir /tmp/tokpress && rm -rf /tmp/tokpress

# Install llm-relay dependencies
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir ".[proxy]"

# Copy source
COPY src/ src/

# Install llm-relay
RUN pip install --no-cache-dir .

# Data directory for SQLite DB
RUN mkdir -p /data

ENV LLM_RELAY_UPSTREAM=https://api.anthropic.com \
    LLM_RELAY_DB=/data/usage.db

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/_health')" || exit 1

CMD ["python", "-m", "uvicorn", "llm_relay.proxy.proxy:app", \
     "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
