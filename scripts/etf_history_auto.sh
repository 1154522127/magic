#!/usr/bin/env bash
# magic 运行期间：每个交易日 16:00–17:00 内自动采集一次 515450（写入本地 json）
# 由 magic.sh 后台拉起；勿单独依赖此脚本做推送。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CODE="515450"
MARKER="$ROOT/.etf_${CODE}_auto_date"
LOG="$ROOT/.etf_auto.log"
COLLECT="$ROOT/scripts/collect_etf_valuation.py"
INTERVAL_SEC=900  # 15 分钟

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$LOG"
}

already_done_today() {
  local today
  today="$(date +%F)"
  [ -f "$MARKER" ] && [ "$(cat "$MARKER" 2>/dev/null)" = "$today" ]
}

should_collect_now() {
  # 周一–周五，16:00（含）～17:00（不含）
  local dow hm
  dow="$(date +%u)"
  hm="$(date +%H%M)"
  [ "$dow" -le 5 ] && [ "$hm" -ge 1600 ] && [ "$hm" -lt 1700 ]
}

run_collect() {
  local today
  today="$(date +%F)"
  log "→ 16:00–17:00 自动采集 $CODE …"
  if python3 "$COLLECT" "$CODE" >>"$LOG" 2>&1; then
    echo "$today" >"$MARKER"
    log "✓ 已写入 data/etf_${CODE}_history.json（手机要用请再双击 push-etf-history）"
  else
    log "✗ 采集失败，详见 $LOG"
  fi
}

log "自动采集守护已启动（交易日 16:00–17:00，每 ${INTERVAL_SEC}s 检查，一天最多一次）"
while true; do
  if should_collect_now && ! already_done_today; then
    run_collect || true
  fi
  sleep "$INTERVAL_SEC"
done
