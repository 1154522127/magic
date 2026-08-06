/**
 * magic-proxy — 蛋卷估值代理 + 515450 持仓加权历史（KV）+ 定时采集
 *
 * GET /                 健康检查
 * GET /valuation        蛋卷指数估值（CORS）
 * GET /etf_fundamentals?code=515450  （只读，不落盘）
 * GET /etf_yield?code=515450
 * GET /etf_history?code=515450
 * GET /?url=https://... 兼容旧版白名单转发
 * Cron: 北京时间工作日 22:08 / 22:38 / 23:08 / 23:38 → KV + 推 GitHub
 * 交易日检查失败时工作日仍继续采集，避免东财偶发超时导致整晚空跑
 */

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

const DANJUAN_URLS = [
  "https://danjuanfunds.com/djapi/index_eva/dj",
  "https://danjuanapp.com/djapi/index_eva/dj",
];

const DANJUAN_HEADERS = {
  Referer: "https://danjuanfunds.com/djmodule/value-center",
  "User-Agent": UA,
};

const ALLOWED_PROXY_HOSTS = new Set([
  "danjuanfunds.com",
  "www.danjuanfunds.com",
  "danjuanapp.com",
  "www.danjuanapp.com",
  "push2delay.eastmoney.com",
  "push2.eastmoney.com",
  "push2his.eastmoney.com",
  "fundf10.eastmoney.com",
  "fundmobapi.eastmoney.com",
]);

const MIN_HISTORY_DAYS = 60;
const MAX_HISTORY_DAYS = 30 * 252;
const DEFAULT_CODE = "515450";
/** 季报通常只披前十；年报/半年报才有全量。 */
const FULL_HOLDINGS_WEIGHT_MIN = 90;
const FULL_HOLDINGS_COUNT_MIN = 40;

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders(), "Content-Type": "application/json; charset=utf-8" },
  });
}

function beijingParts(d = new Date()) {
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  const parts = Object.fromEntries(
    fmt.formatToParts(d).filter((p) => p.type !== "literal").map((p) => [p.type, p.value]),
  );
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    collected_at: `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}`,
  };
}

async function httpGet(url, headers = {}) {
  const res = await fetch(url, {
    headers: { "User-Agent": UA, ...headers },
    redirect: "follow",
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`);
  return res;
}

async function httpText(url, headers = {}) {
  return (await httpGet(url, headers)).text();
}

async function httpJson(url, headers = {}) {
  return (await httpGet(url, headers)).json();
}

function stockSecid(code) {
  return (String(code).startsWith("6") ? "1." : "0.") + code;
}

function stripTags(s) {
  return String(s).replace(/<[^>]+>/g, "").trim();
}

function parseF10Content(content) {
  const tables = [];
  for (const part of content.matchAll(
    /<h4 class='t'>(.*?)<\/h4>[\s\S]*?<table[^>]*>(.*?)<\/table>/g,
  )) {
    const title = stripTags(part[1]);
    const dm = title.match(/(\d{4}-\d{2}-\d{2})/);
    const date = dm ? dm[1] : "";
    const table = part[2];
    const ths = [...table.matchAll(/<th[^>]*>(.*?)<\/th>/gs)].map((x) =>
      stripTags(x[1]),
    );
    const ccol = ths.indexOf("股票代码");
    const ncol = ths.indexOf("股票名称");
    const wcol = ths.indexOf("占净值比例");
    if (ccol < 0 || ncol < 0 || wcol < 0) continue;
    const rows = [];
    const seen = new Set();
    for (const trMatch of table.matchAll(/<tr>(.*?)<\/tr>/gs)) {
      const tds = [...trMatch[1].matchAll(/<td[^>]*>(.*?)<\/td>/gs)].map((x) =>
        stripTags(x[1]),
      );
      if (tds.length <= Math.max(ccol, ncol, wcol)) continue;
      const scode = tds[ccol];
      if (!/^\d{6}$/.test(scode) || seen.has(scode)) continue;
      const wraw = tds[wcol];
      if (!/^[0-9.]+%$/.test(wraw)) continue;
      const weight = parseFloat(wraw);
      if (!(weight > 0)) continue;
      seen.add(scode);
      rows.push({ code: scode, name: tds[ncol], weight });
    }
    if (rows.length) {
      tables.push({
        date,
        rows,
        weight_sum: rows.reduce((s, r) => s + r.weight, 0),
      });
    }
  }
  return tables;
}

async function fetchF10Page(code, year = "", month = "") {
  const url =
    "https://fundf10.eastmoney.com/FundArchivesDatas.aspx" +
    `?type=jjcc&code=${code}&topline=100&year=${year}&month=${month}`;
  const text = await httpText(url, { Referer: "https://fundf10.eastmoney.com/" });
  const m = text.match(/content:"((?:[^"\\]|\\.)*)"/);
  if (!m) return { tables: [], years: [] };
  const content = m[1].replace(/\\"/g, '"').replace(/\\\//g, "/");
  const ym = text.match(/arryear:(\[[^\]]+\])/);
  let years = [];
  if (ym) {
    try {
      years = JSON.parse(ym[1]);
    } catch {
      years = [];
    }
  }
  return { tables: parseF10Content(content), years };
}

async function parseMobileHoldings(code) {
  const url =
    "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNInverstPosition" +
    `?FCODE=${code}&deviceid=Wap&plat=Wap&product=EFund&version=2.0.0`;
  const data = await httpJson(url, { Referer: "https://fund.eastmoney.com/" });
  const stocks = (data?.Datas || {}).fundStocks || [];
  const out = [];
  for (const s of stocks) {
    const scode = String(s.GPDM || "");
    const weight = parseFloat(s.JZBL || 0);
    if (/^\d{6}$/.test(scode) && weight > 0) {
      out.push({ code: scode, name: s.GPJC || scode, weight });
    }
  }
  return out;
}

function isFullHoldings(weightSum, n) {
  return weightSum >= FULL_HOLDINGS_WEIGHT_MIN && n >= FULL_HOLDINGS_COUNT_MIN;
}

function pickBestHoldingsTable(candidates) {
  const full = candidates.filter((t) =>
    isFullHoldings(t.weight_sum, t.rows.length),
  );
  const pool = full.length ? full : candidates;
  return pool.reduce((a, b) => {
    if ((b.date || "") !== (a.date || "")) {
      return (b.date || "") > (a.date || "") ? b : a;
    }
    if (b.weight_sum !== a.weight_sum) {
      return b.weight_sum > a.weight_sum ? b : a;
    }
    return b.rows.length > a.rows.length ? b : a;
  });
}

async function resolveHoldings(code) {
  const first = await fetchF10Page(code, "", "");
  const candidates = [];
  const seenDates = new Set();
  for (const t of first.tables) {
    const d = t.date || "";
    if (seenDates.has(d)) continue;
    seenDates.add(d);
    candidates.push(t);
  }
  // 默认页常已含最近全量年报/中报；够用则不再按年翻页，降低东财限流/超时风险
  const alreadyFull = candidates.some((t) =>
    isFullHoldings(t.weight_sum, t.rows.length),
  );
  if (!alreadyFull) {
    let years = first.years;
    if (!years.length) {
      const y = new Date().getFullYear();
      years = [y, y - 1];
    }
    for (const year of years.slice(0, 4)) {
      const { tables } = await fetchF10Page(code, String(year), "");
      for (const t of tables) {
        const d = t.date || "";
        if (seenDates.has(d)) continue;
        seenDates.add(d);
        candidates.push(t);
      }
    }
  }
  if (!candidates.length) {
    return { holdings: await parseMobileHoldings(code), holdings_asof: null };
  }
  const best = pickBestHoldingsTable(candidates);
  let rows = [...best.rows].sort((a, b) => b.weight - a.weight);
  if (!isFullHoldings(best.weight_sum, rows.length)) {
    const have = new Set(rows.map((r) => r.code));
    for (const r of await parseMobileHoldings(code)) {
      if (!have.has(r.code)) rows.push(r);
    }
    rows = rows.sort((a, b) => b.weight - a.weight);
  }
  return { holdings: rows, holdings_asof: best.date || null };
}

async function quoteFundamentals(codes) {
  if (!codes.length) return {};
  const out = {};
  const chunkSize = 80;
  for (let i = 0; i < codes.length; i += chunkSize) {
    const chunk = codes.slice(i, i + chunkSize);
    const ids = chunk.map(stockSecid).join(",");
    const fields = "f12,f14,f9,f23,f133";
    let lastErr;
    let got = null;
    for (const host of ["push2delay.eastmoney.com", "push2.eastmoney.com"]) {
      const url = `https://${host}/api/qt/ulist.np/get?fltt=2&secids=${ids}&fields=${fields}`;
      try {
        const data = await httpJson(url, { Referer: "https://quote.eastmoney.com/" });
        const rows = data?.data?.diff || [];
        got = {};
        for (const row of rows) {
          const code = String(row.f12 || "");
          if (!code) continue;
          const item = {};
          if (typeof row.f9 === "number" && row.f9 > 0) item.pe = row.f9;
          if (typeof row.f23 === "number" && row.f23 > 0) item.pb = row.f23;
          if (typeof row.f133 === "number" && row.f133 > 0) item.yield_pct = row.f133;
          if (Object.keys(item).length) got[code] = item;
        }
        break;
      } catch (e) {
        lastErr = e;
      }
    }
    if (!got) throw new Error(`quote failed: ${lastErr}`);
    Object.assign(out, got);
  }
  return out;
}

function weightedArithmetic(pairs) {
  if (!pairs.length) return null;
  const wSum = pairs.reduce((s, [w]) => s + w, 0);
  if (!(wSum > 0)) return null;
  return pairs.reduce((s, [w, v]) => s + w * v, 0) / wSum;
}

function weightedHarmonic(pairs) {
  if (!pairs.length) return null;
  const wSum = pairs.reduce((s, [w]) => s + w, 0);
  const den = pairs.reduce((s, [w, v]) => (v > 0 ? s + w / v : s), 0);
  if (!(wSum > 0) || !(den > 0)) return null;
  return wSum / den;
}

function historyKey(code) {
  return `etf:${code}`;
}

function emptyHistory(code) {
  return {
    code,
    note:
      "515450持仓加权近似·标普大盘红利低波50；非官方指数点位。" +
      "权重取最近一期年报/半年报全量持仓，估值用当日行情。由 Cloudflare Worker 定时采集。",
    points: [],
  };
}

async function loadHistory(env, code) {
  const raw = await env.HISTORY.get(historyKey(code));
  if (!raw) return emptyHistory(code);
  try {
    const hist = JSON.parse(raw);
    if (!Array.isArray(hist.points)) hist.points = [];
    hist.code = code;
    return hist;
  } catch {
    return emptyHistory(code);
  }
}

async function saveHistory(env, code, hist) {
  hist.code = code;
  hist.note =
    hist.note ||
    "515450持仓加权近似·标普大盘红利低波50；非官方指数点位。" +
      "权重取最近一期年报/半年报全量持仓，估值用当日行情。由 Cloudflare Worker 定时采集。";
  await env.HISTORY.put(historyKey(code), JSON.stringify(hist));
}

async function appendHistoryPoint(env, code, point) {
  const hist = await loadHistory(env, code);
  const d = point.date;
  let points = (hist.points || []).filter((p) => p.date !== d);
  points.push(point);
  // 全量持仓启用后，丢掉旧的「仅前十/半仓」样本，避免污染自建分位
  if (
    (point.n || 0) >= FULL_HOLDINGS_COUNT_MIN ||
    (point.coverage_pct || 0) >= FULL_HOLDINGS_WEIGHT_MIN
  ) {
    points = points.filter(
      (p) =>
        (p.n || 0) >= FULL_HOLDINGS_COUNT_MIN ||
        (p.coverage_pct || 0) >= FULL_HOLDINGS_WEIGHT_MIN,
    );
  }
  points.sort((a, b) => String(a.date).localeCompare(String(b.date)));
  if (points.length > MAX_HISTORY_DAYS) points = points.slice(-MAX_HISTORY_DAYS);
  hist.points = points;
  delete hist.updated_at;
  await saveHistory(env, code, hist);
  return hist;
}

function percentileRank(values, current) {
  const vals = values.filter((v) => typeof v === "number");
  if (!vals.length || typeof current !== "number") return null;
  return vals.filter((v) => v <= current).length / vals.length;
}

function beijingWeekday(d = new Date()) {
  const wd = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    weekday: "short",
  }).format(d);
  return { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }[wd];
}

async function isCnTradingDay(day) {
  const path =
    "/api/qt/stock/kline/get" +
    "?secid=1.000001&fields1=f1,f2,f3,f4,f5,f6" +
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61" +
    "&klt=101&fqt=1&end=20500101&lmt=10";
  const hosts = ["push2his.eastmoney.com", "push2delay.eastmoney.com"];
  let lastErr;
  for (const host of hosts) {
    try {
      const data = await httpJson(`https://${host}${path}`, {
        Referer: "https://quote.eastmoney.com/",
      });
      const klines = data?.data?.klines || [];
      if (!klines.length) continue;
      const dates = klines
        .map((row) => String(row).split(",", 1)[0].trim())
        .filter(Boolean);
      return dates.includes(day);
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("trading-day check: empty klines");
}

async function computeEtfFundamentals(env, code, persist) {
  const { holdings, holdings_asof } = await resolveHoldings(code);
  if (!holdings.length) throw new Error("no holdings");
  const quotes = await quoteFundamentals(holdings.map((h) => h.code));

  const pePairs = [];
  const pbPairs = [];
  const yPairs = [];
  const used = [];
  for (const h of holdings) {
    const q = quotes[h.code] || {};
    const row = { ...h };
    if (q.pe > 0) {
      pePairs.push([h.weight, q.pe]);
      row.pe = Math.round(q.pe * 10000) / 10000;
    }
    if (q.pb > 0) {
      pbPairs.push([h.weight, q.pb]);
      row.pb = Math.round(q.pb * 10000) / 10000;
    }
    if (q.yield_pct > 0) {
      yPairs.push([h.weight, q.yield_pct]);
      row.yield_pct = Math.round(q.yield_pct * 10000) / 10000;
    }
    if (Object.keys(row).length > 3) used.push(row);
  }

  const pe = weightedHarmonic(pePairs);
  const pb = weightedArithmetic(pbPairs);
  const yieldPct = weightedArithmetic(yPairs);
  if (pe == null && pb == null && yieldPct == null) throw new Error("no fundamental quotes");

  let coverage = 0;
  if (yPairs.length) coverage = yPairs.reduce((s, [w]) => s + w, 0);
  else if (pePairs.length) coverage = pePairs.reduce((s, [w]) => s + w, 0);
  else if (pbPairs.length) coverage = pbPairs.reduce((s, [w]) => s + w, 0);

  const { date: today, collected_at } = beijingParts();
  const point = {
    date: today,
    pe: pe != null ? Math.round(pe * 10000) / 10000 : null,
    pb: pb != null ? Math.round(pb * 10000) / 10000 : null,
    yeild: yieldPct != null ? Math.round((yieldPct / 100) * 1e6) / 1e6 : null,
    yield_pct: yieldPct != null ? Math.round(yieldPct * 10000) / 10000 : null,
    coverage_pct: Math.round(coverage * 100) / 100,
    n: used.length,
    holdings_asof,
    collected_at,
  };

  const hist = persist
    ? await appendHistoryPoint(env, code, point)
    : await loadHistory(env, code);
  const points = hist.points || [];
  const peHist = points.map((p) => p.pe).filter((v) => typeof v === "number");
  const pbHist = points.map((p) => p.pb).filter((v) => typeof v === "number");
  const peP = point.pe != null ? percentileRank(peHist, point.pe) : null;
  const pbP = point.pb != null ? percentileRank(pbHist, point.pb) : null;
  const nHist = points.length;
  const weightSum =
    Math.round(holdings.reduce((s, h) => s + h.weight, 0) * 100) / 100;

  return {
    code,
    date: today,
    pe: point.pe,
    pb: point.pb,
    yeild: point.yeild,
    yield_pct: point.yield_pct,
    coverage_pct: point.coverage_pct,
    n: point.n,
    holdings_asof,
    holdings_weight_sum: weightSum,
    pe_percentile: peP != null ? Math.round(peP * 1e6) / 1e6 : null,
    pb_percentile: pbP != null ? Math.round(pbP * 1e6) / 1e6 : null,
    history_n: nHist,
    history_min: MIN_HISTORY_DAYS,
    percentile_ready: nHist >= MIN_HISTORY_DAYS,
    source: "eastmoney-full-holdings+self-history",
    note: "持仓加权近似（最近全量定期报告权重×当日行情），非标普官方指数点；分位样本不足时勿单独使用",
    holdings: used.slice(0, 20),
  };
}

async function proxyDanjuan() {
  let lastErr;
  for (const url of DANJUAN_URLS) {
    try {
      const upstream = await fetch(url, {
        headers: DANJUAN_HEADERS,
        redirect: "follow",
      });
      const body = await upstream.arrayBuffer();
      const headers = new Headers(corsHeaders());
      const ct = upstream.headers.get("Content-Type") || "application/json";
      headers.set("Content-Type", ct);
      return new Response(body, { status: upstream.status, headers });
    } catch (e) {
      lastErr = e;
    }
  }
  return json({ error: `upstream unavailable: ${lastErr}` }, 502);
}

async function proxyWhitelistedUrl(target, method) {
  let targetUrl;
  try {
    targetUrl = new URL(target);
  } catch {
    return json({ error: "invalid url" }, 400);
  }
  if (targetUrl.protocol !== "https:") return json({ error: "https only" }, 400);
  if (!ALLOWED_PROXY_HOSTS.has(targetUrl.hostname)) {
    return json({ error: "host not allowed" }, 403);
  }
  const headers = new Headers();
  if (/danjuan(?:funds|app)\.com$/i.test(targetUrl.hostname)) {
    for (const [k, v] of Object.entries(DANJUAN_HEADERS)) headers.set(k, v);
  } else {
    headers.set("User-Agent", UA);
  }
  const upstream = await fetch(targetUrl.toString(), {
    method,
    headers,
    redirect: "follow",
  });
  const out = new Headers(corsHeaders());
  const contentType = upstream.headers.get("Content-Type");
  if (contentType) out.set("Content-Type", contentType);
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

async function syncHistoryToGitHub(env, code, hist) {
  const token = env.GH_TOKEN;
  if (!token) return { synced: false, reason: "no GH_TOKEN" };

  const path = `data/etf_${code}_history.json`;
  const api = `https://api.github.com/repos/1154522127/magic/contents/${path}`;
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "magic-proxy-worker",
    "Content-Type": "application/json",
  };

  let sha;
  try {
    const cur = await fetch(api, { headers });
    if (cur.ok) {
      const j = await cur.json();
      sha = j.sha;
    } else if (cur.status !== 404) {
      return { synced: false, reason: `get ${cur.status}` };
    }
  } catch (e) {
    return { synced: false, reason: `get ${e}` };
  }

  const text = JSON.stringify(hist, null, 2) + "\n";
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  const content = btoa(bin);
  const last = (hist.points || [])[hist.points.length - 1];
  const date = last?.date || beijingParts().date;
  const n = (hist.points || []).length;
  const body = {
    message: `chore: update ${code} valuation history (${date}, n=${n})`,
    content,
    branch: "main",
  };
  if (sha) body.sha = sha;

  const put = await fetch(api, {
    method: "PUT",
    headers,
    body: JSON.stringify(body),
  });
  if (!put.ok) {
    const text = await put.text();
    return { synced: false, reason: `put ${put.status}: ${text.slice(0, 200)}` };
  }
  return { synced: true, date, n };
}

async function collectIfNeeded(env, code = DEFAULT_CODE) {
  const { date: today } = beijingParts();
  try {
    if (!(await isCnTradingDay(today))) {
      return { ok: true, skipped: "closed", date: today };
    }
  } catch (e) {
    // 东财偶发对 CF IP 超时/拒绝时，工作日夜晚仍继续采集，避免整晚 0 点
    const dow = beijingWeekday();
    if (dow >= 1 && dow <= 5) {
      console.log(
        JSON.stringify({
          warn: "trading-day check failed, assume open on weekday",
          date: today,
          error: String(e),
        }),
      );
    } else {
      return { ok: false, error: `trading-day check: ${e}` };
    }
  }

  const hist = await loadHistory(env, code);
  if ((hist.points || []).some((p) => p.date === today)) {
    const sync = await syncHistoryToGitHub(env, code, hist);
    return {
      ok: true,
      skipped: "already",
      date: today,
      history_n: hist.points.length,
      github: sync,
    };
  }

  try {
    const data = await computeEtfFundamentals(env, code, true);
    const saved = await loadHistory(env, code);
    const sync = await syncHistoryToGitHub(env, code, saved);
    return {
      ok: true,
      collected: true,
      date: data.date,
      pe: data.pe,
      pb: data.pb,
      yield_pct: data.yield_pct,
      history_n: data.history_n,
      github: sync,
    };
  } catch (e) {
    return { ok: false, error: String(e), date: today };
  }
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ error: "method not allowed" }, 405);
    }

    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    const qs = url.searchParams;

    // 兼容旧版：/?url=https://...
    const legacyUrl = qs.get("url");
    if (legacyUrl && (path === "/" || path === "")) {
      return proxyWhitelistedUrl(legacyUrl, request.method);
    }

    if (path === "/" || path === "/health") {
      return json({
        ok: true,
        service: "magic-proxy",
        endpoints: [
          "/valuation",
          "/etf_fundamentals?code=515450",
          "/etf_history?code=515450",
        ],
        crons_beijing: ["22:08", "22:38", "23:08", "23:38"],
      });
    }

    if (path === "/valuation") {
      return proxyDanjuan();
    }

    if (path === "/etf_history") {
      const code = (qs.get("code") || DEFAULT_CODE).trim();
      if (!/^\d{6}$/.test(code)) return json({ error: "invalid code" }, 400);
      const hist = await loadHistory(env, code);
      hist.history_n = (hist.points || []).length;
      hist.history_min = MIN_HISTORY_DAYS;
      hist.percentile_ready = hist.history_n >= MIN_HISTORY_DAYS;
      return json(hist);
    }

    if (path === "/etf_fundamentals" || path === "/etf_yield") {
      const code = (qs.get("code") || DEFAULT_CODE).trim();
      if (!/^\d{6}$/.test(code)) return json({ error: "invalid code" }, 400);
      try {
        // HTTP 只读；落盘仅由 scheduled cron 触发
        const full = await computeEtfFundamentals(env, code, false);
        if (path === "/etf_yield") {
          return json({
            code: full.code,
            yeild: full.yeild,
            yield_pct: full.yield_pct,
            coverage_pct: full.coverage_pct,
            n: full.n,
            source: full.source,
            holdings: full.holdings || [],
            pe: full.pe,
            pb: full.pb,
            pe_percentile: full.pe_percentile,
            pb_percentile: full.pb_percentile,
            history_n: full.history_n,
            history_min: full.history_min,
            percentile_ready: full.percentile_ready,
          });
        }
        return json(full);
      } catch (e) {
        return json({ error: String(e) }, 502);
      }
    }

    return json({ error: "not found" }, 404);
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      (async () => {
        const result = await collectIfNeeded(env, DEFAULT_CODE);
        console.log(JSON.stringify({ cron: event.cron, ...result }));
        if (!result.ok) throw new Error(result.error || "collect failed");
      })(),
    );
  },
};
