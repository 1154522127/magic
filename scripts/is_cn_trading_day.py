#!/usr/bin/env python3
"""判断今天（北京时间）是否为 A 股交易日。

依据上证指数日 K 是否含当日：节假日/周末无 K 线则视为休市。

用法：
    python3 scripts/is_cn_trading_day.py          # 交易日 exit 0，否则 1
    python3 scripts/is_cn_trading_day.py --print  # 额外打印日期与结果
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def beijing_today() -> str:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    return datetime.now().date().isoformat()


def latest_sse_trade_dates(limit: int = 10) -> list[str]:
    """上证指数 000001 最近若干日 K 线日期（YYYY-MM-DD）。"""
    path = (
        "/api/qt/stock/kline/get"
        "?secid=1.000001&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        "&klt=101&fqt=1&end=20500101"
        f"&lmt={limit}"
    )
    hosts = ("push2his.eastmoney.com", "push2delay.eastmoney.com")
    last_err: Exception | None = None
    for host in hosts:
        url = f"https://{host}{path}"
        req = Request(url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
        for _ in range(2):
            try:
                data = json.loads(urlopen(req, timeout=20).read().decode("utf-8", "replace"))
                klines = (data.get("data") or {}).get("klines") or []
                out = []
                for row in klines:
                    day = str(row).split(",", 1)[0].strip()
                    if day:
                        out.append(day)
                if out:
                    return out
            except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
                last_err = e
    raise RuntimeError(f"kline fetch failed: {last_err}")


def is_cn_trading_day(day: str | None = None) -> bool:
    day = day or beijing_today()
    dates = latest_sse_trade_dates()
    return day in dates


def main() -> int:
    verbose = "--print" in sys.argv or "-p" in sys.argv
    today = beijing_today()
    try:
        ok = is_cn_trading_day(today)
    except RuntimeError as e:
        print(f"✗ 无法判断交易日: {e}", file=sys.stderr)
        return 2
    if verbose:
        print(f"{today}\t{'交易日' if ok else '休市'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
