FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/ArkNill/llm-relay"
LABEL org.opencontainers.image.description="Unified LLM usage management — API proxy, session diagnostics, multi-CLI orchestration"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Install llm-relay with proxy dependencies
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir ".[proxy]"

# Data directory for SQLite DB
RUN mkdir -p /data

ENV LLM_RELAY_UPSTREAM=https://api.anthropic.com \
    LLM_RELAY_DB=/data/usage.db \
    LLM_RELAY_HISTORY=1 \
    LLM_RELAY_SSE_PARSE_USAGE=1

EXPOSE 8083

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8083/_health')" || exit 1

CMD ["python", "-m", "uvicorn", "llm_relay.proxy.proxy:app", \
     "--host", "0.0.0.0", "--port", "8083", "--log-level", "info"]
