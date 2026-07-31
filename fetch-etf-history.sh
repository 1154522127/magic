#!/usr/bin/env bash
# 双击 fetch-etf-history.command：只采集最新估值写入本地 json，不提交 git
# 会打印上一份 vs 本次，方便看有没有变动。
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

HISTORY="data/etf_515450_history.json"
CODE="515450"
SNAP="$(mktemp -t etf515450.XXXXXX)"

cleanup() { rm -f "$SNAP"; }
trap cleanup EXIT

if [ -f "$HISTORY" ]; then
  cp "$HISTORY" "$SNAP"
else
  echo "{}" >"$SNAP"
fi

echo "→ 采集最新持仓加权估值（仅本地，不 git）…"
python3 "$ROOT/scripts/collect_etf_valuation.py" "$CODE"

if [ ! -f "$HISTORY" ]; then
  echo "✗ 未找到 $HISTORY"
  echo ""
  read -r -p "按回车关闭…" _
  exit 1
fi

python3 - "$SNAP" "$HISTORY" <<'PY'
import json, sys
from pathlib import Path

old_path, new_path = Path(sys.argv[1]), Path(sys.argv[2])

def load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def last_point(d):
    pts = d.get("points") or []
    return pts[-1] if pts else None

old, new = load(old_path), load(new_path)
op, np_ = last_point(old), last_point(new)
keys = ("date", "pe", "pb", "yield_pct", "coverage_pct", "n")

def fmt(p):
    if not p:
        return "(无)"
    return "  ".join(f"{k}={p.get(k)}" for k in keys)

print()
print(f"文件: {new_path}")
print(f"样本数: {len(old.get('points') or [])} → {len(new.get('points') or [])}")
print(f"updated_at: {old.get('updated_at') or '(无)'} → {new.get('updated_at') or '(无)'}")
print()
print("上一份末条:", fmt(op))
print("本次末条:  ", fmt(np_))
print()

if not op and np_:
    print("✓ 首次写入今日数据")
elif op and np_ and op.get("date") == np_.get("date"):
    changed = [k for k in keys if op.get(k) != np_.get(k)]
    if not changed and old.get("updated_at") != new.get("updated_at"):
        # 同日重采但数值完全一样
        print("✓ 今日点已存在，数值无变动（仅刷新了 updated_at）")
    elif not changed:
        print("✓ 与上一份完全相同")
    else:
        print("⚠ 同日数据有变动：")
        for k in changed:
            print(f"  {k}: {op.get(k)} → {np_.get(k)}")
elif op and np_ and op.get("date") != np_.get("date"):
    print(f"✓ 新日期点：{op.get('date')} → {np_.get('date')}")
else:
    print("? 未能比较")
PY

echo ""
echo "（未提交 git；手机端要用请双击 push-etf-history）"
echo ""
read -r -p "按回车关闭…" _
