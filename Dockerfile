FROM python:3.12-slim

WORKDIR /app

# Install build deps
RUN pip install --no-cache-dir hatchling

# tokpress is an optional compression layer the proxy will use if it can
# import it at runtime (see src/llm_relay/proxy/proxy.py -- ImportError
# is caught and the feature simply stays disabled). Its sources live
# outside this repository, so the Docker image ships without it; nothing
# else in the proxy depends on it.

# Install llm-relay dependencies
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir ".[proxy,pg]"

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
