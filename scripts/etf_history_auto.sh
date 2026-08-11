#!/usr/bin/env bash
# magic 运行期间：交易日 17:08、17:28 各尝试一次（本机兜底）。
# 写入 proxy/data/（不进 git），与正式 data/etf_*_history.json 隔离。
# 正式历史仅由 Cloudflare Worker（22:08 / 22:38 / 23:08 / 23:38）采集推送。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CODE="515450"
# 与仓库正式历史隔离，避免和 Cloudflare 推送互相覆盖
LOCAL_DATA="$ROOT/proxy/data"
MARKER="$ROOT/.etf_${CODE}_auto_date"
HISTORY="$LOCAL_DATA/etf_${CODE}_history.json"
LOG="$ROOT/.etf_auto.log"
COLLECT="$ROOT/scripts/collect_etf_valuation.py"
INTERVAL_SEC=30

mkdir -p "$LOCAL_DATA"

log() {
  # 只打 stdout：magic.sh 已把本脚本 stdout 重定向到 .etf_auto.log，避免 tee 再写一份导致双行
  echo "[$(date '+%F %T')] $*"
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
  local dow hm
  dow="$(date +%u)"
  hm="$(date +%H%M)"
  [ "$dow" -le 5 ] && { [ "$hm" = "1708" ] || [ "$hm" = "1728" ]; }
}

# exit 0=交易日, 1=休市, 2=无法判断（东财超时等）
trading_day_status() {
  python3 "$ROOT/scripts/is_cn_trading_day.py" >/dev/null 2>&1
  return $?
}

run_collect() {
  local today status dow
  today="$(date +%F)"
  trading_day_status
  status=$?
  if [ "$status" -eq 1 ]; then
    log "休市，跳过采集"
    echo "$today" >"$MARKER"
    return 0
  fi
  if [ "$status" -eq 2 ]; then
    dow="$(date +%u)"
    if [ "$dow" -ge 1 ] && [ "$dow" -le 5 ]; then
      log "⚠ 交易日判断失败，工作日按开市继续采集"
    else
      log "⚠ 交易日判断失败且非工作日，跳过"
      return 0
    fi
  fi
  log "→ 本机兜底采集 $CODE → proxy/data/ …"
  if ETF_HISTORY_DIR="$LOCAL_DATA" python3 "$COLLECT" "$CODE" >>"$LOG" 2>&1; then
    echo "$today" >"$MARKER"
    log "✓ 已写入 $HISTORY（正式历史仍由 Cloudflare 负责）"
  else
    log "✗ 采集失败，详见 $LOG"
  fi
}

log "本机兜底已启动（17:08/17:28 → proxy/data/；正式历史 Cloudflare 22:08–23:38×4）"
while true; do
  if should_collect_now && ! already_done_today; then
    run_collect || true
  fi
  sleep "$INTERVAL_SEC"
done
