#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# 可选：从本目录加载 .env（需自行安装并调用 `set -a; source .env; set +a`，此处不强制依赖）
export OPENCLAW_MODEL_ADMIN_HOST="${OPENCLAW_MODEL_ADMIN_HOST:-0.0.0.0}"
export OPENCLAW_MODEL_ADMIN_PORT="${OPENCLAW_MODEL_ADMIN_PORT:-8765}"
exec python3 server.py
