#!/usr/bin/env python3
"""采集 515450 持仓加权 PE/PB/股息并落盘。

供本机兜底脚本调用（ETF_HISTORY_DIR=proxy/data）；
正式仓库历史由 Cloudflare Worker 写入。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from proxy.valuation import (  # noqa: E402
    MIN_HISTORY_DAYS,
    compute_etf_fundamentals,
    history_path,
)


def main() -> int:
    code = (sys.argv[1] if len(sys.argv) > 1 else "515450").strip()
    data = compute_etf_fundamentals(code, persist=True)
    brief = {
        k: data[k]
        for k in (
            "code",
            "date",
            "pe",
            "pb",
            "yield_pct",
            "coverage_pct",
            "history_n",
            "history_min",
            "percentile_ready",
            "pe_percentile",
            "pb_percentile",
        )
    }
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    path = history_path(code)
    print(f"已写入 {path}")
    n = data["history_n"]
    need = MIN_HISTORY_DAYS
    if data["percentile_ready"]:
        print(f"自建分位已启用（{n}≥{need}）")
    else:
        print(f"样本积累中：{n}/{need}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
