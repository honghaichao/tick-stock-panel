#!/usr/bin/env bash
# tickflow-stock-panel — 一次性 sync 2020 → 现在
#
# 网络可用的环境里执行:
#   1. 后端起来 (本脚本会检查 /api/health)
#   2. 多年分片触发 /api/kline/extend_history (2020 ~ 2026, 5 次年 + 1 次 day)
#   3. 触发 /api/pipeline/run 补当天 + enriched + 财务
#   4. 验证 daily/enriched/adj_factor/financials 范围
#
# 用法:
#   ./scripts/sync_2020_to_now.sh [BACKEND_URL]   # 默认 http://127.0.0.1:3018
set -euo pipefail

BACKEND="${1:-http://127.0.0.1:3018}"
LOG_DIR="data/.extend_2020_$(date +%Y%m%d_%H%M%S)"
mkdir -p "${LOG_DIR}"

info() { echo "[sync] $*"; }
err()  { echo "[sync][err] $*" >&2; }

# ── 0. 健康检查 ─────────────────────────────────────────
info "checking backend at ${BACKEND}"
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 "${BACKEND}/api/capabilities" >"${LOG_DIR}/cap.json" 2>/dev/null; then
    info "backend up (capabilities saved)"
    break
  fi
  if [ "${i}" -eq 30 ]; then
    err "backend not reachable after 30s. 用 dev.sh 或 docker compose up 起后端再跑。"
    exit 1
  fi
  sleep 1
done

# ── 1. 历史回填 (extend_history) ─────────────────────────
# 注意:
#   - Cap.KLINE_DAILY_BATCH 必须可用 (Free 档靠 tushare policy 注入)。
#   - 单次 value=1 year 约 80 min (Tushare free 200/min + 退避)。
#   - 服务端会在 new_start >= earliest 时返回 {"error": "扩展范围无效"} 直接退出,
#     所以重跑安全 (会自动短路)。
poll_job() {
  local jid="$1" deadline=$(( $(date +%s) + ${2:-600} ))
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    local resp st pct msg
    resp=$(curl -fsS --max-time 5 "${BACKEND}/api/pipeline/jobs/${jid}" || echo "{}")
    st=$(echo "${resp}" | jq -r '.status // "?"')
    pct=$(echo "${resp}" | jq -r '.progress.pct // 0')
    msg=$(echo "${resp}" | jq -r '.progress.msg // ""')
    info "  job=${jid} status=${st} pct=${pct} msg=${msg}"
    case "${st}" in
      success)
        jq -r '.result // {}' <<<"${resp}" | tee "${LOG_DIR}/${jid}.result.json"
        return 0
        ;;
      failed)
        err "job ${jid} failed: $(jq -r '.error // .' <<<"${resp}")"
        return 1
        ;;
    esac
    sleep 30
  done
  err "job ${jid} 超时 ${2:-600}s, 还没完成"
  return 2
}

trigger_extend() {
  local body="$1" tag="$2"
  local jid
  jid=$(curl -fsS -X POST "${BACKEND}/api/kline/extend_history" \
        -H 'content-type: application/json' \
        -d "${body}" | jq -r '.job_id // empty')
  if [ -z "${jid}" ]; then
    err "extend ${tag} 触发失败, body=${body}"
    curl -sS -X POST "${BACKEND}/api/kline/extend_history" -H 'content-type: application/json' -d "${body}" | head -2
    return 1
  fi
  info "extend ${tag} 触发, job=${jid}"
  poll_job "${jid}" 7200 || return 1  # 2h 上限
}

# 6 次 year + 1 次 day 兜底
for n in 1 2 3 4 5 6; do
  trigger_extend '{"value":1,"unit":"year"}' "year#${n}" || true
  # 检查是否已经触及 2020-01-01 (服务端短路会失败但不算错)
  FIRST=$(ls data/kline_daily/date=* 2>/dev/null | awk -F= '{print $2}' | sort | head -1)
  if [ -n "${FIRST}" ] && [ "${FIRST}" \< "2020-02-01" ]; then
    info "已抵达 ${FIRST}, 不再 continue 扩展"
    break
  fi
done

# 余量兜底
trigger_extend '{"value":200,"unit":"day"}' "leftover_day" || true

# ── 2. 当日完整管道 ───────────────────────────────────────
info "triggering full /api/pipeline/run"
jid=$(curl -fsS -X POST "${BACKEND}/api/pipeline/run" | jq -r '.job_id // empty')
if [ -n "${jid}" ]; then
  poll_job "${jid}" 3600 || info "pipeline 任务未结束 (同步耗时大, 让它后台跑)"
fi

# ── 3. 验证 ──────────────────────────────────────────────
info "=== verification ==="
echo "kline_daily:"
ls data/kline_daily/date=* 2>/dev/null | awk -F= '{print $2}' | sort | sed -n '1p;$p' | sed 's/^/  /'
echo "kline_daily_enriched:"
ls data/kline_daily_enriched/date=* 2>/dev/null | awk -F= '{print $2}' | sort | sed -n '1p;$p' | sed 's/^/  /'
echo "adj_factor parquets:"
ls data/adj_factor/*.parquet 2>/dev/null | wc -l | sed 's/^/  count: /'
echo "financials dir:"
ls -la data/financials/ 2>/dev/null | sed 's/^/  /'
info "后端 log 中 ERROR/Traceback/SchemaError 数 (期望 0):"
grep -cE "ERROR|Traceback|SchemaError" data/backend.log | sed 's/^/  /'

info "done. logs in ${LOG_DIR}"
