#!/usr/bin/env python3
"""本机估值代理 — 蛋卷指数估值 + ETF 持仓加权基本面/自建分位。

用法（在项目根目录）：
    python3 proxy/valuation.py

接口：
  /valuation              → 蛋卷指数估值
  /etf_yield?code=515450  → 兼容旧接口（股息）
  /etf_fundamentals?code=515450 → 持仓加权 PE/PB/股息 + 自建历史分位

正式历史 data/etf_*_history.json 仅由 Cloudflare 写入；
本机 magic 兜底写入 proxy/data/（gitignore，不进仓库）。
分位样本 < MIN_HISTORY_DAYS 时 percentile_ready=false，前端继续用蛋卷代理分位。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

PORT = 8787
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
UPSTREAMS = [
    "https://danjuanfunds.com/djapi/index_eva/dj",
    "https://danjuanapp.com/djapi/index_eva/dj",
]
UPSTREAM_HEADERS = {
    "Referer": "https://danjuanfunds.com/djmodule/value-center",
    "User-Agent": UA,
}
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
# 正式历史在仓库 data/；本机兜底可通过 ETF_HISTORY_DIR 指到 proxy/data/
DATA_DIR = Path(os.environ["ETF_HISTORY_DIR"]) if os.environ.get("ETF_HISTORY_DIR") else (REPO_ROOT / "data")
MIN_HISTORY_DAYS = 60  # 满 60 个交易日样本后启用自建分位
MAX_HISTORY_DAYS = 30 * 252  # 约 30 年交易日；单点很小，全量约 1–2MB


def http_get(url: str, referer: str, timeout: int = 15) -> bytes:
    req = Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def stock_secid(code: str) -> str:
    return ("1." if code.startswith("6") else "0.") + code


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


# 季报通常只披前十；年报/半年报才有全量。优先选权重合计≥此阈值的一期。
FULL_HOLDINGS_WEIGHT_MIN = 90.0
FULL_HOLDINGS_COUNT_MIN = 40


def _parse_f10_content(content: str) -> list[dict]:
    """按表头解析各报告期持仓表，返回 [{date, rows, weight_sum}, ...]。"""
    parts = re.findall(
        r"<h4 class='t'>(.*?)</h4>.*?<table[^>]*>(.*?)</table>",
        content,
        re.S,
    )
    tables: list[dict] = []
    for h4, table in parts:
        title = _strip_tags(h4)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        date = dm.group(1) if dm else ""
        ths = [_strip_tags(t) for t in re.findall(r"<th[^>]*>(.*?)</th>", table, re.S)]
        if "股票代码" not in ths or "占净值比例" not in ths:
            continue
        ccol, ncol, wcol = (
            ths.index("股票代码"),
            ths.index("股票名称"),
            ths.index("占净值比例"),
        )
        rows: list[dict] = []
        seen: set[str] = set()
        for tr in re.findall(r"<tr>(.*?)</tr>", table, re.S):
            tds = [_strip_tags(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(tds) <= max(ccol, ncol, wcol):
                continue
            scode = tds[ccol]
            if not re.fullmatch(r"\d{6}", scode) or scode in seen:
                continue
            wraw = tds[wcol]
            if not re.fullmatch(r"[0-9.]+%", wraw):
                continue
            weight = float(wraw[:-1])
            if weight <= 0:
                continue
            seen.add(scode)
            rows.append({"code": scode, "name": tds[ncol], "weight": weight})
        if rows:
            tables.append(
                {
                    "date": date,
                    "rows": rows,
                    "weight_sum": sum(r["weight"] for r in rows),
                }
            )
    return tables


def _fetch_f10_page(code: str, year: str = "", month: str = "") -> tuple[list[dict], list[int]]:
    url = (
        "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        f"?type=jjcc&code={code}&topline=100&year={year}&month={month}"
    )
    text = http_get(url, "https://fundf10.eastmoney.com/").decode("utf-8", "replace")
    m = re.search(r'content:"((?:[^"\\]|\\.)*)"', text)
    if not m:
        return [], []
    content = m.group(1).replace('\\"', '"').replace("\\/", "/")
    ym = re.search(r"arryear:(\[[^\]]+\])", text)
    years = json.loads(ym.group(1)) if ym else []
    return _parse_f10_content(content), years


def parse_f10_holdings(code: str) -> list[dict]:
    """兼容旧接口：返回最优一期持仓行（不含报告期元数据）。"""
    holdings, _ = resolve_holdings(code)
    return holdings


def parse_mobile_holdings(code: str) -> list[dict]:
    url = (
        "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition"
        f"?FCODE={code}&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0"
    )
    data = json.loads(
        http_get(url, "https://fund.eastmoney.com/").decode("utf-8", "replace")
    )
    stocks = (data.get("Datas") or {}).get("fundStocks") or []
    out = []
    for s in stocks:
        scode = str(s.get("GPDM") or "")
        try:
            weight = float(s.get("JZBL") or 0)
        except (TypeError, ValueError):
            weight = 0
        if scode.isdigit() and weight > 0:
            out.append(
                {"code": scode, "name": s.get("GPJC") or scode, "weight": weight}
            )
    return out


def merge_holdings(*groups: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for group in groups:
        for row in group:
            prev = best.get(row["code"])
            if not prev or row["weight"] > prev["weight"]:
                best[row["code"]] = row
    return sorted(best.values(), key=lambda x: -x["weight"])


def _is_full_holdings(weight_sum: float, n: int) -> bool:
    return weight_sum >= FULL_HOLDINGS_WEIGHT_MIN and n >= FULL_HOLDINGS_COUNT_MIN


def resolve_holdings(code: str) -> tuple[list[dict], str | None]:
    """取最近一期「全量披露」持仓；若当年仅有季报前十，回退到上年年报/半年报。

    返回 (holdings, holdings_asof)。权重来自定期报告，估值用当日行情。
    """
    _, years = _fetch_f10_page(code, "", "")
    if not years:
        years = [date.today().year, date.today().year - 1]

    candidates: list[dict] = []
    seen_dates: set[str] = set()
    for year in years[:4]:
        tables, _ = _fetch_f10_page(code, str(year), "")
        for t in tables:
            d = t.get("date") or ""
            if d in seen_dates:
                continue
            seen_dates.add(d)
            candidates.append(t)

    if not candidates:
        mobile = parse_mobile_holdings(code)
        return mobile, None

    full = [t for t in candidates if _is_full_holdings(t["weight_sum"], len(t["rows"]))]
    pool = full or candidates
    best = max(
        pool,
        key=lambda t: (
            t.get("date") or "",
            t["weight_sum"],
            len(t["rows"]),
        ),
    )
    rows = sorted(best["rows"], key=lambda x: -x["weight"])
    # 全量不足时再用手机端十大补洞（不应覆盖已有权重）
    if not _is_full_holdings(best["weight_sum"], len(rows)):
        have = {r["code"] for r in rows}
        for r in parse_mobile_holdings(code):
            if r["code"] not in have:
                rows.append(r)
        rows = sorted(rows, key=lambda x: -x["weight"])
    return rows, best.get("date") or None


def quote_fundamentals(codes: list[str]) -> dict[str, dict]:
    """东财：f9≈PE，f23≈PB，f133≈股息率(%)。超长列表分批。"""
    if not codes:
        return {}
    out: dict[str, dict] = {}
    chunk_size = 80
    for i in range(0, len(codes), chunk_size):
        chunk = codes[i : i + chunk_size]
        ids = ",".join(stock_secid(c) for c in chunk)
        fields = "f12,f14,f9,f23,f133"
        last_err = None
        got = None
        for host in ("push2delay.eastmoney.com", "push2.eastmoney.com"):
            url = f"https://{host}/api/qt/ulist.np/get?fltt=2&secids={ids}&fields={fields}"
            try:
                data = json.loads(
                    http_get(url, "https://quote.eastmoney.com/").decode(
                        "utf-8", "replace"
                    )
                )
                rows = ((data.get("data") or {}).get("diff")) or []
                got = {}
                for row in rows:
                    c = str(row.get("f12") or "")
                    if not c:
                        continue
                    item: dict = {}
                    pe = row.get("f9")
                    pb = row.get("f23")
                    yld = row.get("f133")
                    if isinstance(pe, (int, float)) and pe > 0:
                        item["pe"] = float(pe)
                    if isinstance(pb, (int, float)) and pb > 0:
                        item["pb"] = float(pb)
                    if isinstance(yld, (int, float)) and yld > 0:
                        item["yield_pct"] = float(yld)
                    if item:
                        got[c] = item
                break
            except (URLError, HTTPError, TimeoutError, json.JSONDecodeError) as e:
                last_err = e
                continue
        if got is None:
            raise RuntimeError(f"quote failed: {last_err}")
        out.update(got)
    return out


def weighted_arithmetic(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    w_sum = sum(w for w, _ in pairs)
    if w_sum <= 0:
        return None
    return sum(w * v for w, v in pairs) / w_sum


def weighted_harmonic(pairs: list[tuple[float, float]]) -> float | None:
    """组合 PE 更常用加权调和平均。"""
    if not pairs:
        return None
    w_sum = sum(w for w, _ in pairs)
    den = sum(w / v for w, v in pairs if v > 0)
    if w_sum <= 0 or den <= 0:
        return None
    return w_sum / den


def history_path(code: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"etf_{code}_history.json"


def _history_note() -> str:
    # 与正式 data/ 字段一致；本机兜底目录仅 note 文案区分
    if DATA_DIR.resolve() == (REPO_ROOT / "data").resolve():
        return (
            "515450持仓加权近似·标普大盘红利低波50；非官方指数点位。"
            "权重取最近一期年报/半年报全量持仓，估值用当日行情。"
            "正式历史由 Cloudflare Worker 交易日 22:08/22:38/23:08/23:38 采集推送。"
        )
    return (
        "515450持仓加权近似·标普大盘红利低波50；非官方指数点位。"
        "权重取最近一期年报/半年报全量持仓，估值用当日行情。"
        "本机 magic 兜底（17:08/17:28），与正式 data/ 隔离，不进 git。"
    )


def load_history(code: str) -> dict:
    path = history_path(code)
    if not path.exists():
        return {"code": code, "note": _history_note(), "points": []}
    try:
        hist = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"code": code, "note": _history_note(), "points": []}
    hist["code"] = code
    hist["note"] = hist.get("note") or _history_note()
    hist.pop("updated_at", None)  # 旧格式兼容：改用各点 collected_at
    points = hist.get("points") or []
    if not isinstance(points, list):
        points = []
    hist["points"] = points
    return hist


def save_history(code: str, hist: dict) -> None:
    path = history_path(code)
    hist = {
        "code": code,
        "note": hist.get("note") or _history_note(),
        "points": hist.get("points") or [],
    }
    path.write_text(
        json.dumps(hist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def append_history_point(code: str, point: dict) -> dict:
    hist = load_history(code)
    points = list(hist.get("points") or [])
    d = point["date"]
    now = datetime.now().isoformat(timespec="seconds")
    if not point.get("collected_at"):
        point = {**point, "collected_at": now}
    # 与正式历史同一字段：date/pe/pb/yeild/yield_pct/coverage_pct/n/holdings_asof/collected_at
    point = {
        "date": point["date"],
        "pe": point.get("pe"),
        "pb": point.get("pb"),
        "yeild": point.get("yeild"),
        "yield_pct": point.get("yield_pct"),
        "coverage_pct": point.get("coverage_pct"),
        "n": point.get("n"),
        "holdings_asof": point.get("holdings_asof"),
        "collected_at": point["collected_at"],
    }
    points = [p for p in points if p.get("date") != d]
    points.append(point)
    # 全量持仓启用后，丢掉旧的「仅前十/半仓」样本，避免污染自建分位
    if (point.get("n") or 0) >= FULL_HOLDINGS_COUNT_MIN or (
        point.get("coverage_pct") or 0
    ) >= FULL_HOLDINGS_WEIGHT_MIN:
        points = [
            p
            for p in points
            if (p.get("n") or 0) >= FULL_HOLDINGS_COUNT_MIN
            or (p.get("coverage_pct") or 0) >= FULL_HOLDINGS_WEIGHT_MIN
        ]
    points.sort(key=lambda p: p.get("date") or "")
    if len(points) > MAX_HISTORY_DAYS:
        points = points[-MAX_HISTORY_DAYS:]
    hist["points"] = points
    hist["note"] = _history_note()
    save_history(code, hist)
    return hist


def percentile_rank(values: list[float], current: float) -> float | None:
    """与蛋卷口径一致：越高越贵。= 历史中 ≤ 当前值 的占比。"""
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals or not isinstance(current, (int, float)):
        return None
    return sum(1 for v in vals if v <= current) / len(vals)


def compute_etf_fundamentals(code: str, persist: bool = True) -> dict:
    holdings, holdings_asof = resolve_holdings(code)
    if not holdings:
        raise RuntimeError("no holdings")
    quotes = quote_fundamentals([h["code"] for h in holdings])

    pe_pairs: list[tuple[float, float]] = []
    pb_pairs: list[tuple[float, float]] = []
    y_pairs: list[tuple[float, float]] = []
    used = []
    for h in holdings:
        q = quotes.get(h["code"]) or {}
        row = {**h}
        if "pe" in q:
            pe_pairs.append((h["weight"], q["pe"]))
            row["pe"] = round(q["pe"], 4)
        if "pb" in q:
            pb_pairs.append((h["weight"], q["pb"]))
            row["pb"] = round(q["pb"], 4)
        if "yield_pct" in q:
            y_pairs.append((h["weight"], q["yield_pct"]))
            row["yield_pct"] = round(q["yield_pct"], 4)
        if len(row) > 3:
            used.append(row)

    pe = weighted_harmonic(pe_pairs)
    pb = weighted_arithmetic(pb_pairs)
    yield_pct = weighted_arithmetic(y_pairs)
    if pe is None and pb is None and yield_pct is None:
        raise RuntimeError("no fundamental quotes")

    coverage = 0.0
    if y_pairs:
        coverage = sum(w for w, _ in y_pairs)
    elif pe_pairs:
        coverage = sum(w for w, _ in pe_pairs)
    elif pb_pairs:
        coverage = sum(w for w, _ in pb_pairs)

    today = date.today().isoformat()
    collected_at = datetime.now().isoformat(timespec="seconds")
    point = {
        "date": today,
        "pe": round(pe, 4) if pe is not None else None,
        "pb": round(pb, 4) if pb is not None else None,
        "yeild": round(yield_pct / 100.0, 6) if yield_pct is not None else None,
        "yield_pct": round(yield_pct, 4) if yield_pct is not None else None,
        "coverage_pct": round(coverage, 2),
        "n": len(used),
        "holdings_asof": holdings_asof,
        "collected_at": collected_at,
    }

    hist = append_history_point(code, point) if persist else load_history(code)
    points = hist.get("points") or []
    pe_hist = [p["pe"] for p in points if p.get("pe") is not None]
    pb_hist = [p["pb"] for p in points if p.get("pb") is not None]
    # 用入库后的四舍五入值算分位，避免 float 微差导致单日样本落成 0%
    pe_p = (
        percentile_rank(pe_hist, point["pe"])
        if point["pe"] is not None
        else None
    )
    pb_p = (
        percentile_rank(pb_hist, point["pb"])
        if point["pb"] is not None
        else None
    )
    n_hist = len(points)
    ready = n_hist >= MIN_HISTORY_DAYS
    weight_sum = round(sum(h["weight"] for h in holdings), 2)

    return {
        "code": code,
        "date": today,
        "pe": point["pe"],
        "pb": point["pb"],
        "yeild": point["yeild"],
        "yield_pct": point["yield_pct"],
        "coverage_pct": point["coverage_pct"],
        "n": point["n"],
        "holdings_asof": holdings_asof,
        "holdings_weight_sum": weight_sum,
        "pe_percentile": round(pe_p, 6) if pe_p is not None else None,
        "pb_percentile": round(pb_p, 6) if pb_p is not None else None,
        "history_n": n_hist,
        "history_min": MIN_HISTORY_DAYS,
        "percentile_ready": ready,
        "source": "eastmoney-full-holdings+self-history",
        "note": (
            "持仓加权近似（最近全量定期报告权重×当日行情），非标普官方指数点；"
            "分位样本不足时勿单独使用"
        ),
        "holdings": used[:20],
    }


def compute_etf_yield(code: str, persist: bool = False) -> dict:
    """兼容旧接口。默认不落盘（与页面刷新一致）。"""
    full = compute_etf_fundamentals(code, persist=persist)
    return {
        "code": full["code"],
        "yeild": full["yeild"],
        "yield_pct": full["yield_pct"],
        "coverage_pct": full["coverage_pct"],
        "n": full["n"],
        "source": full["source"],
        "holdings": full.get("holdings") or [],
        "pe": full.get("pe"),
        "pb": full.get("pb"),
        "pe_percentile": full.get("pe_percentile"),
        "pb_percentile": full.get("pb_percentile"),
        "history_n": full.get("history_n"),
        "history_min": full.get("history_min"),
        "percentile_ready": full.get("percentile_ready"),
    }


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict | list | bytes):
    body = (
        payload
        if isinstance(payload, (bytes, bytearray))
        else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    handler.send_response(status)
    for k, v in CORS.items():
        handler.send_header(k, v)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/valuation"):
            for url in UPSTREAMS:
                try:
                    body = http_get(url, UPSTREAM_HEADERS["Referer"])
                    send_json(self, 200, body)
                    return
                except (URLError, HTTPError) as e:
                    print(f"  upstream fail: {url} ({e})")
            send_json(self, 502, {"error": "upstream unavailable"})
            return

        if path in ("/etf_yield", "/etf_fundamentals"):
            code = (qs.get("code") or ["515450"])[0].strip()
            if not re.fullmatch(r"\d{6}", code):
                send_json(self, 400, {"error": "invalid code"})
                return
            # HTTP 一律不落盘；正式历史仅 Cloudflare，本机兜底走独立脚本目录
            try:
                if path == "/etf_fundamentals":
                    send_json(
                        self, 200, compute_etf_fundamentals(code, persist=False)
                    )
                else:
                    send_json(self, 200, compute_etf_yield(code, persist=False))
            except Exception as e:
                print(f"  {path} fail: {code} ({e})")
                send_json(self, 502, {"error": str(e)})
            return

        if path == "/etf_history":
            code = (qs.get("code") or ["515450"])[0].strip()
            if not re.fullmatch(r"\d{6}", code):
                send_json(self, 400, {"error": "invalid code"})
                return
            hist = load_history(code)
            hist["history_n"] = len(hist.get("points") or [])
            hist["history_min"] = MIN_HISTORY_DAYS
            hist["percentile_ready"] = hist["history_n"] >= MIN_HISTORY_DAYS
            send_json(self, 200, hist)
            return

        self.send_error(404)

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    addr = ("0.0.0.0", PORT)
    print(f"估值代理     http://127.0.0.1:{PORT}/valuation")
    print(f"ETF基本面    http://127.0.0.1:{PORT}/etf_fundamentals?code=515450")
    print(f"ETF历史      http://127.0.0.1:{PORT}/etf_history?code=515450")
    print(f"历史文件   {DATA_DIR / 'etf_515450_history.json'}（提交 git 后手机可直接用）")
    print(f"自建分位门槛 {MIN_HISTORY_DAYS} 个交易日样本")
    print("保持运行，浏览器打开 magic 页面")
    HTTPServer(addr, Handler).serve_forever()


if __name__ == "__main__":
    main()
