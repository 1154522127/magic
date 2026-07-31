#!/usr/bin/env bash
# 采集 515450 持仓加权估值并推送到 git（供 GitHub Pages 手机端使用）
#   双击 push-etf-history.command  或  ./push-etf-history.sh
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

HISTORY="data/etf_515450_history.json"
CODE="515450"

echo "→ 采集今日持仓加权估值…"
python3 "$ROOT/scripts/collect_etf_valuation.py" "$CODE"

if [ ! -f "$HISTORY" ]; then
  echo "✗ 未找到 $HISTORY"
  exit 1
fi

git add -- "$HISTORY"
if git diff --cached --quiet -- "$HISTORY"; then
  echo "✓ $HISTORY 无新变更（今天可能已推送过）"
  echo ""
  read -r -p "按回车关闭…" _
  exit 0
fi

DATE="$(python3 -c "import json; print(json.load(open('$HISTORY'))['points'][-1]['date'])" 2>/dev/null || date +%F)"
N="$(python3 -c "import json; print(len(json.load(open('$HISTORY'))['points']))" 2>/dev/null || echo '?')"

echo "→ 提交 $HISTORY …"
git commit -m "$(cat <<EOF
chore: update 515450 valuation history ($DATE, n=$N)

EOF
)"

echo "→ 推送到 origin…"
git push -u origin HEAD

echo ""
echo "✓ 已推送 $HISTORY（样本 $N 条，日期 $DATE）"
echo "  手机打开 Git 页刷新即可读到最新历史"
echo ""
read -r -p "按回车关闭…" _
