// Near-live market digest. A free Cloudflare Worker that rebuilds the page's
// lists from fresh Alpaca prices whenever the page asks (at most once a minute).
// digest.py still runs every 15 minutes and publishes baseline.json, which holds
// each stock's normal move and average volume, so this only fetches today's prices
// and headlines. The list rules mirror digest.py; keep the two in step.
//
// A cron trigger (wrangler.toml) also starts the Market digest GitHub workflow every 5
// minutes, because GitHub's own scheduler is unreliable. It needs the secret DISPATCH_TOKEN
// (fine-grained token, Actions read and write on this repo); digest.py exits by itself
// outside trading hours.
//
// Secrets: ALPACA_KEY_ID, ALPACA_SECRET_KEY, DISPATCH_TOKEN. Var: BASELINE_URL (see wrangler.toml).

const DATA_URL = "https://data.alpaca.markets";
const CACHE_MS = 55_000;

let cached = null;   // { at, body } shared by everyone who opens the page
let building = null;

export default {
  async scheduled(event, env) {
    if (!env.DISPATCH_TOKEN) return;
    const res = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/digest.yml/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "market-digest-live",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    if (!res.ok) console.log("workflow dispatch failed", res.status, await res.text());
  },

  async fetch(request, env) {
    if (request.method === "OPTIONS") return reply(null, 204);
    try {
      if (!cached || Date.now() - cached.at > CACHE_MS) {
        building = building || build(env).finally(() => { building = null; });
        cached = { at: Date.now(), body: JSON.stringify(await building) };
      }
      return reply(cached.body, 200);
    } catch (err) {
      return reply(JSON.stringify({ live: false, error: String(err && err.message || err) }), 502);
    }
  },
};

function reply(body, status) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": "no-store",
    },
  });
}

async function alpaca(env, path, params) {
  const url = DATA_URL + path + "?" + new URLSearchParams(params);
  const res = await fetch(url, {
    headers: { "APCA-API-KEY-ID": env.ALPACA_KEY_ID, "APCA-API-SECRET-KEY": env.ALPACA_SECRET_KEY },
  });
  if (!res.ok) throw new Error(`Alpaca ${path} ${res.status}`);
  return res.json();
}

function chunks(arr, n) {
  const out = [];
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
  return out;
}

// Current New York time as plain fields.
function nowET() {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date()).map((p) => [p.type, p.value]));
  return { date: `${parts.year}-${parts.month}-${parts.day}`, minutes: +parts.hour * 60 + +parts.minute };
}

function sessionFraction(minutes) {
  const elapsed = (minutes - 570) / 390;
  return Math.pow(Math.min(1, Math.max(0.03, elapsed)), 0.7);
}

const round = (x, d) => Math.round(x * 10 ** d) / 10 ** d;

function fadeRead(price, high, low, prev) {
  if (!(high > low && prev > 0)) return null;
  const peakUp = (high - prev) / prev, peakDn = (low - prev) / prev, now = (price - prev) / prev;
  const posn = (price - low) / (high - low);
  const pct = (x) => (x >= 0 ? "+" : "") + (x * 100).toFixed(1) + "%";
  if (peakUp >= 0.03 && high > prev && (high - price) / (high - prev) >= 0.4) {
    const gave = Math.min(Math.round((high - price) / (high - prev) * 100), 100);
    return { kind: "fade", title: "Spike and fade", text: `Peaked ${pct(peakUp)}, now ${pct(now)}. Gave back ${gave}% of the gain.` };
  }
  if (peakDn <= -0.03 && prev > low && (price - low) / (prev - low) >= 0.4) {
    const back = Math.min(Math.round((price - low) / (prev - low) * 100), 100);
    return { kind: "recover", title: "Dip and recover", text: `Fell as far as ${pct(peakDn)}, now ${pct(now)}. Recovered ${back}% of the drop.` };
  }
  if (posn <= 0.15 && Math.abs(now) >= 0.015) return { kind: "low", title: "Near the low", text: "Trading at the bottom of today's range." };
  if (posn >= 0.85 && Math.abs(now) >= 0.015) return { kind: "high", title: "Near the high", text: "Trading at the top of today's range." };
  return null;
}

// Multi-day context: streak (today counts, via the live price), 3-day change, 52-week range. Mirrors trend() in digest.py.
function trend(s, price, high, low, tail, range52) {
  const out = { streak: 0, chg3: null, w52: null, near52: null };
  const path = [...(tail || []), price];
  if (path.length >= 4 && path[path.length - 4] > 0) out.chg3 = round((price - path[path.length - 4]) / path[path.length - 4] * 100, 1);
  for (let i = path.length - 1; i > 0; i--) {
    const step = path[i] > path[i - 1] ? 1 : path[i] < path[i - 1] ? -1 : 0;
    if (!step || (out.streak && step !== Math.sign(out.streak))) break;
    out.streak += step;
  }
  if (range52) {
    const hi = Math.max(range52[0], high), lo = Math.min(range52[1], low); // baseline stores [high, low]
    if (hi > lo) {
      out.w52 = [round(lo, 4), round(hi, 4)];
      if ((hi - price) / hi * 100 <= s.near_52w_pct) out.near52 = "high";
      else if ((price - lo) / lo * 100 <= s.near_52w_pct) out.near52 = "low";
    }
  }
  return out;
}

const rankScore = (r, s) => r.ratio * (r.near52 || Math.abs(r.streak || 0) >= s.streak_min ? 1.25 : 1);

async function fetchNews(env, symbols, perSymbol) {
  const since = new Date(Date.now() - 36 * 3600_000).toISOString().replace(/\.\d+Z$/, "Z");
  const wanted = new Set(symbols);
  const found = Object.fromEntries(symbols.map((s) => [s, new Map()]));
  const pages = await Promise.all(chunks(symbols, 10).map((part) =>
    alpaca(env, "/v1beta1/news", { symbols: part.join(","), start: since, limit: 50, sort: "desc" })
      .catch(() => ({ news: [] }))));
  for (const res of pages) {
    for (const a of res.news || []) {
      const tagged = a.symbols || [];
      const item = { headline: a.headline || "", url: a.url || "", source: a.source || "",
        time: a.created_at || "", summary: (a.summary || "").slice(0, 280), focus: tagged.length };
      for (const s of tagged) if (wanted.has(s) && item.url && !found[s].has(item.url)) found[s].set(item.url, item);
    }
  }
  const out = {};
  for (const [s, items] of Object.entries(found)) {
    const ranked = [...items.values()].sort((a, b) => (b.time > a.time ? 1 : b.time < a.time ? -1 : 0));
    ranked.sort((a, b) => (a.focus > 3) - (b.focus > 3)); // focused first, newest within each
    out[s] = ranked.slice(0, perSymbol);
  }
  return out;
}

async function build(env) {
  const base = await fetch(env.BASELINE_URL, { cf: { cacheTtl: 300 } }).then((r) => {
    if (!r.ok) throw new Error(`baseline ${r.status}`);
    return r.json();
  });
  const s = base.settings;
  const symbols = Object.keys(base.symbols);
  const { date: today, minutes } = nowET();

  const snaps = Object.assign({}, ...await Promise.all(chunks(symbols, 100).map((part) =>
    alpaca(env, "/v2/stocks/snapshots", { symbols: part.join(","), feed: "iex" }))));

  const frac = sessionFraction(minutes);
  const rows = [];
  for (const [sym, snap] of Object.entries(snaps)) {
    const day = snap && snap.dailyBar, prevBar = snap && snap.prevDailyBar;
    if (!day || !prevBar || day.t.slice(0, 10) !== today || !prevBar.c) continue;
    const price = (snap.latestTrade && snap.latestTrade.p) || day.c;
    const prev = prevBar.c;
    const [name, group, normal, avgVol, range52, tail] = base.symbols[sym] || ["", "", null, null, null, []];
    const pct = (price - prev) / prev * 100;
    // After 4pm: move since today's regular close (IEX daily bars stop at the close).
    const ah = minutes >= 960 && day.c && snap.latestTrade && snap.latestTrade.p !== day.c
      ? round((price - day.c) / day.c * 100, 2) : null;
    rows.push({
      symbol: sym, name, group, price: round(price, 4), prev, open: day.o, high: day.h, low: day.l,
      pct: round(pct, 2), normal: normal ? round(normal, 2) : null,
      ratio: normal ? round(Math.abs(pct) / normal, 1) : null,
      volume: day.v, rvol: avgVol ? round(day.v / (avgVol * frac), 1) : null,
      read: fadeRead(price, day.h, day.l, prev),
      earn: (base.earnings || {})[sym] || null, ah,
      ...trend(s, price, day.h, day.l, tail, range52),
    });
  }
  // Before the first trade of the day, the page keeps showing the last 15-minute digest.
  if (!rows.length) return { live: false };

  const unusual = rows.filter((r) => r.ratio && r.ratio >= s.unusual_ratio && Math.abs(r.pct) >= s.unusual_min_pct)
    .sort((a, b) => rankScore(b, s) - rankScore(a, s)).slice(0, s.list_size);
  const swingers = rows.filter((r) => r.normal && r.normal >= s.swinger_normal)
    .sort((a, b) => Math.abs(b.pct) - Math.abs(a.pct)).slice(0, s.list_size);
  for (const r of swingers) r.usual = r.ratio >= s.bigger_than_usual ? "Bigger than usual" : "Normal day for it";
  const bySym = Object.fromEntries(rows.map((r) => [r.symbol, r]));
  const market = base.etfs.filter((e) => bySym[e]).map((e) => bySym[e]);

  const listed = [...new Set([...unusual, ...swingers].map((r) => r.symbol))].sort();
  const news = await fetchNews(env, listed, s.news_per_symbol);
  for (const r of [...unusual, ...swingers]) r.news = news[r.symbol] || [];

  const up = rows.filter((r) => r.pct > 0).length;
  return {
    live: true, generated: new Date().toISOString(), slot: minutes >= 1020 ? "Close" : minutes >= 960 ? "After hours" : "Intraday", demo: false,
    breadth: { up, down: rows.length - up, total: rows.length },
    market, unusual, swingers, settings: s,
    // Every scanned symbol, for the page's watchlist: [name, price, pct, ratio, rvol, high, low, w52, streak, chg3, near52]
    quotes: Object.fromEntries(rows.map((r) => [r.symbol, [r.name, r.price, r.pct, r.ratio, r.rvol, r.high, r.low, r.w52, r.streak, r.chg3, r.near52]])),
  };
}
