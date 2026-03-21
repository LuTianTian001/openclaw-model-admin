#!/usr/bin/env bash
# 一键获取代码并启动。支持：git 克隆 或 无 git（源码包）；启动前预检 Python / 配置路径。
set -euo pipefail

DEFAULT_REPO="LuTianTian001/openclaw-model-admin"
REPO="${REPO:-$DEFAULT_REPO}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/openclaw-model-admin}"
BRANCH="${BRANCH:-main}"
# 1=git（默认），0=从 GitHub archive 下载 tar.gz（仅需 curl 或 wget）
USE_GIT="${USE_GIT:-1}"
SKIP_OPENCLAW_CHECK="${SKIP_OPENCLAW_CHECK:-0}"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[install] 缺少命令: $1"
    exit 1
  fi
}

download_to() {
  local url="$1" out="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$out"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$out" "$url"
  else
    echo "[install] 需要 curl 或 wget 以下载源码包"
    exit 1
  fi
}

echo "[install] Python 版本检查（需 >= 3.10）"
need_cmd python3
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  echo "[install] 当前 Python 版本过低，请安装 Python 3.10+"
  exit 1
}

resolve_default_config() {
  if [[ -n "${OPENCLAW_CONFIG_PATH:-}" ]]; then
    printf '%s' "$OPENCLAW_CONFIG_PATH"
    return
  fi
  if [[ -n "${OPENCLAW_HOME:-}" ]]; then
    printf '%s' "${OPENCLAW_HOME%/}/openclaw.json"
    return
  fi
  printf '%s' "$HOME/.openclaw/openclaw.json"
}

CFG="$(resolve_default_config)"
if [[ ! -f "$CFG" ]]; then
  echo "[install] 警告: 未找到 OpenClaw 配置文件: $CFG"
  echo "        若数据不在 ~/.openclaw，请在启动前导出 OPENCLAW_HOME 或 OPENCLAW_CONFIG_PATH（可写入安装目录下的 .env）。"
else
  echo "[install] 已检测到配置: $CFG"
fi

if [[ "$SKIP_OPENCLAW_CHECK" != "1" ]] && ! command -v openclaw >/dev/null 2>&1; then
  echo "[install] 警告: PATH 中未找到 openclaw，保存配置时「校验」将失败。"
  echo "        请安装 OpenClaw CLI，或在 .env 中设置 OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE=1（仅建议测试环境）。"
fi

sync_from_archive() {
  local repo_name="${REPO##*/}"
  local url="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  echo "[install] 下载源码包: $url"
  download_to "$url" "$tmp/src.tgz"
  tar -xzf "$tmp/src.tgz" -C "$tmp"
  local src="$tmp/${repo_name}-${BRANCH}"
  if [[ ! -d "$src" ]]; then
    echo "[install] 解压后未找到预期目录: ${repo_name}-${BRANCH}"
    exit 1
  fi
  mkdir -p "$INSTALL_DIR"
  if command -v rsync >/dev/null 2>&1; then
    # 不使用 --delete，避免删掉本机 .env、admin-prefs.json 等未入仓文件
    rsync -a --exclude='.env' --exclude='admin-prefs.json' "$src/" "$INSTALL_DIR/"
  else
    echo "[install] 未找到 rsync，使用 cp -a（不会删除 INSTALL_DIR 中已删除的上游文件）"
    ( shopt -s dotglob 2>/dev/null || true; cp -a "$src"/* "$INSTALL_DIR/" 2>/dev/null || true )
    for f in "$src"/.[!.]* "$src"/..?*; do
      [[ -e "$f" ]] || continue
      base=$(basename "$f")
      [[ "$base" == "." || "$base" == ".." ]] && continue
      [[ "$base" == ".env" ]] && continue
      cp -a "$f" "$INSTALL_DIR/" 2>/dev/null || true
    done
  fi
}

if [[ "$USE_GIT" == "1" ]]; then
  need_cmd git
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "[install] 目录已存在，更新并启动: $INSTALL_DIR"
    cd "$INSTALL_DIR"
    git fetch origin "$BRANCH" 2>/dev/null || true
    git checkout "$BRANCH" 2>/dev/null || true
    git pull --ff-only origin "$BRANCH" 2>/dev/null || {
      echo "[install] git pull 失败，可尝试 USE_GIT=0 强制用源码包覆盖（会保留 .env）"
      exit 1
    }
  else
    echo "[install] 克隆 https://github.com/$REPO.git -> $INSTALL_DIR"
    git clone --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" "$INSTALL_DIR"
  fi
  cd "$INSTALL_DIR"
else
  need_cmd tar
  sync_from_archive
  cd "$INSTALL_DIR"
fi

chmod +x start.sh 2>/dev/null || true
chmod +x install.sh 2>/dev/null || true

echo "[install] 默认配置目录: \${OPENCLAW_HOME:-~/.openclaw}，详见 README「困难与对策」。"
echo "[install] 启动: http://127.0.0.1:${OPENCLAW_MODEL_ADMIN_PORT:-8765}"
exec ./start.sh
