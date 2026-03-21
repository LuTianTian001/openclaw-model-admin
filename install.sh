#!/usr/bin/env bash
# 一键克隆并启动（可被 curl | bash 调用）
set -euo pipefail

DEFAULT_REPO="LuTianTian001/openclaw-model-admin"
REPO="${REPO:-$DEFAULT_REPO}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/openclaw-model-admin}"
BRANCH="${BRANCH:-main}"

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "[install] 目录已存在，更新并启动: $INSTALL_DIR"
  cd "$INSTALL_DIR"
  git fetch origin "$BRANCH" 2>/dev/null || true
  git checkout "$BRANCH" 2>/dev/null || true
  git pull --ff-only origin "$BRANCH" 2>/dev/null || true
else
  echo "[install] 克隆 https://github.com/$REPO.git -> $INSTALL_DIR"
  git clone --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" "$INSTALL_DIR"
  cd "$INSTALL_DIR"
fi

chmod +x start.sh
echo "[install] 配置路径未设置时默认使用 ~/.openclaw/（见 README）"
echo "[install] 启动面板: http://127.0.0.1:${OPENCLAW_MODEL_ADMIN_PORT:-8765}"
exec ./start.sh
