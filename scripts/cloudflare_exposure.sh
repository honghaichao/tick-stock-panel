#!/bin/bash
# 启动 tickflow-stock-panel + trycloudflare 临时公网隧道。
#
# 拓扑:
#   backend (3018)  <-- 本机 vite proxy  <-- frontend (3011)
#   frontend (3011) <-- cloudflared <-- 公网 trycloudflare URL
#
# 用法:
#   ./scripts/cloudflare_exposure.sh
#
# 行为:
#   - 终端 1: 跑这个脚本(前台), Ctrl-C 优雅关所有
#   - 看到 "https://xxx.trycloudflare.com" 后,在浏览器打开
#   - 用你设置的 AUTH_PASSWORD 登录
#
# 日志:
#   /tmp/ts-fill/backend.log, /tmp/ts-fill/cloudflared.log
#   dev.sh 自己有彩色前缀输出

set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p /tmp/ts-fill

# 清理旧实例
echo "[cleanup] 杀掉旧 dev.sh / uvicorn / frontend dev / cloudflared..."
pkill -f "uvicorn.*3018" 2>/dev/null || true
pkill -f "vite.*3011" 2>/dev/null || true
pkill -f "cloudflared.*--url" 2>/dev/null || true
sleep 1

# 启动 dev.sh (后台, 它同时启 backend 3018 和 frontend 3011)
echo "[dev] 启动 ./dev.sh (后台, 后端 3018 + 前端 3011) ..."
./dev.sh > /tmp/ts-fill/dev.log 2>&1 &
DEV_PID=$!
echo "[dev] PID=$DEV_PID"

# 等 backend ready
echo -n "[dev] 等 backend ready "
for i in $(seq 1 30); do
  if curl -sf -o /dev/null http://localhost:3018/api/settings/capability-matrix 2>/dev/null; then
    echo " (${i}s)"; break
  fi
  echo -n "."
  sleep 1
done

# 等 frontend ready
echo -n "[dev] 等 frontend ready "
for i in $(seq 1 20); do
  if curl -sf -o /dev/null http://localhost:3011/ 2>/dev/null; then
    echo " (${i}s)"; break
  fi
  echo -n "."
  sleep 1
done

cleanup() {
  echo
  echo "[shutdown] cleaning up..."
  kill -TERM $DEV_PID 2>/dev/null
  pkill -f "uvicorn.*3018" 2>/dev/null
  pkill -f "vite.*3011" 2>/dev/null
  pkill -f "cloudflared.*--url" 2>/dev/null
  sleep 1
  echo "[shutdown] done"
}
trap cleanup EXIT INT TERM

# cloudflared 把 3011 暴露公网
echo "[tunnel] 启动 cloudflared tunnel --url http://localhost:3011 ..."
echo "[tunnel] trycloudflare URL 会出现在下面 (等 ~5-10s)"
echo
exec cloudflared tunnel --url http://localhost:3011 2>&1 | tee /tmp/ts-fill/cloudflared.log
