#!/usr/bin/env bash
# magic 一键启动/停止
#   双击 magic.command  或  ./magic.sh
#   后台运行（手机访问时电脑别关）：./magic.sh bg
#   仅停止：./magic.sh stop
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PROXY_PORT=8787
WEB_PORT=8765
APP_URL="http://127.0.0.1:$WEB_PORT/index.html"
PID_FILE="$ROOT/.proxy.pid"
WEB_PID_FILE="$ROOT/.web.pid"
AUTO_PID_FILE="$ROOT/.etf_auto.pid"
LOG_FILE="$ROOT/.proxy.log"
WEB_LOG="$ROOT/.web.log"
AUTO_LOG="$ROOT/.etf_auto.log"

lan_ip() {
  ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true
}

stop_services() {
  [ -f "$AUTO_PID_FILE" ] && kill "$(cat "$AUTO_PID_FILE")" 2>/dev/null || true
  rm -f "$AUTO_PID_FILE"

  [ -f "$PID_FILE" ] && kill "$(cat "$PID_FILE")" 2>/dev/null || true
  rm -f "$PID_FILE"
  lsof -ti ":$PROXY_PORT" | xargs kill 2>/dev/null || true

  [ -f "$WEB_PID_FILE" ] && kill "$(cat "$WEB_PID_FILE")" 2>/dev/null || true
  rm -f "$WEB_PID_FILE"
  lsof -ti ":$WEB_PORT" | xargs kill 2>/dev/null || true

  echo "✓ 蛋卷代理、本地网页与自动采集已停止"
}

start_etf_auto() {
  if [ -f "$AUTO_PID_FILE" ] && kill -0 "$(cat "$AUTO_PID_FILE")" 2>/dev/null; then
    echo "✓ 515450 本地自动采集已在运行"
    return 0
  fi
  chmod +x "$ROOT/scripts/etf_history_auto.sh" 2>/dev/null || true
  nohup "$ROOT/scripts/etf_history_auto.sh" >>"$AUTO_LOG" 2>&1 &
  echo $! >"$AUTO_PID_FILE"
  echo "✓ 515450 自动采集：交易日 17:08 / 17:28 写入本地；Actions 20:08–23:08 兜底"
}

start_proxy() {
  if lsof -ti ":$PROXY_PORT" >/dev/null 2>&1; then
    echo "✓ 蛋卷代理已在运行 (:$PROXY_PORT)"
    return 0
  fi
  echo "→ 启动蛋卷代理..."
  nohup python3 "$ROOT/proxy/valuation.py" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  for _ in $(seq 1 20); do
    curl -sf "http://127.0.0.1:$PROXY_PORT/" >/dev/null 2>&1 && break
    sleep 0.25
  done
  curl -sf "http://127.0.0.1:$PROXY_PORT/" >/dev/null 2>&1 || {
    echo "✗ 代理启动失败，查看 $LOG_FILE"
    exit 1
  }
  echo "✓ 蛋卷代理 http://127.0.0.1:$PROXY_PORT"
}

start_web() {
  if lsof -ti ":$WEB_PORT" >/dev/null 2>&1; then
    echo "✓ 本地网页已在运行 (:$WEB_PORT)"
    return 0
  fi
  echo "→ 启动本地网页（局域网可访问）..."
  nohup python3 -m http.server "$WEB_PORT" --bind 0.0.0.0 >>"$WEB_LOG" 2>&1 &
  echo $! >"$WEB_PID_FILE"
  sleep 0.4
  echo "✓ 电脑访问 $APP_URL"
}

print_phone_hint() {
  local ip
  ip="$(lan_ip)"
  if [ -n "$ip" ]; then
    echo "📱 手机（同一 WiFi）: http://$ip:$WEB_PORT/index.html"
    echo "   可添加到主屏幕，当 App 用"
  else
    echo "📱 手机：系统设置 → 网络 查看本机 IP，浏览器打开 http://IP:$WEB_PORT/index.html"
  fi
}

cmd="${1:-start}"
case "$cmd" in
  stop)
    stop_services
    ;;
  bg)
    start_proxy
    start_web
    start_etf_auto
    print_phone_hint
    echo ""
    echo "✓ 已在后台运行；停止请执行: ./magic.sh stop"
    ;;
  start)
    start_proxy
    start_web
    start_etf_auto
    open "$APP_URL"
    print_phone_hint
    echo ""
    echo "✓ 电脑已打开（估值应显示 ·蛋卷，否则点刷新）"
    echo "⏱ 保持此窗口开着：交易日 17:08 / 17:28 自动写入本地；Actions 20:08 / 21:08 / 22:08 / 23:08 兜底"
    echo "⚠ 按回车会停止服务，手机也将无法访问"
    echo ""
    read -r -p "按回车停止代理并关闭此窗口…" _
    stop_services
    ;;
  *)
    echo "用法: $0 [start|bg|stop]  （默认 start）"
    exit 1
    ;;
esac
