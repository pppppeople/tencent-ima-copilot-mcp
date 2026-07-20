#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
export IMA_ENV_FILE="${IMA_ENV_FILE:-$HOME/.claude/ima/.env}"
cd "$PROJECT_DIR"

exec "$PROJECT_DIR/.venv/bin/python" \
  -c 'from fastmcp.cli import app; app()' \
  run ima_server_simple.py:mcp \
  --transport http \
  --host 127.0.0.1 \
  --port 8081
