#!/bin/bash
# Stop cc-relay proxy
pkill -f "uvicorn cc_relay.proxy:app" 2>/dev/null && echo "cc-relay stopped" || echo "cc-relay not running"
