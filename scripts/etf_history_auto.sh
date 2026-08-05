#!/usr/bin/env bash
# magic 运行期间：交易日 17:08、17:28 各尝试一次（本机兜底）。
# 主路径为 Cloudflare Worker 定时采集并推送；当日已有点则跳过。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CODE="515450"
MARKER="$ROOT/.etf_${CODE}_auto_date"
HISTORY="$ROOT/data/etf_${CODE}_history.json"
LOG="$ROOT/.etf_auto.log"
COLLECT="$ROOT/scripts/collect_etf_valuation.py"
INTERVAL_SEC=30  # 半分钟检查一次，避免错过目标分钟

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
  if has_today_point; then
    echo "$today" >"$MARKER"
    return 0
  fi
  [ -f "$MARKER" ] && [ "$(cat "$MARKER" 2>/dev/null)" = "$today" ]
}

should_collect_now() {
  # 周一–周五，17:08 或 17:28 那一分钟内
  local dow hm
  dow="$(date +%u)"
  hm="$(date +%H%M)"
  [ "$dow" -le 5 ] && { [ "$hm" = "1708" ] || [ "$hm" = "1728" ]; }
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
  log "→ 本机兜底采集 $CODE（17:08/17:28）…"
  if python3 "$COLLECT" "$CODE" >>"$LOG" 2>&1; then
    echo "$today" >"$MARKER"
    log "✓ 已写入 data/etf_${CODE}_history.json（主路径仍为 Cloudflare）"
  else
    log "✗ 采集失败，详见 $LOG"
  fi
}

log "本机兜底已启动（交易日 17:08、17:28；主路径 Cloudflare Worker）"
while true; do
  if should_collect_now && ! already_done_today; then
    run_collect || true
  fi
  sleep "$INTERVAL_SEC"
done
