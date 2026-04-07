#!/bin/bash
# Stop llm-relay proxy
pkill -f "uvicorn llm_relay.proxy:app" 2>/dev/null && echo "llm-relay stopped" || echo "llm-relay not running"
