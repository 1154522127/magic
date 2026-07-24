#!/usr/bin/env python3
"""Backtest magic valuation signals vs plain DCA using djeva + Eastmoney klines."""

from __future__ import annotations

import json
import math
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

US10Y = 4.3
CN10Y = 1.8
# 近年中枢重标定（与 index.html VAL.bondAttractiveY / bondRichY 一致）
CN_ATTRACTIVE = 2.4
CN_RICH = 1.65
FX_REF_LOW = 6.5
FX_REF_HIGH = 7.4
QDII_CODES = {"513100", "513500"}

VAL = dict(
    cheap=30,
    normal=50,
    mild=70,
    rich=85,
    zoneHigh=70,
    zoneLow=30,
    warnCap=0.5,
    upgradePullMax=10,
    # 激进分目标：纳指~40 · 标普~42 · 红利~47 · 国债~53
    # 标普暂停档 96→92；部署/warnCap/spPePauseGate 不变，激进分仍≈42
    deploy=dict(
        ndx=dict(cheap=34, rich=92, warnCap=0.55, warnScoreCap=55, pullMax=14),
        sp500=dict(cheap=22, rich=99, warnCap=0.38, warnScoreCap=55, pullMax=10),
        dividend=dict(cheap=22, rich=96, warnCap=0.65, peakCap=0.78, pullMax=8),
        bond=dict(cheap=18, rich=82, warnCap=0.7, warnInvCap=38, fullWhenOk=True),
    ),
    bands=dict(
        ndx=(28, 55, 78, 90),
        sp500=(30, 58, 72, 92),
        dividend=(22, 44, 66, 86),
    ),
    bondAvgHigh=62,
    bondAvgLow=28,
    bondAvgPause=10,
    bondHighWeight=0.45,
    bondLowWeight=0.55,
    bondHighFrac=0.5,
    bondLowFrac=0.75,
    bondYieldWarn=1.4,
    bondAttractiveY=2.4,
    bondRichY=1.65,
    ndxPbWarn=97,
    ndxPbStop=99.9,
    ndxPegHi=4.0,
    ndxPegMid=2.8,
    ndxErpStop=-99,
    ndxErpWarn=-6.0,
    ndxErpSoft=-3.0,
    ndxDualStop=99.5,
    spBogleStop=1,
    spBogleWarnLo=4.5,
    spBogleWarnHi=4.5,
    spBogleBuy=7,
    spBogleUp=6,
    spErpWarn=-3.0,
    spErpSoft=-3.0,
    spDualStop=98,
    spPePauseGate=95,
    # 启发预期偏低：PE分位>75 降谨慎（70 过勤；75→标普激进分约42）
    spPeWarnGate=75,
    qdiiPremStop=5,
    qdiiPremWarn=3,
    divPeCheap=8,
    divYldFloor=4.8,
    divYldStrong=5.5,
    divYldHot=6.5,
    divPremBond=4.0,
    divErpBoost=6.0,
    divUpgradePb=45,
    divUpgradeScore=50,
)

PORTFOLIO = [
    dict(code="513100", name="纳指100", ratio=0.12, index="NDX", method="ndx", secid="1.513100"),
    dict(code="513500", name="标普500", ratio=0.28, index="SP500", method="sp500", secid="1.513500"),
    dict(code="515450", name="红利低波", ratio=0.40, index="CSIH30269", method="dividend", secid="1.515450"),
    dict(code="511260", name="十年国债", ratio=0.20, index=None, method="bond", secid="1.511260"),
]

RANK = {"积极买入": 5, "正常买入": 4, "可以买入": 3, "谨慎买入": 2, "暂停买入": 1, "减半买入": 2}
LEVEL = {
    "积极买入": "buy",
    "正常买入": "ok",
    "可以买入": "ok",
    "谨慎买入": "warn",
    "暂停买入": "stop",
    "减半买入": "warn",
}


def fetch(url: str, timeout: int = 12) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "magic-backtest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url: str, timeout: int = 12) -> Any:
    return json.loads(fetch(url, timeout))


def list_djeva_index_dates() -> list[str]:
    text = fetch(
        "https://cdn.jsdelivr.net/gh/caibingcheng/djeva@master/djeva.js", timeout=20
    ).decode("utf-8", "ignore")
    m = re.search(r"return (\{.*\});", text, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))["data"]
    dates = []
    for y in sorted(data):
        for mo in sorted(data[y], key=lambda x: int(x)):
            for day in sorted(data[y][mo], key=lambda x: int(x)):
                dates.append(f"{y}-{str(mo).zfill(2)}-{str(day).zfill(2)}")
    return dates


def load_djeva_dates(max_samples: int = 100) -> list[tuple[str, dict[str, Any]]]:
    """Use djeva.js index; sample ~weekly across available history."""
    import concurrent.futures

    all_dates = list_djeva_index_dates()
    if not all_dates:
        print("djeva index empty", file=sys.stderr)
        return []
    step = max(1, len(all_dates) // max_samples)
    sample = all_dates[::step]
    if all_dates[-1] not in sample:
        sample.append(all_dates[-1])

    def one(ds: str):
        url = f"https://cdn.jsdelivr.net/gh/caibingcheng/djeva@master/json/{ds}.json"
        try:
            items = fetch_json(url, timeout=10)
        except Exception:
            return None
        if not isinstance(items, list) or not items:
            return None
        m = {i["index_code"]: i for i in items if "index_code" in i}
        return ds, m

    hits: list[tuple[str, dict[str, Any]]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(one, sample):
            if res:
                hits.append(res)
    hits.sort(key=lambda x: x[0])
    return hits


def recent_weekdays(n: int = 780) -> list[str]:
    out = []
    d = date.today()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out


def pct(v: dict, key: str) -> float | None:
    x = v.get(key)
    if x is None or x == "":
        return None
    try:
        return float(x) * 100
    except (TypeError, ValueError):
        return None


def yield_pct(v: dict) -> float | None:
    return pct(v, "yeild") if pct(v, "yeild") is not None else pct(v, "yield")


def ey(v: dict) -> float:
    pe = float(v.get("pe") or 0)
    return 100 / pe if pe > 0 else 0


def erp(v: dict, bond: float) -> float:
    return ey(v) - bond


def zone(score: float) -> str:
    if score <= VAL["zoneLow"]:
        return "低估区"
    if score < VAL["zoneHigh"]:
        return "合理区"
    return "偏高区"


def sig_of(action: str) -> dict:
    return {"action": action, "level": LEVEL[action], "rank": RANK[action]}


def from_pct(p: float, div_bonus: bool = False, bands: tuple | None = None) -> dict:
    b = bands or (VAL["cheap"], VAL["normal"], VAL["mild"], VAL["rich"])
    if p <= b[0]:
        a = "积极买入"
    elif p < b[1]:
        a = "正常买入"
    elif p < b[2]:
        a = "可以买入"
    elif p < b[3]:
        a = "谨慎买入"
    else:
        a = "暂停买入"
    s = sig_of(a)
    if div_bonus and p < b[3] - 10 and s["level"] == "warn":
        s = sig_of("可以买入")
    return s


def action_proxy(action: str) -> float:
    return {
        "积极买入": 15.0,
        "正常买入": 40.0,
        "可以买入": 60.0,
        "谨慎买入": 75.0,
        "减半买入": 75.0,
        "暂停买入": 92.0,
    }.get(action, 70.0)


def down(sig: dict, action: str) -> dict:
    nxt = sig_of(action)
    return nxt if nxt["rank"] < sig["rank"] else sig


def up(sig: dict, action: str) -> dict:
    nxt = sig_of(action)
    return nxt if nxt["rank"] > sig["rank"] else sig


def force(action: str) -> dict:
    return sig_of(action)


def heuristic(v: dict) -> float | None:
    pe_p = pct(v, "pe_percentile")
    pe = float(v.get("pe") or 0)
    if pe_p is None or pe <= 0:
        return None
    div = yield_pct(v) or 0
    roe = pct(v, "roe")
    growth = min(roe * 0.1, 2) if roe is not None else min(div * 0.25, 1.2)
    return ey(v) + growth - (pe_p - 50) * 0.08


def old_bogle(v: dict) -> float:
    div = yield_pct(v) or 0
    roe = pct(v, "roe") or 0
    pe_p = pct(v, "pe_percentile") or 50
    return div + roe * 0.3 - (pe_p - 50) * 0.08


def eval_ndx(v: dict, usy: float = US10Y) -> dict:
    pe_p, pb_p = pct(v, "pe_percentile"), pct(v, "pb_percentile")
    if pe_p is None or pb_p is None:
        return {"action": "暂无数据", "level": "na", "score": None}
    peg = float(v.get("peg") or 0)
    e = erp(v, usy)
    score = pb_p * 0.6 + pe_p * 0.4
    bands = VAL["bands"]["ndx"]
    sig = from_pct(score, False, bands)
    if pb_p >= VAL["ndxPbWarn"]:
        sig = down(sig, "谨慎买入")
    if pb_p >= VAL["ndxPbStop"] or score >= bands[3]:
        sig = force("暂停买入")
    if peg > VAL["ndxPegHi"] and score >= 70:
        sig = down(sig, "谨慎买入")
    elif peg > VAL["ndxPegMid"] and score >= 80:
        sig = down(sig, "谨慎买入")
    if e < VAL["ndxErpWarn"] and score >= 70:
        sig = down(sig, "谨慎买入")
    elif e < VAL["ndxErpSoft"] and score >= 75:
        sig = down(sig, "谨慎买入")
    if pe_p >= VAL["ndxDualStop"] and pb_p >= VAL["ndxDualStop"]:
        sig = force("暂停买入")
    return {**sig, "score": score, "zone": zone(score)}


def eval_sp(v: dict, usy: float = US10Y, use_new: bool = True) -> dict:
    pe_p = pct(v, "pe_percentile")
    if pe_p is None:
        return {"action": "暂无数据", "level": "na", "score": None}
    pb_p = pct(v, "pb_percentile")
    div = yield_pct(v) or 0
    expected = heuristic(v) if use_new else old_bogle(v)
    e = erp(v, usy)
    bands = VAL["bands"]["sp500"]
    sig = from_pct(pe_p, div > 2.5, bands)
    if expected is not None:
        if expected < VAL["spBogleStop"] and pe_p > VAL["spPePauseGate"]:
            sig = force("暂停买入")
        elif expected < VAL["spBogleWarnHi"] and pe_p > VAL["spPeWarnGate"]:
            sig = down(sig, "谨慎买入")
        elif expected > VAL["spBogleBuy"] and pe_p < 55:
            sig = force("积极买入")
        elif expected > VAL["spBogleUp"] and pe_p < 50 and e > 0.5:
            sig = up(sig, "积极买入")
    if pb_p is not None and pb_p >= VAL["ndxPbWarn"]:
        sig = down(sig, "谨慎买入")
    if pe_p >= VAL["spDualStop"] and pb_p is not None and pb_p >= VAL["spDualStop"]:
        sig = force("暂停买入")
    if e < VAL["spErpWarn"] and pe_p > 72:
        sig = down(sig, "谨慎买入")
    return {**sig, "score": pe_p, "zone": zone(pe_p), "expected": expected}


def eval_div(v: dict, cny: float = CN10Y, use_new: bool = True) -> dict:
    pe_p, pb_p = pct(v, "pe_percentile"), pct(v, "pb_percentile")
    if pe_p is None or pb_p is None:
        return {"action": "暂无数据", "level": "na", "score": None}
    div = yield_pct(v) or 0
    roe = pct(v, "roe")
    pe = float(v.get("pe") or 0) or None
    score = pb_p * 0.65 + pe_p * 0.35
    bands = VAL["bands"]["dividend"]
    div_prem = div - cny
    div_erp = ey(v) - cny
    sig = from_pct(score, False, bands)
    if div < VAL["divYldFloor"] and score >= bands[1]:
        sig = down(sig, "谨慎买入")
    if (
        div > VAL["divYldHot"]
        and pb_p < VAL["divUpgradePb"]
        and score < VAL["divUpgradeScore"]
        and sig["level"] != "stop"
    ):
        sig = up(sig, "正常买入")
    elif (
        div > VAL["divYldStrong"]
        and pb_p < VAL["divUpgradePb"]
        and score < VAL["divUpgradeScore"]
        and sig["level"] != "stop"
    ):
        sig = up(sig, "可以买入")
    if pe is not None and pe <= VAL["divPeCheap"] and score < 55 and sig["level"] != "stop":
        sig = up(sig, "可以买入")
    if (
        div_prem >= VAL["divPremBond"]
        and score < 50
        and pb_p < 50
        and sig["level"] != "stop"
    ):
        sig = up(sig, "可以买入")
    if div_erp >= VAL["divErpBoost"] and score < 50 and sig["level"] != "stop":
        sig = up(sig, "可以买入")
    if div > VAL["divYldStrong"] and roe is not None and roe < 7:
        sig = down(sig, "谨慎买入")
    return {**sig, "score": score, "zone": zone(score)}


def eval_bond_new(avg: float, high_n: int, low_n: int, high_w: float, low_w: float, n: int, y: float | None) -> dict:
    has = y is not None
    yy = y if has else CN10Y
    attractive = VAL.get("bondAttractiveY", CN_ATTRACTIVE)
    high_need = max(1, math.ceil(n * VAL["bondHighFrac"]))
    low_need = max(1, math.ceil(n * VAL["bondLowFrac"]))
    rich = avg >= VAL["bondAvgHigh"] or high_w >= VAL["bondHighWeight"] or high_n >= high_need
    cheap = avg <= VAL["bondAvgLow"] or low_w >= VAL["bondLowWeight"] or low_n >= low_need
    if rich:
        return force("积极买入" if has and yy >= attractive else "可以买入") | {"score": avg}
    if cheap:
        if avg <= VAL["bondAvgPause"]:
            return force("暂停买入") | {"score": avg}
        return force("谨慎买入") | {"score": avg}
    if has and yy < VAL["bondYieldWarn"] and avg < 50:
        return force("谨慎买入") | {"score": avg, "yieldDriven": True}
    return force("正常买入") | {"score": avg}


def eval_bond_old(avg: float, high_n: int, low_n: int, y: float | None) -> dict:
    has = y is not None
    yy = y if has else CN10Y
    if avg >= 75 or high_n >= 3:
        return force("积极买入" if has and yy >= CN_ATTRACTIVE else "可以买入") | {"score": avg}
    if avg <= 40 or low_n >= 2:
        if avg <= 15:
            return force("暂停买入") | {"score": avg}
        return force("谨慎买入") | {"score": avg}
    if has and yy < CN_RICH:
        return force("谨慎买入") | {"score": avg, "yieldDriven": True}
    return force("正常买入") | {"score": avg}


def deploy_factor(sig: dict, method: str) -> float:
    if not sig or sig.get("level") == "na" or sig.get("action") == "暂无数据":
        return 0.0
    if sig.get("hardStop") or sig.get("level") == "stop":
        return 0.0
    cfg = VAL.get("deploy", {}).get(method, {})
    if method == "bond" and cfg.get("fullWhenOk") and sig.get("level") in ("ok", "buy"):
        return 1.0
    score = sig.get("score")
    if score is None:
        score = action_proxy(sig["action"])
    score = max(0, min(100, float(score)))
    if method != "bond" and sig.get("action"):
        proxy = action_proxy(sig["action"])
        pull = cfg.get("pullMax", VAL.get("upgradePullMax", 10))
        if proxy < score:
            score = max(proxy, score - pull)
    if method == "dividend" and cfg.get("scoreBias"):
        score = min(100, score + cfg["scoreBias"])
    if method in ("ndx", "sp500") and sig.get("level") == "warn" and cfg.get("warnScoreCap") is not None:
        score = min(score, cfg["warnScoreCap"])
    if method == "bond":
        inv = 100 - score
        if sig.get("level") == "warn" and cfg.get("warnInvCap") is not None:
            inv = min(inv, cfg["warnInvCap"])
        score = inv
    cheap = cfg.get("cheap", VAL["cheap"])
    rich = cfg.get("rich", VAL["rich"])
    wcap = cfg.get("warnCap", VAL.get("warnCap", 0.5))
    if score <= cheap:
        f = 1.0
    elif score >= rich:
        f = 0.0
    else:
        t = (score - cheap) / (rich - cheap)
        f = 0.5 * (1 + math.cos(math.pi * t))
    if cfg.get("peakCap") is not None:
        f = min(f, float(cfg["peakCap"]))
    if sig.get("level") == "warn":
        f = min(f, wcap)
    return f


def apply_overlays(sig: dict, code: str, premium_pct: float | None = None, usd_pct: float | None = None) -> dict:
    """Mirror index.html applyPremiumAdvice: QDII premium hard-stop + USD-strong downgrade."""
    if not sig or sig.get("level") == "na" or sig.get("action") == "暂无数据":
        return sig
    r = dict(sig)
    is_qdii = code in QDII_CODES
    if is_qdii and premium_pct is not None:
        if premium_pct > VAL["qdiiPremStop"]:
            r = force("暂停买入")
            r["hardStop"] = True
            r["score"] = sig.get("score")
            r["zone"] = sig.get("zone")
        elif premium_pct > VAL["qdiiPremWarn"] and r.get("level") in ("buy", "ok"):
            r = force("谨慎买入")
            r["score"] = sig.get("score")
            r["zone"] = sig.get("zone")
    if is_qdii and usd_pct is not None and usd_pct >= 80 and r.get("level") in ("buy", "ok"):
        r = force("谨慎买入")
        r["score"] = sig.get("score")
        r["zone"] = sig.get("zone")
        r["hardStop"] = r.get("hardStop", False)
    return r


def usdcny_percentile(rate: float, hist_rates: list[float]) -> float:
    """Match frontend: empirical rank if enough samples, else damped linear 6.5→0 / 7.4→100."""
    if rate <= FX_REF_LOW:
        linear = 0.0
    elif rate >= FX_REF_HIGH:
        linear = 100.0
    else:
        linear = ((rate - FX_REF_LOW) / (FX_REF_HIGH - FX_REF_LOW)) * 100
    if len(hist_rates) < 15:
        return linear
    below = sum(1 for x in hist_rates if x <= rate)
    empir = below / len(hist_rates) * 100
    return 0.35 * linear + 0.65 * empir


def load_usdcny_klines(beg: str = "20200101") -> dict[str, float]:
    end = date.today().strftime("%Y%m%d")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid=133.USDCNH&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        f"&klt=101&fqt=1&beg={beg}&end={end}"
    )
    try:
        data = fetch_json(url, timeout=15)
    except Exception as e:
        print(f"usdcny kline fail: {e}", file=sys.stderr)
        return {}
    rows = (data.get("data") or {}).get("klines") or []
    out: dict[str, float] = {}
    for row in rows:
        parts = row.split(",")
        if len(parts) >= 3:
            try:
                out[parts[0]] = float(parts[2])  # close
            except ValueError:
                continue
    return out


def load_fund_nav(code: str) -> dict[str, float]:
    """Eastmoney pingzhongdata unit NAV → date -> nav (for QDII premium vs ETF close)."""
    url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
    try:
        raw = fetch(url, timeout=20)
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    except Exception as e:
        print(f"nav fail {code}: {e}", file=sys.stderr)
        return {}
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[[\s\S]*?\]);", text)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    cn_tz = timezone(timedelta(hours=8))
    out: dict[str, float] = {}
    for item in arr:
        try:
            ts = int(item["x"]) / 1000
            ds = datetime.fromtimestamp(ts, tz=cn_tz).strftime("%Y-%m-%d")
            out[ds] = float(item["y"])
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return out


def calc_premium(price: float | None, nav: float | None) -> float | None:
    if price is None or nav is None or nav <= 0 or price <= 0:
        return None
    return (price - nav) / nav * 100


def _plausible_yield_pct(raw: Any, lo: float, hi: float) -> float | None:
    """djeva 的 bond_yeild 常年写死 0.05（=5%），不能当真实国债收益率。

    只剔除占位 5% 附近；真实美债曾到 5%+，不能用 hi=4.9 一刀切。
    """
    if raw is None or raw == "":
        return None
    try:
        y = float(raw) * 100
    except (TypeError, ValueError):
        return None
    # 占位值：精确 5.0，或历史快照里偶发的 5.00% 脏数据
    if abs(y - 5.0) < 1e-6:
        return None
    if lo < y < hi:
        return y
    return None


def snapshot_yields(mmap: dict[str, Any]) -> tuple[float, float]:
    """Try per-snapshot bond_yeild; reject placeholder 5%; else defaults."""
    us, cn = None, None
    for code in ("SP500", "NDX"):
        v = mmap.get(code) or {}
        us = _plausible_yield_pct(v.get("bond_yeild", v.get("bond_yield")), 0.5, 7.5)
        if us is not None:
            break
    for code in ("CSIH30269", "CSI000922", "HSCEI", "HKHSCEI"):
        v = mmap.get(code) or {}
        cn = _plausible_yield_pct(v.get("bond_yeild", v.get("bond_yield")), 0.5, 6.0)
        if cn is not None:
            break
    return (us if us is not None else US10Y), (cn if cn is not None else CN10Y)


def evaluate_day(
    mmap: dict[str, Any],
    use_new: bool = True,
    usy: float | None = None,
    cny: float | None = None,
    overlays: dict[str, dict[str, float | None]] | None = None,
) -> dict[str, dict]:
    su, sc = snapshot_yields(mmap)
    usy = su if usy is None else usy
    cny = sc if cny is None else cny
    overlays = overlays or {}
    out: dict[str, dict] = {}
    equity = []
    for p in PORTFOLIO:
        if p["method"] == "bond":
            continue
        v = mmap.get(p["index"])
        if not v:
            out[p["code"]] = {"action": "暂无数据", "level": "na", "score": None}
            continue
        if p["method"] == "ndx":
            r = eval_ndx(v, usy)
        elif p["method"] == "sp500":
            r = eval_sp(v, usy, use_new=use_new)
        else:
            r = eval_div(v, cny, use_new=use_new)
        ov = overlays.get(p["code"]) or {}
        r = apply_overlays(r, p["code"], ov.get("premium"), ov.get("usd_pct"))
        out[p["code"]] = r
        if r.get("score") is not None:
            equity.append({"score": r["score"], "zone": r["zone"], "weight": p["ratio"]})
    bond = next(p for p in PORTFOLIO if p["method"] == "bond")
    if not equity:
        out[bond["code"]] = {"action": "暂无数据", "level": "na", "score": None}
    else:
        tw = sum(e["weight"] for e in equity)
        avg = sum(e["score"] * e["weight"] for e in equity) / tw
        high_n = sum(1 for e in equity if e["zone"] == "偏高区")
        low_n = sum(1 for e in equity if e["zone"] == "低估区")
        high_w = sum(e["weight"] for e in equity if e["zone"] == "偏高区") / tw
        low_w = sum(e["weight"] for e in equity if e["zone"] == "低估区") / tw
        if use_new:
            out[bond["code"]] = eval_bond_new(avg, high_n, low_n, high_w, low_w, len(equity), cny)
        else:
            out[bond["code"]] = eval_bond_old(avg, high_n, low_n, cny)
    for p in PORTFOLIO:
        r = out[p["code"]]
        r["factor"] = deploy_factor(r, p["method"])
    return out


def day_overlays(
    ds: str,
    prices: dict[str, dict[str, float]],
    navs: dict[str, dict[str, float]],
    fx: dict[str, float],
) -> dict[str, dict[str, float | None]]:
    """Per-code premium% (price vs NAV) + USDCNY percentile for QDII overlays."""
    fx_hist = [v for k, v in fx.items() if k <= ds]
    fx_rate = nearest_price(fx, ds)
    usd_pct = usdcny_percentile(fx_rate, fx_hist) if fx_rate is not None else None
    out: dict[str, dict[str, float | None]] = {}
    for p in PORTFOLIO:
        if p["code"] not in QDII_CODES:
            continue
        pr = nearest_price(prices.get(p["code"], {}), ds)
        nav = nearest_price(navs.get(p["code"], {}), ds)
        out[p["code"]] = {
            "premium": calc_premium(pr, nav),
            "usd_pct": usd_pct,
        }
    return out


def load_klines(secid: str, beg: str = "20200101") -> dict[str, float]:
    end = date.today().strftime("%Y%m%d")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        f"&klt=101&fqt=1&beg={beg}&end={end}"
    )
    try:
        data = fetch_json(url, timeout=15)
    except Exception as e:
        print(f"kline fail {secid}: {e}", file=sys.stderr)
        return {}
    kl = data.get("data", {}) or {}
    rows = kl.get("klines") or []
    out = {}
    for row in rows:
        parts = row.split(",")
        if len(parts) >= 3:
            out[parts[0]] = float(parts[2])  # close
    return out


def nearest_price(px: dict[str, float], ds: str) -> float | None:
    if ds in px:
        return px[ds]
    # search back up to 10 calendar days
    d = date.fromisoformat(ds)
    for i in range(1, 11):
        k = (d - timedelta(days=i)).isoformat()
        if k in px:
            return px[k]
    return None


def simulate(
    snapshots: list[tuple[str, dict]],
    prices: dict[str, dict[str, float]],
    use_new: bool,
    navs: dict[str, dict[str, float]] | None = None,
    fx: dict[str, float] | None = None,
) -> dict:
    """Weekly valuation-weighted DCA vs fixed-ratio DCA. Invest up to 1000 each rebalance day.

    估值配仓与前端同构：每品种 allocated = cash × ratio × factor，延后部分不回流重分配。
    含 QDII 溢价硬停 + 美元强势降级（与 applyPremiumAdvice 对齐）。
    """
    cash_each = 1000.0
    shares_val = {p["code"]: 0.0 for p in PORTFOLIO}
    shares_dca = {p["code"]: 0.0 for p in PORTFOLIO}
    cash_residual = 0.0
    invested = 0.0
    actions = Counter()
    factors = defaultdict(list)
    navs = navs or {}
    fx = fx or {}
    overlay_hits = Counter()

    for ds, mmap in snapshots:
        day_px = {}
        ok = True
        for p in PORTFOLIO:
            pr = nearest_price(prices[p["code"]], ds)
            if pr is None or pr <= 0:
                ok = False
                break
            day_px[p["code"]] = pr
        if not ok:
            continue

        ov = day_overlays(ds, prices, navs, fx)
        for code, o in ov.items():
            if o.get("premium") is not None and o["premium"] > 3:
                overlay_hits["premium>3"] += 1
            if o.get("usd_pct") is not None and o["usd_pct"] >= 80:
                overlay_hits["usd>=80"] += 1

        sigs = evaluate_day(mmap, use_new=use_new, overlays=ov)
        # 与前端：目标×系数，延后不归一到其他品种
        alloc = {}
        spent = 0.0
        for p in PORTFOLIO:
            f = sigs[p["code"]].get("factor") or 0
            actions[f"{p['name']}:{sigs[p['code']].get('action')}"] += 1
            factors[p["code"]].append(f)
            yuan = cash_each * p["ratio"] * max(0.0, f)
            alloc[p["code"]] = yuan
            spent += yuan
        deferred = max(0.0, cash_each - spent)
        cash_residual += deferred

        dca_alloc = {p["code"]: cash_each * p["ratio"] for p in PORTFOLIO}

        for p in PORTFOLIO:
            pr = day_px[p["code"]]
            shares_val[p["code"]] += alloc[p["code"]] / pr
            shares_dca[p["code"]] += dca_alloc[p["code"]] / pr
        invested += cash_each

    last_ds = snapshots[-1][0] if snapshots else None
    last_px = {}
    for p in PORTFOLIO:
        pr = nearest_price(prices[p["code"]], last_ds) if last_ds else None
        if pr is None:
            series = prices[p["code"]]
            pr = series[max(series)] if series else None
        last_px[p["code"]] = pr or 0

    def port_value(shares):
        return sum(shares[c] * last_px[c] for c in shares)

    v_val = port_value(shares_val) + cash_residual
    v_dca = port_value(shares_dca)
    return {
        "invested": invested,
        "val_end": v_val,
        "dca_end": v_dca,
        "val_ret": (v_val / invested - 1) * 100 if invested else 0,
        "dca_ret": (v_dca / invested - 1) * 100 if invested else 0,
        "alpha": (v_val / invested - v_dca / invested) * 100 if invested else 0,
        "cash_residual": cash_residual,
        "actions": actions,
        "avg_factor": {c: (sum(fs) / len(fs) if fs else 0) for c, fs in factors.items()},
        "n_days": int(invested / cash_each) if cash_each else 0,
        "overlay_hits": dict(overlay_hits),
    }


def forward_score_check(snapshots: list[tuple[str, dict]], horizon: int = 12) -> list[str]:
    """After buy/stop, did composite score mean-revert over ~horizon samples?"""
    rows = []
    codes = [(p["code"], p["name"], p["method"], p["index"]) for p in PORTFOLIO if p["method"] != "bond"]
    for i, (ds, mmap) in enumerate(snapshots):
        if i + horizon >= len(snapshots):
            break
        sigs = evaluate_day(mmap, use_new=True)
        _, mmap2 = snapshots[i + horizon]
        for code, name, method, idx in codes:
            s0 = sigs[code]
            if s0.get("score") is None:
                continue
            v2 = mmap2.get(idx)
            if not v2:
                continue
            if method == "ndx":
                s1 = eval_ndx(v2)["score"]
            elif method == "sp500":
                s1 = eval_sp(v2)["score"]
            else:
                s1 = eval_div(v2)["score"]
            if s1 is None:
                continue
            delta = s1 - s0["score"]
            rows.append((s0["action"], name, delta))

    # summarize
    buckets = defaultdict(list)
    for action, name, delta in rows:
        key = "积极/正常" if action in ("积极买入", "正常买入") else (
            "暂停" if action == "暂停买入" else "其他"
        )
        buckets[key].append(delta)
    lines = []
    for k, xs in buckets.items():
        if not xs:
            continue
        lines.append(f"  {k}: n={len(xs)} 后续{horizon}步分位变化均值={sum(xs)/len(xs):+.1f}pt")
    return lines


def main():
    print("加载 djeva 估值快照…")
    snaps = load_djeva_dates(100)
    print(f"  有效快照 {len(snaps)} 个：{snaps[0][0] if snaps else '—'} → {snaps[-1][0] if snaps else '—'}")
    if len(snaps) < 10:
        print("快照太少，退出")
        sys.exit(1)

    print("加载 ETF 日线…")
    import concurrent.futures

    prices = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(load_klines, p["secid"]): p for p in PORTFOLIO}
        for fut, p in futs.items():
            prices[p["code"]] = fut.result()
            print(f"  {p['name']} {len(prices[p['code']])} bars")

    print("加载 USDCNY + QDII 净值（溢价/美元覆盖）…")
    fx = load_usdcny_klines()
    print(f"  USDCNY {len(fx)} bars")
    navs: dict[str, dict[str, float]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(load_fund_nav, code): code for code in QDII_CODES}
        for fut, code in futs.items():
            navs[code] = fut.result()
            print(f"  NAV {code} {len(navs[code])} pts")

    print("\n=== 新算法 vs 定额定投 ===")
    new = simulate(snaps, prices, use_new=True, navs=navs, fx=fx)
    print(f"  再平衡次数: {new['n_days']}")
    print(f"  累计投入: {new['invested']:.0f}")
    print(f"  估值配仓终值: {new['val_end']:.0f}  收益 {new['val_ret']:+.2f}%")
    print(f"  定额定投终值: {new['dca_end']:.0f}  收益 {new['dca_ret']:+.2f}%")
    print(f"  超额(估值−定额): {new['alpha']:+.2f}pt")
    print(f"  估值延后现金余额: {new.get('cash_residual', 0):.0f}")
    print(f"  覆盖触发: {new.get('overlay_hits')}")
    print("  平均部署系数:")
    for p in PORTFOLIO:
        print(f"    {p['name']}: {new['avg_factor'].get(p['code'], 0):.2f}")

    print("\n=== 旧算法对照（启发预期旧式 + 国债 highCount≥3）===")
    old = simulate(snaps, prices, use_new=False, navs=navs, fx=fx)
    print(f"  估值配仓终值: {old['val_end']:.0f}  收益 {old['val_ret']:+.2f}%")
    print(f"  超额(旧−定额): {old['alpha']:+.2f}pt")
    print(f"  新旧超额差(新−旧): {new['alpha'] - old['alpha']:+.2f}pt")

    print("\n=== 最新一日信号 ===")
    last_ds, last_map = snaps[-1]
    last_ov = day_overlays(last_ds, prices, navs, fx)
    latest = evaluate_day(last_map, use_new=True, overlays=last_ov)
    for p in PORTFOLIO:
        r = latest[p["code"]]
        print(
            f"  {p['name']}: {r.get('action')}  score={r.get('score')}  factor={r.get('factor', 0):.2f}"
        )

    print("\n=== 信号分布（新算法，全样本，含覆盖）===")
    dist = Counter()
    for ds, mmap in snaps:
        ov = day_overlays(ds, prices, navs, fx)
        for p in PORTFOLIO:
            r = evaluate_day(mmap, use_new=True, overlays=ov)[p["code"]]
            dist[f"{p['name']}|{r.get('action')}"] += 1
    by_name = defaultdict(list)
    for k, n in dist.items():
        name, act = k.split("|", 1)
        by_name[name].append((act, n))
    for p in PORTFOLIO:
        total = sum(n for _, n in by_name[p["name"]])
        parts = sorted(by_name[p["name"]], key=lambda x: -x[1])
        s = " · ".join(f"{a} {n/total*100:.0f}%" for a, n in parts)
        print(f"  {p['name']}: {s}")

    print("\n=== 分位均值回归抽检（约12个采样点后）===")
    for line in forward_score_check(snaps, horizon=max(4, len(snaps) // 20)):
        print(line)

    # upgrade bug regression: 可以→正常 must work
    print("\n=== 升降级回归 ===")
    s = sig_of("可以买入")
    s2 = up(s, "正常买入")
    print(f"  可以→正常: {s2['action']} ({'OK' if s2['action']=='正常买入' else 'FAIL'})")
    s3 = up(sig_of("谨慎买入"), "可以买入")
    print(f"  谨慎→可以: {s3['action']} ({'OK' if s3['action']=='可以买入' else 'FAIL'})")

    # 标普：股息加成后，预期偏低须能降回谨慎（pe 门闸 75，覆盖中高分位）
    sp_case = dict(
        pe=28, pe_percentile=0.78, pb_percentile=0.50, roe=0.10, yeild=0.03
    )
    sp_r = eval_sp(sp_case, 4.3)
    print(
        f"  标普预期回降: {sp_r['action']} expected={sp_r['expected']:.2f} "
        f"({'OK' if sp_r['action']=='谨慎买入' else 'FAIL'})"
    )
    # WarnHi 档：peP>75 须盖住股息加成抬档
    sp_hi = dict(
        pe=22, pe_percentile=0.76, pb_percentile=0.50, roe=0.12, yeild=0.03
    )
    sp_r_hi = eval_sp(sp_hi, 4.3)
    print(
        f"  标普WarnHi盖>75: {sp_r_hi['action']} expected={sp_r_hi['expected']:.2f} "
        f"({'OK' if sp_r_hi['action']=='谨慎买入' else 'FAIL'})"
    )
    # 标普：仅 PB 极端不硬停
    sp_pb = dict(
        pe=22, pe_percentile=0.60, pb_percentile=0.96, roe=0.15, yeild=0.015
    )
    sp_r2 = eval_sp(sp_pb, 4.3)
    print(
        f"  标普高PB不硬停: {sp_r2['action']} "
        f"({'OK' if sp_r2['action']!='暂停买入' else 'FAIL'})"
    )
    # djeva 占位 bond_yeild=0.05 须忽略
    sy = snapshot_yields(
        {"SP500": {"bond_yeild": 0.05}, "CSIH30269": {"bond_yeild": 0.05}}
    )
    print(
        f"  忽略占位国债: us={sy[0]} cn={sy[1]} "
        f"({'OK' if sy==(US10Y, CN10Y) else 'FAIL'})"
    )
    # QDII 溢价/美元覆盖
    base = force("正常买入") | {"score": 50, "zone": "合理区"}
    prem_stop = apply_overlays(base, "513100", premium_pct=5.5)
    print(
        f"  溢价>5硬停: {prem_stop['action']} hardStop={prem_stop.get('hardStop')} "
        f"({'OK' if prem_stop['action']=='暂停买入' and prem_stop.get('hardStop') else 'FAIL'})"
    )
    usd_warn = apply_overlays(base, "513100", usd_pct=85)
    print(
        f"  美元≥80降级: {usd_warn['action']} "
        f"({'OK' if usd_warn['action']=='谨慎买入' else 'FAIL'})"
    )
    # 国债积极门槛 = bondAttractiveY
    bond_ok = eval_bond_new(70, 2, 0, 0.6, 0, 3, 2.5)
    bond_lo = eval_bond_new(70, 2, 0, 0.6, 0, 3, 2.0)
    print(
        f"  国债积极门槛2.4: {bond_ok['action']}/{bond_lo['action']} "
        f"({'OK' if bond_ok['action']=='积极买入' and bond_lo['action']=='可以买入' else 'FAIL'})"
    )
    # 红利股息地板
    div_lo = eval_div(
        dict(pe=12, pe_percentile=0.55, pb_percentile=0.55, yeild=0.03, roe=0.12),
        1.8,
    )
    print(
        f"  红利低息降级: {div_lo['action']} "
        f"({'OK' if div_lo['action']=='谨慎买入' else 'FAIL'})"
    )


if __name__ == "__main__":
    main()
