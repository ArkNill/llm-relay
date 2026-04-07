#!/bin/bash
# Build llm-relay Docker image with tokpress vendored in
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOKPRESS_DIR="${TOKPRESS_DIR:-../tokpress}"

echo "Vendoring tokpress from $TOKPRESS_DIR ..."
rm -rf "$SCRIPT_DIR/vendor/tokpress"
mkdir -p "$SCRIPT_DIR/vendor/tokpress"
cp -r "$TOKPRESS_DIR/src" "$TOKPRESS_DIR/pyproject.toml" "$TOKPRESS_DIR/README.md" \
      "$SCRIPT_DIR/vendor/tokpress/"

echo "Building Docker image ..."
docker compose -f "$SCRIPT_DIR/docker-compose.yml" build

echo "Cleaning up vendor ..."
rm -rf "$SCRIPT_DIR/vendor"

echo "Done. Run: docker compose up -d"
