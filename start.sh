#!/bin/bash
# cc-relay: start proxy + launch claude-patched through it
set -euo pipefail

PORT=${LLM_RELAY_PORT:-8080}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill existing proxy if running
pkill -f "uvicorn cc_relay.proxy:app" 2>/dev/null || true
sleep 1

# Start proxy in background
cd "$SCRIPT_DIR"
nohup .venv/bin/python3 -m uvicorn cc_relay.proxy:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning \
    > /tmp/cc-relay.log 2>&1 &
echo "cc-relay proxy started on :$PORT (PID $!)"

# Wait for proxy to be ready
for i in $(seq 1 10); do
    if curl -s "http://localhost:$PORT/_health" > /dev/null 2>&1; then
        echo "proxy ready"
        break
    fi
    sleep 0.5
done

# Launch claude-patched through the proxy
export ANTHROPIC_BASE_URL="http://localhost:$PORT"
exec claude-patched "$@"
