"""Market digest: scans the S&P 500 plus a list of ETFs with Alpaca's free
market data, finds unusual moves and big swingers, attaches headlines, and
writes docs/data.json for the web page in docs/index.html.

Runs every 15 minutes in GitHub Actions while the market is open. Needs two environment variables:
ALPACA_KEY_ID and ALPACA_SECRET_KEY (paper account keys are fine).

    python digest.py            # real run (only during market hours)
    python digest.py --force    # run even if the market is closed
    python digest.py --demo     # fake data, no keys needed, for previewing the page
"""

import csv
import json
import os
import random
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "docs", "data.json")
PREMARKET_OUT = os.path.join(ROOT, "docs", "premarket.json")
# Per-symbol normals for the near-live worker in worker/, which only fetches today's prices.
BASELINE_OUT = os.path.join(ROOT, "docs", "baseline.json")
DATA_URL = "https://data.alpaca.markets"
TRADING_URL = "https://paper-api.alpaca.markets"
ET = ZoneInfo("America/New_York")

# Tuning. A stock's "normal move" is its average absolute daily % change over
# the last LOOKBACK trading days.
LOOKBACK = 20
UNUSUAL_RATIO = 2.5      # today's move must be this many times its normal move
UNUSUAL_MIN_PCT = 1.5    # ...and at least this big in absolute %
SWINGER_NORMAL = 2.5     # a stock whose normal move is at least this % is a "swinger"
BIGGER_THAN_USUAL = 1.8  # swinger tag threshold
LIST_SIZE = 25
NEWS_PER_SYMBOL = 3
PREMARKET_START_MIN = 150  # pre-market scans begin this many minutes before the open (7:00am ET)
GAP_MIN_PCT = 1.0          # pre-market gap lists need at least this % vs yesterday's close...
GAP_MIN_RATIO = 1.5        # ...and this many times the stock's normal daily move
THIN_SHARES = 2000         # under this many IEX shares traded pre-market, flag the price as thin


# ---------- universe ----------

def load_universe():
    names, groups = {}, {}
    with open(os.path.join(ROOT, "data", "sp500.csv"), newline="") as f:
        for r in csv.DictReader(f):
            s = r["Symbol"].strip().upper()
            names[s] = r["Security"].strip()
            groups[s] = r["GICS Sector"].strip()
    etfs = []
    with open(os.path.join(ROOT, "data", "etfs.csv"), newline="") as f:
        for r in csv.DictReader(f):
            s = r["symbol"].strip().upper()
            names[s] = r["name"].strip()
            groups[s] = "ETF: " + r["group"].strip()
            etfs.append(s)
    return names, groups, etfs


# ---------- alpaca ----------

def get(base, path, params):
    url = base + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": os.environ["ALPACA_KEY_ID"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"],
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def market_hours(today):
    """Today's (open, close) as ET datetimes, or None on a market holiday."""
    days = get(TRADING_URL, "/v2/calendar", {"start": today, "end": today})
    if not days:
        return None
    d = days[0]
    at = lambda hm: datetime.fromisoformat(f"{d['date']}T{hm}").replace(tzinfo=ET)
    return at(d["open"]), at(d["close"])


def fetch_snapshots(symbols):
    out = {}
    for part in chunks(symbols, 100):
        res = get(DATA_URL, "/v2/stocks/snapshots", {"symbols": ",".join(part), "feed": "iex"})
        out.update(res)
    return out


def fetch_daily_history(symbols, today):
    """Recent daily closes and volumes per symbol, oldest first, excluding today."""
    start = (datetime.fromisoformat(today) - timedelta(days=45)).date().isoformat()
    closes, volumes = {}, {}
    for part in chunks(symbols, 100):
        token = None
        while True:
            params = {"symbols": ",".join(part), "timeframe": "1Day", "start": start,
                      "end": today, "feed": "iex", "adjustment": "split", "limit": 10000}
            if token:
                params["page_token"] = token
            res = get(DATA_URL, "/v2/stocks/bars", params)
            for sym, bars in (res.get("bars") or {}).items():
                for b in bars:
                    if b["t"][:10] < today:
                        closes.setdefault(sym, []).append(b["c"])
                        volumes.setdefault(sym, []).append(b["v"])
            token = res.get("next_page_token")
            if not token:
                break
    return closes, volumes


def fetch_news(symbols):
    """Latest headlines per symbol. Articles about one or two companies rank
    ahead of market roundups that tag dozens of tickers."""
    since = (datetime.now(timezone.utc) - timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
    wanted = set(symbols)
    found = {s: {} for s in symbols}
    for part in chunks(list(symbols), 10):
        res = get(DATA_URL, "/v1beta1/news", {"symbols": ",".join(part), "start": since,
                                             "limit": 50, "sort": "desc"})
        for a in res.get("news", []):
            tagged = a.get("symbols", [])
            item = {"headline": a.get("headline", ""), "url": a.get("url", ""),
                    "source": a.get("source", ""), "time": a.get("created_at", ""),
                    "summary": (a.get("summary") or "")[:280], "focus": len(tagged)}
            for s in tagged:
                if s in wanted and item["url"]:
                    found[s].setdefault(item["url"], item)
    by_sym = {}
    for s, items in found.items():
        ranked = sorted(items.values(), key=lambda x: x["time"], reverse=True)
        ranked.sort(key=lambda x: x["focus"] > 3)  # stable: focused first, newest within each
        by_sym[s] = ranked[:NEWS_PER_SYMBOL]
    return by_sym


# ---------- analysis ----------

def normal_move(closes):
    c = closes[-(LOOKBACK + 1):]
    moves = [abs(b - a) / a * 100 for a, b in zip(c, c[1:]) if a > 0]
    return statistics.mean(moves) if len(moves) >= 5 else None


def avg_volume(volumes):
    v = volumes[-LOOKBACK:]
    return statistics.mean(v) if len(v) >= 5 else None


def session_fraction(now_et):
    """Roughly how much of a normal day's volume has traded by now. Volume is heavy
    near the open, so this rises faster than the clock (about 17% after 30 minutes,
    60% by midday)."""
    open_ = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed = (now_et - open_).total_seconds() / 60 / 390
    return min(1.0, max(0.03, elapsed)) ** 0.7


def rel_volume(volume, avg, frac):
    """Today's volume against what a normal day would have traded by now."""
    return round(volume / (avg * frac), 1) if avg else None


def fade_read(price, high, low, prev):
    """Same rules as the original Outlier Board."""
    if not (high > low and prev > 0):
        return None
    peak_up = (high - prev) / prev
    peak_dn = (low - prev) / prev
    now = (price - prev) / prev
    posn = (price - low) / (high - low)
    pct = lambda x: f"{x * 100:+.1f}%"
    if peak_up >= 0.03 and high > prev and (high - price) / (high - prev) >= 0.4:
        gave = min(round((high - price) / (high - prev) * 100), 100)
        return {"kind": "fade", "title": "Spike and fade",
                "text": f"Peaked {pct(peak_up)}, now {pct(now)}. Gave back {gave}% of the gain."}
    if peak_dn <= -0.03 and prev > low and (price - low) / (prev - low) >= 0.4:
        back = min(round((price - low) / (prev - low) * 100), 100)
        return {"kind": "recover", "title": "Dip and recover",
                "text": f"Fell as far as {pct(peak_dn)}, now {pct(now)}. Recovered {back}% of the drop."}
    if posn <= 0.15 and abs(now) >= 0.015:
        return {"kind": "low", "title": "Near the low", "text": "Trading at the bottom of today's range."}
    if posn >= 0.85 and abs(now) >= 0.015:
        return {"kind": "high", "title": "Near the high", "text": "Trading at the top of today's range."}
    return None


def build_rows(snaps, closes, volumes, names, groups, today, frac):
    rows = []
    for sym, s in snaps.items():
        try:
            day, prevbar = s["dailyBar"], s["prevDailyBar"]
            price = (s.get("latestTrade") or {}).get("p") or day["c"]
        except (KeyError, TypeError):
            continue
        if day["t"][:10] != today:  # no trades yet today
            continue
        prev = prevbar["c"]
        if not prev:
            continue
        pct = (price - prev) / prev * 100
        nm = normal_move(closes.get(sym, []))
        av = avg_volume(volumes.get(sym, []))
        rows.append({
            "symbol": sym, "name": names.get(sym, ""), "group": groups.get(sym, ""),
            "price": round(price, 4), "prev": prev, "open": day["o"], "high": day["h"], "low": day["l"],
            "pct": round(pct, 2), "normal": round(nm, 2) if nm else None,
            "ratio": round(abs(pct) / nm, 1) if nm else None,
            "volume": day["v"], "rvol": rel_volume(day["v"], av, frac),
            "read": fade_read(price, day["h"], day["l"], prev),
        })
    return rows


def pick_lists(rows, etfs):
    unusual = [r for r in rows if r["ratio"] and r["ratio"] >= UNUSUAL_RATIO and abs(r["pct"]) >= UNUSUAL_MIN_PCT]
    unusual.sort(key=lambda r: -r["ratio"])
    swingers = [r for r in rows if r["normal"] and r["normal"] >= SWINGER_NORMAL]
    swingers.sort(key=lambda r: -abs(r["pct"]))
    for r in swingers:
        r["usual"] = "Bigger than usual" if r["ratio"] >= BIGGER_THAN_USUAL else "Normal day for it"
    by = {r["symbol"]: r for r in rows}
    market = [by[s] for s in etfs if s in by]
    return unusual[:LIST_SIZE], swingers[:LIST_SIZE], market


def slot_label(now_et, close=None):
    if close is None:
        close = now_et.replace(hour=16, minute=0)
    return "Close" if now_et >= close else "Intraday"


# ---------- pre-market gaps ----------

def fetch_premarket_bars(symbols, start_et, today):
    """Pre-market volume, high and low per symbol from 1-minute IEX bars since 4am ET."""
    out = {}
    start = start_et.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for part in chunks(symbols, 50):
        token = None
        while True:
            params = {"symbols": ",".join(part), "timeframe": "1Min", "start": start, "feed": "iex",
                      "limit": 10000}
            if token:
                params["page_token"] = token
            res = get(DATA_URL, "/v2/stocks/bars", params)
            for sym, bars in (res.get("bars") or {}).items():
                for b in bars:
                    o = out.setdefault(sym, {"volume": 0, "high": b["h"], "low": b["l"]})
                    o["volume"] += b["v"]
                    o["high"], o["low"] = max(o["high"], b["h"]), min(o["low"], b["l"])
            token = res.get("next_page_token")
            if not token:
                break
    return out


def build_gaps(snaps, closes, names, groups, open_et):
    """Names whose latest pre-market trade is well away from yesterday's close."""
    today = open_et.date().isoformat()
    rows = []
    for sym, s in snaps.items():
        try:
            lt = s["latestTrade"]
            when = datetime.fromisoformat(lt["t"].replace("Z", "+00:00")).astimezone(ET)
            # Before the open the daily bar is usually yesterday's; use whichever bar is not today's.
            bar = s["dailyBar"] if s["dailyBar"]["t"][:10] < today else s["prevDailyBar"]
            prior = bar["c"]
        except (KeyError, TypeError, ValueError):
            continue
        if when.date() != open_et.date() or when >= open_et or not prior:
            continue  # no pre-market trade yet today
        nm = normal_move(closes.get(sym, []))
        gap = (lt["p"] - prior) / prior * 100
        ratio = abs(gap) / nm if nm else None
        if abs(gap) < GAP_MIN_PCT or not ratio or ratio < GAP_MIN_RATIO:
            continue
        rows.append({"symbol": sym, "name": names.get(sym, ""), "group": groups.get(sym, ""),
                     "price": round(lt["p"], 4), "prev": prior, "pct": round(gap, 2),
                     "normal": round(nm, 2), "ratio": round(ratio, 1), "last_trade": when.isoformat(timespec="minutes")})
    rows.sort(key=lambda r: -r["ratio"])
    return rows[:LIST_SIZE]


def run_premarket(symbols, names, groups, now_et, open_et):
    snaps = fetch_snapshots(symbols)
    closes, _ = fetch_daily_history(symbols, open_et.date().isoformat())
    gaps = build_gaps(snaps, closes, names, groups, open_et)
    pm = fetch_premarket_bars([r["symbol"] for r in gaps], now_et.replace(hour=4, minute=0, second=0, microsecond=0),
                              open_et.date().isoformat()) if gaps else {}
    news = fetch_news([r["symbol"] for r in gaps]) if gaps else {}
    for r in gaps:
        b = pm.get(r["symbol"], {})
        r["pm_volume"], r["pm_high"], r["pm_low"] = b.get("volume", 0), b.get("high"), b.get("low")
        r["thin"] = r["pm_volume"] < THIN_SHARES
        r["news"] = news.get(r["symbol"], [])
    payload = {"generated": now_et.isoformat(timespec="minutes"), "date": open_et.date().isoformat(),
               "open": open_et.isoformat(timespec="minutes"),
               "settings": {"min_pct": GAP_MIN_PCT, "min_ratio": GAP_MIN_RATIO, "thin_shares": THIN_SHARES},
               "gaps": gaps, "scanned": len(snaps)}
    with open(PREMARKET_OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"Wrote {PREMARKET_OUT}: {len(gaps)} gaps from {len(snaps)} snapshots.")


# ---------- demo data ----------

def demo_rows(names, groups):
    rnd = random.Random(7)
    rows = []
    for sym in names:
        normal = rnd.choice([0.7, 0.9, 1.1, 1.4, 1.8, 2.6, 3.4, 4.5])
        pct = rnd.gauss(0, normal)
        if rnd.random() < 0.02:
            pct *= 4
        prev = round(rnd.uniform(20, 600), 2)
        price = prev * (1 + pct / 100)
        hi = max(price, prev) * (1 + abs(rnd.gauss(0, normal / 150)))
        lo = min(price, prev) * (1 - abs(rnd.gauss(0, normal / 150)))
        if rnd.random() < 0.15:  # simulate a morning spike that faded
            hi = prev * (1 + abs(pct) / 100 * 2.2)
        rows.append({"symbol": sym, "name": names[sym], "group": groups[sym], "price": round(price, 2),
                     "prev": prev, "open": prev, "high": round(hi, 2), "low": round(lo, 2),
                     "pct": round(pct, 2), "normal": normal, "ratio": round(abs(pct) / normal, 1),
                     "volume": rnd.randint(200_000, 9_000_000), "rvol": round(rnd.lognormvariate(0.1, 0.6), 1),
                     "read": fade_read(price, hi, lo, prev)})
    return rows


def demo_news(symbols):
    return {s: [{"headline": f"Why {s} shares are moving today", "url": "https://www.benzinga.com/",
                 "source": "benzinga", "time": datetime.now(timezone.utc).isoformat(), "summary": ""}]
            for s in symbols}


# ---------- output ----------

def settings():
    return {"lookback": LOOKBACK, "unusual_ratio": UNUSUAL_RATIO, "unusual_min_pct": UNUSUAL_MIN_PCT,
            "swinger_normal": SWINGER_NORMAL, "bigger_than_usual": BIGGER_THAN_USUAL,
            "list_size": LIST_SIZE, "news_per_symbol": NEWS_PER_SYMBOL}


def write_baseline(names, groups, etfs, closes, volumes, today):
    """What the near-live worker needs to rebuild the lists from fresh prices:
    each symbol's name, group, normal move and average volume."""
    syms = {}
    for s in sorted(names):
        nm, av = normal_move(closes.get(s, [])), avg_volume(volumes.get(s, []))
        syms[s] = [names[s], groups[s], round(nm, 3) if nm else None, round(av) if av else None]
    with open(BASELINE_OUT, "w") as f:
        json.dump({"date": today, "settings": settings(), "etfs": etfs, "symbols": syms}, f,
                  separators=(",", ":"))


# ---------- main ----------

def main():
    demo, force = "--demo" in sys.argv, "--force" in sys.argv
    names, groups, etfs = load_universe()
    symbols = sorted(names)
    now_et = datetime.now(ET)
    today = now_et.date().isoformat()

    close, closes, volumes = None, {}, {}
    if demo:
        rows = demo_rows(names, groups)
    else:
        hours = market_hours(today)
        close = hours[1] if hours else None
        pre_window = hours and hours[0] - timedelta(minutes=PREMARKET_START_MIN) <= now_et < hours[0]
        if pre_window or (force and hours and now_et < hours[0]):
            run_premarket(symbols, names, groups, now_et, hours[0])
            return
        # Runs every 15 minutes; only scan from the open until shortly after the close.
        if not force and not (hours and hours[0] <= now_et <= hours[1] + timedelta(minutes=20)):
            print("Market not open; leaving the page as it is.")
            return
        snaps = fetch_snapshots(symbols)
        closes, volumes = fetch_daily_history(symbols, today)
        write_baseline(names, groups, etfs, closes, volumes, today)
        rows = build_rows(snaps, closes, volumes, names, groups, today, session_fraction(now_et))
        if not rows:
            print("No trades yet today; leaving the page as it is.")
            return

    unusual, swingers, market = pick_lists(rows, etfs)
    listed = sorted({r["symbol"] for r in unusual + swingers})
    news = demo_news(listed) if demo else fetch_news(listed)
    for r in unusual + swingers:
        r["news"] = news.get(r["symbol"], [])

    up = sum(1 for r in rows if r["pct"] > 0)
    payload = {
        "generated": now_et.isoformat(timespec="minutes"),
        "slot": slot_label(now_et, close), "demo": demo,
        "breadth": {"up": up, "down": len(rows) - up, "total": len(rows)},
        "market": market, "unusual": unusual, "swingers": swingers,
        "settings": settings(),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"Wrote {OUT}: {len(unusual)} unusual, {len(swingers)} swingers, {len(rows)} scanned.")


if __name__ == "__main__":
    main()
