#!/usr/bin/env bash
# 停止 PanWatch (按 PID 文件优先, 端口兜底强杀进程树)
# 用法: bash stop-panwatch.sh
#
# 同时处理: 后端 uvicorn(可能带 reload 子进程) 与 前端 vite(node)
# 端口 strictPort=true, 旧实例残留会导致重启失败, 因此端口兜底清理很重要

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/.logs"

GREEN='\033[32m'; YELLOW='\033[33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[PanWatch]${NC} $*"; }
warn() { echo -e "${YELLOW}[PanWatch]${NC} $*"; }

kill_pidfile() {
  local f="$1" name="$2" pid
  if [ -f "$f" ]; then
    pid="$(cat "$f" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      log "停止 $name (PID $pid) ..."
      taskkill.exe /PID "$pid" /T /F >/dev/null 2>&1 || kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$f"
  fi
}

kill_pidfile "$LOG_DIR/backend.pid"  "后端"
kill_pidfile "$LOG_DIR/frontend.pid" "前端"

# 端口兜底: 只强杀真正 LISTENING 的进程树(忽略 TIME_WAIT 残留)
for port in 8000 5183; do
  for pid in $(netstat -ano 2>/dev/null | grep -E ":$port " | grep LISTENING | awk '{print $NF}' | grep -E '^[0-9]+$'); do
    log "兜底强杀端口 $port 的进程树 (PID $pid)"
    taskkill.exe /PID "$pid" /T /F >/dev/null 2>&1 || true
  done
done

sleep 1
if netstat -ano 2>/dev/null | grep -E ':(8000|5183) ' | grep -q LISTENING; then
  warn "仍有进程监听 8000/5183, 请手动检查。"
else
  log "端口已释放, PanWatch 已停止。"
fi
