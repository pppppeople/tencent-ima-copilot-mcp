#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.pp.ima-copilot-mcp"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
export IMA_ENV_FILE="${IMA_ENV_FILE:-$HOME/.claude/ima/.env}"

cd "$PROJECT_DIR"

"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/update_cookie.py" --headers-file -

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
  echo "已重启 IMA MCP 常驻服务。"
else
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "已启动 IMA MCP 常驻服务。"
fi

sleep 1
if curl --max-time 3 -fsS -H 'Accept: text/event-stream' http://127.0.0.1:8081/mcp >/dev/null 2>&1; then
  echo "IMA MCP 端口已响应。"
else
  echo "IMA MCP 服务已重启；端口探测未返回成功，稍后可再试一次。"
fi
