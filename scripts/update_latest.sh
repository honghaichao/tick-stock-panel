#!/usr/bin/env bash
# tickflow-stock-panel — 增量"今日 + 缺日"刷新
#
# 端到端用: ./dev.sh 起来后, 这条脚本拉一次最新数据 (含当日盘后)。
# 用法:
#   ./scripts/update_latest.sh [BACKEND_URL]   # 默认 http://127.0.0.1:3018
set -euo pipefail

BACKEND="${1:-http://127.0.0.1:3018}"

info() { echo "[latest] $*"; }
err()  { echo "[latest][err] $*" >&2; }

# 后端在不在
if ! curl -fsS --max-time 3 "${BACKEND}/api/capabilities" >/dev/null 2>&1; then
  err "backend 不在 (${BACKEND}),  请先 ./dev.sh 或 docker compose up"
  exit 1
fi

info "triggering /api/pipeline/run"
jid=$(curl -fsS -X POST "${BACKEND}/api/pipeline/run" | jq -r '.job_id // empty')
if [ -z "${jid}" ]; then
  err "无法拿到 job_id, 直接看后端 log"
  exit 1
fi
info "job=${jid}, 轮询…"

deadline=$(( $(date +%s) + 3600 ))
while [ "$(date +%s)" -lt "${deadline}" ]; do
  resp=$(curl -fsS --max-time 5 "${BACKEND}/api/pipeline/jobs/${jid}" || echo "{}")
  st=$(echo "${resp}" | jq -r '.status // "?"')
  pct=$(echo "${resp}" | jq -r '.progress.pct // 0')
  msg=$(echo "${resp}" | jq -r '.progress.msg // ""')
  echo "  status=${st} pct=${pct} msg=${msg}"
  case "${st}" in
    success) info "完成"; break ;;
    failed)  err "失败:"; jq . <<<"${resp}"; exit 1 ;;
  esac
  sleep 30
done

# 验证
info "=== range check ==="
ls data/kline_daily/date=* 2>/dev/null | awk -F= '{print $2}' | sort | sed -n '1p;$p' | sed 's/^/  daily: /'
ls data/kline_daily_enriched/date=* 2>/dev/null | awk -F= '{print $2}' | sort | sed -n '1p;$p' | sed 's/^/  enriched: /'
info "OK"
