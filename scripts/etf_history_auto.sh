#!/usr/bin/env bash
# magic 运行期间：每个交易日 17:30–18:00 内，每 5 分钟检查一次；
# 当日 data/etf_*_history.json 已有今天的点则跳过，否则采集一次。
# 由 magic.sh 后台拉起；勿单独依赖此脚本做推送。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CODE="515450"
MARKER="$ROOT/.etf_${CODE}_auto_date"
HISTORY="$ROOT/data/etf_${CODE}_history.json"
LOG="$ROOT/.etf_auto.log"
COLLECT="$ROOT/scripts/collect_etf_valuation.py"
INTERVAL_SEC=300  # 5 分钟（窗口仅 30 分钟，缩短间隔以便重试）

log() {
  echo "[$(date '+%F %T')] $*" | tee -a "$LOG"
}

has_today_point() {
  local today
  today="$(date +%F)"
  [ -f "$HISTORY" ] || return 1
  python3 - "$HISTORY" "$today" <<'PY'
import json, sys
path, today = sys.argv[1], sys.argv[2]
try:
    pts = json.load(open(path, encoding="utf-8")).get("points") or []
except Exception:
    sys.exit(1)
sys.exit(0 if any(p.get("date") == today for p in pts) else 1)
PY
}

already_done_today() {
  local today
  today="$(date +%F)"
  # 有了就不采：历史里已有今日点，或当日 marker 已写
  if has_today_point; then
    echo "$today" >"$MARKER"
    return 0
  fi
  [ -f "$MARKER" ] && [ "$(cat "$MARKER" 2>/dev/null)" = "$today" ]
}

should_collect_now() {
  # 周一–周五，17:30（含）～18:00（不含）
  local dow hm
  dow="$(date +%u)"
  hm="$(date +%H%M)"
  [ "$dow" -le 5 ] && [ "$hm" -ge 1730 ] && [ "$hm" -lt 1800 ]
}

is_trading_day() {
  python3 "$ROOT/scripts/is_cn_trading_day.py" >/dev/null 2>&1
}

run_collect() {
  local today
  today="$(date +%F)"
  if ! is_trading_day; then
    log "休市，跳过采集"
    echo "$today" >"$MARKER"
    return 0
  fi
  log "→ 17:30–18:00 自动采集 $CODE …"
  if python3 "$COLLECT" "$CODE" >>"$LOG" 2>&1; then
    echo "$today" >"$MARKER"
    log "✓ 已写入 data/etf_${CODE}_history.json（手机要用请再双击 push-etf-history）"
  else
    log "✗ 采集失败，详见 $LOG"
  fi
}

log "自动采集守护已启动（交易日 17:30–18:00，每 ${INTERVAL_SEC}s 检查；当日已有数据则跳过）"
while true; do
  if should_collect_now && ! already_done_today; then
    run_collect || true
  fi
  sleep "$INTERVAL_SEC"
done
