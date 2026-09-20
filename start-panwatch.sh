#!/usr/bin/env bash
# PanWatch 一键启动脚本 (Windows / Git Bash)
# 用法: bash start-panwatch.sh
#
# 说明:
#   - 后端: 本地 venv 启动 server.py (uvicorn :8000, reload)
#           注意: Docker 镜像版本落后于代码, 必须用本地 venv 才能跑最新功能
#   - 前端: vite dev server (:5183, /api 代理到 :8000)
#           优先 pnpm(dev 脚本), 否则直接用 node 调 vite 入口(绕过 pnpm 不在 PATH 的问题)
#   - 严格端口: 若 8000/5183 已被占用会直接退出, 请先 bash stop-panwatch.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

BACKEND_PORT=8000
FRONTEND_PORT=5183
LOG_DIR="$ROOT/.logs"
mkdir -p "$LOG_DIR"

GREEN='\033[32m'; YELLOW='\033[33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[PanWatch]${NC} $*"; }
warn() { echo -e "${YELLOW}[PanWatch]${NC} $*"; }

port_used() {
  # 只认真正监听的端口, 忽略 TIME_WAIT 等瞬时状态
  netstat -ano 2>/dev/null | grep -E ":$1 " | grep -q LISTENING
}

# ---- 端口占用检查 ----
if port_used "$BACKEND_PORT" || port_used "$FRONTEND_PORT"; then
  warn "端口 $BACKEND_PORT 或 $FRONTEND_PORT 已被占用, 请先运行: bash stop-panwatch.sh"
  exit 1
fi

# ---- 选择 python ----
PYTHON_BIN=""
for c in python python3 py; do
  if command -v "$c" >/dev/null 2>&1; then PYTHON_BIN="$c"; break; fi
done

# ===== 后端 =====
log "准备后端虚拟环境 (.venv)..."
if [ ! -d ".venv" ]; then
  if [ -z "$PYTHON_BIN" ]; then
    warn "未找到 python, 无法创建 venv, 请先安装 Python。"
    exit 1
  fi
  log "未找到 .venv, 正在创建并安装依赖..."
  "$PYTHON_BIN" -m venv .venv
  .venv/Scripts/python.exe -m pip install -q -r requirements.txt
  [ -f .env ] || [ ! -f .env.example ] || cp .env.example .env
fi
log "启动后端 (:8000) ..."
DEV_RELOAD=1 nohup .venv/Scripts/python.exe server.py > "$LOG_DIR/backend.log" 2>&1 &
echo $! > "$LOG_DIR/backend.pid"

# ===== 前端 =====
log "准备前端 (vite)..."
cd "$ROOT/frontend"
if [ ! -d "node_modules" ]; then
  warn "node_modules 不存在, 尝试安装依赖..."
  if command -v pnpm >/dev/null 2>&1; then
    pnpm install --no-frozen-lockfile
  else
    npm install
  fi
fi
log "启动前端 (:5183) ..."
if command -v pnpm >/dev/null 2>&1; then
  nohup pnpm dev > "$LOG_DIR/frontend.log" 2>&1 &
else
  nohup node node_modules/vite/bin/vite.js > "$LOG_DIR/frontend.log" 2>&1 &
fi
echo $! > "$LOG_DIR/frontend.pid"
cd "$ROOT"

# ===== 等待后端就绪 =====
log "等待后端健康检查 (/api/health) ..."
READY=0
for i in $(seq 1 30); do
  if curl -s -m 2 "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 1
done
if [ "$READY" -eq 1 ]; then
  log "后端已就绪。"
else
  warn "后端 30s 内未就绪, 请查看 $LOG_DIR/backend.log"
fi

echo
log "========================================================"
log " PanWatch 已启动"
log " 前端:  http://127.0.0.1:$FRONTEND_PORT   (/api 已代理到 :8000)"
log " 后端:  http://127.0.0.1:$BACKEND_PORT"
log " 日志:  $LOG_DIR/backend.log"
log "        $LOG_DIR/frontend.log"
log " 停止:  bash stop-panwatch.sh"
log "========================================================"
