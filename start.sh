#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# 加载同目录 .env（不提交到 Git），便于一键部署时写死 OPENCLAW_HOME 等
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source ./.env
  set +a
fi

export OPENCLAW_MODEL_ADMIN_HOST="${OPENCLAW_MODEL_ADMIN_HOST:-0.0.0.0}"
export OPENCLAW_MODEL_ADMIN_PORT="${OPENCLAW_MODEL_ADMIN_PORT:-8765}"

port="${OPENCLAW_MODEL_ADMIN_PORT}"
if command -v ss >/dev/null 2>&1; then
  if ss -ltn 2>/dev/null | grep -qE ":${port}\\b"; then
    echo "[start] 警告: 端口 ${port} 已有进程监听，若启动失败请换 OPENCLAW_MODEL_ADMIN_PORT 或结束占用进程。"
  fi
fi

exec python3 server.py
