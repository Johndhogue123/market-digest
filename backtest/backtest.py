"""Replays the paper-trading rules on past data. Standalone: reads Alpaca history, writes only under
backtest/out/. Nothing here is imported by digest.py or the worker, and nothing is written to docs/.

    python backtest/backtest.py --years 2 --rate 60     # real run (needs ALPACA_KEY_ID / ALPACA_SECRET_KEY)
    python -m unittest discover -s tests                # unit tests need no keys

Rules replayed (same as the live log): a stock is "unusual" at a 30-minute bar close when it is at least
UNUSUAL_MIN_PCT away from yesterday's close and at least UNUSUAL_RATIO times its normal daily move. The first
such bar of a day is the entry (no entries in the last half hour). Momentum goes with the move, Fade goes against
it. Exits: stop, target, or the 4pm close. If a stop and a target fall in the same bar, the stop is assumed first.
"""

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import digest as d  # noqa: E402  (reuses the live rules: normal_move, avg_volume, session_fraction)

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
PRIMARY = (3.0, 6.0)                      # the live strategy's (stop %, target %), the one pre-declared test
STOPS = [2.0, 3.0, 4.0, 5.0]
TARGETS = [3.0, 4.0, 6.0, 8.0, 10.0]
COST_PCT = 0.10                           # assumed round-trip cost per trade, in %
HOLDOUT_DAYS = 182                        # the most recent ~6 months are kept out of any tuning
MIN_BARS = 10                             # regular-session 30-minute bars needed for a usable day
LAST_ENTRY_START_MIN = 15 * 60            # a bar starting 15:00 ET (closing 15:30) is the last entry bar


class Throttle:
    """At most `rate` calls a minute, with backoff when Alpaca says slow down. The digest and the live worker
    share these keys and the 200/min limit, so stay far below it."""

    def __init__(self, rate):
        self.gap, self.last, self.calls = 60.0 / rate, 0.0, 0

    def get(self, path, params):
        for attempt in range(6):
            wait = self.last + self.gap - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.calls += 1
            try:
                return d.get(d.DATA_URL, path, params)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504):
                    raise
                time.sleep(min(60, 5 * 2 ** attempt))
        raise RuntimeError(f"Alpaca kept refusing {path}")


def fetch_bars(th, symbols, timeframe, start, end, size):
    """{symbol: [bar, ...]} oldest first, paging through every result."""
    out = defaultdict(list)
    for i in range(0, len(symbols), size):
        token = None
        while True:
            params = {"symbols": ",".join(symbols[i:i + size]), "timeframe": timeframe, "start": start, "end": end,
                      "feed": "iex", "adjustment": "split", "limit": 10000}
            if token:
                params["page_token"] = token
            res = th.get("/v2/stocks/bars", params)
            for sym, bars in (res.get("bars") or {}).items():
                out[sym].extend(bars)
            token = res.get("next_page_token")
            if not token:
                break
        print(f"  fetched {timeframe} bars for {min(i + size, len(symbols))}/{len(symbols)} symbols ({th.calls} calls)", flush=True)
    return out


# ---------- the simulation (pure, unit-tested) ----------

def exit_pct(side, entry, bars, stop, target):
    """Percent result of one trade entered at `entry` and held through `bars` (each {h, l, c}), closed at the
    last close. stop/target are % distances (None = no limit). A bar touching both exits at the stop."""
    sgn = 1 if side == "long" else -1
    stop_px = entry * (1 - sgn * stop / 100) if stop else None
    target_px = entry * (1 + sgn * target / 100) if target else None
    for b in bars:
        hit_stop = stop_px is not None and (b["l"] <= stop_px if sgn == 1 else b["h"] >= stop_px)
        hit_target = target_px is not None and (b["h"] >= target_px if sgn == 1 else b["l"] <= target_px)
        if hit_stop:
            return -stop
        if hit_target:
            return target
    last = bars[-1]["c"] if bars else entry
    return sgn * (last - entry) / entry * 100


def find_trigger(bars, prev, nm, avg_vol):
    """The first scan bar of the day that would have made the stock 'unusual', or None.
    bars: regular-session bars [{t (ET datetime), o, h, l, c, v}]. Returns (index, pct, ratio, rvol)."""
    cum = 0
    for k, b in enumerate(bars):
        cum += b["v"]
        if b["t"].hour * 60 + b["t"].minute > LAST_ENTRY_START_MIN:
            return None
        pct = (b["c"] - prev) / prev * 100
        if nm and abs(pct) >= d.UNUSUAL_MIN_PCT and abs(pct) / nm >= d.UNUSUAL_RATIO:
            end = b["t"] + timedelta(minutes=30)
            rvol = d.rel_volume(cum, avg_vol, d.session_fraction(end))
            return k, pct, round(abs(pct) / nm, 1), rvol
    return None


def regular_session(bars):
    out = []
    for b in bars:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(d.ET)
        if (9, 30) <= (t.hour, t.minute) <= (15, 30):
            out.append({"t": t, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
    return out


def trades_for_symbol(sym, daily, intraday, spy_by_day=None):
    """Every triggered symbol-day for one stock with its result under each (stop, target) pair, long and short."""
    by_day = defaultdict(list)
    for b in intraday:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(d.ET)
        by_day[t.date().isoformat()].append(b)
    closes = [x["c"] for x in daily]
    vols = [x["v"] for x in daily]
    days = [x["t"][:10] for x in daily]
    index = {day: i for i, day in enumerate(days)}
    out = []
    for day, raw in by_day.items():
        i = index.get(day)
        if not i:
            continue
        bars = regular_session(raw)
        if len(bars) < MIN_BARS or bars[0]["t"].hour * 60 + bars[0]["t"].minute != 9 * 60 + 30:
            continue
        # Only days before this one feed the normal move and average volume, so nothing looks ahead.
        nm = d.normal_move(closes[max(0, i - d.LONG_LOOKBACK - 1):i])
        trig = find_trigger(bars, closes[i - 1], nm, d.avg_volume(vols[max(0, i - d.LOOKBACK):i]))
        if not trig:
            continue
        k, pct, ratio, rvol = trig
        rest = bars[k + 1:]
        if not rest:
            continue
        entry = bars[k]["c"]
        res = {side: {f"{s:g}/{t:g}": exit_pct(side, entry, rest, s, t) for s in STOPS for t in TARGETS}
               | {"none": exit_pct(side, entry, rest, None, None)} for side in ("long", "short")}
        out.append({"sym": sym, "date": day, "pct": round(pct, 2), "ratio": ratio, "rvol": rvol,
                    "hour": bars[k]["t"].hour + bars[k]["t"].minute / 60, "up": pct > 0, "res": res,
                    "spy": (spy_by_day or {}).get(day, {}).get(bars[k]["t"].strftime("%H:%M"))})
    return out


# ---------- statistics ----------

def stats(vals):
    n = len(vals)
    if n < 2:
        return {"n": n}
    m, sd = statistics.mean(vals), statistics.stdev(vals)
    return {"n": n, "win": round(sum(v > 0 for v in vals) / n * 100, 1), "mean": round(m, 3),
            "ci": round(1.96 * sd / math.sqrt(n), 3), "median": round(statistics.median(vals), 3)}


def pick(rows, strat, combo, cost=COST_PCT):
    """(trade row, net % result) for a strategy: momentum goes with the move, fade against it, long_only buys."""
    out = []
    for r in rows:
        up = r["up"]
        if strat == "momentum":
            side = "long" if up else "short"
        elif strat == "fade":
            side = "short" if up else "long"
        else:
            continue
        out.append((r, r["res"][side][combo] - cost))
    return out


def long_only(rows, combo, cost=COST_PCT):
    """Every buy either strategy would make: buy the riser (Momentum) and buy the faller (Fade)."""
    return [(r, r["res"]["long"][combo] - cost) for r in rows]


def bucket(rows_vals, key, edges, labels):
    groups = defaultdict(list)
    for r, v in rows_vals:
        x = key(r)
        if x is None:
            groups["n/a"].append(v)
            continue
        for e, lab in zip(edges, labels):
            if x < e:
                groups[lab].append(v)
                break
        else:
            groups[labels[-1]].append(v)
    return {lab: stats(groups[lab]) for lab in labels + ["n/a"] if groups.get(lab)}


def fmt(s):
    return "n=0" if not s.get("n") else (f"n={s['n']}" if s["n"] < 2 else
                                         f"n={s['n']:>5} win={s['win']:>5.1f}% mean={s['mean']:+.3f}% (±{s['ci']:.2f}) median={s['median']:+.2f}%")


def analyse(rows, today):
    cutoff = (today - timedelta(days=HOLDOUT_DAYS)).isoformat()
    train, test = [r for r in rows if r["date"] < cutoff], [r for r in rows if r["date"] >= cutoff]
    primary = f"{PRIMARY[0]:g}/{PRIMARY[1]:g}"
    rep = {"trades": len(rows), "train_trades": len(train), "test_trades": len(test), "holdout_from": cutoff,
           "cost_pct": COST_PCT, "primary": primary, "strategies": {}, "grid": {}, "features": {}, "by_quarter": {}}
    sets = {"momentum": lambda rs, c=primary: pick(rs, "momentum", c), "fade": lambda rs, c=primary: pick(rs, "fade", c),
            "long_only": lambda rs, c=primary: long_only(rs, c)}
    for name, fn in sets.items():
        rep["strategies"][name] = {
            "all": stats([v for _, v in fn(rows)]), "train": stats([v for _, v in fn(train)]),
            "holdout": stats([v for _, v in fn(test)]),
            "per_day_all": daily_avg_stats_pairs(fn(rows))}
        quarters = defaultdict(list)
        for r, v in fn(rows):
            quarters[r["date"][:4] + "Q" + str((int(r["date"][5:7]) - 1) // 3 + 1)].append(v)
        rep["by_quarter"][name] = {q: stats(v) for q, v in sorted(quarters.items())}
    # Limit grid: look at the training period only; the holdout column is for checking a pick, not choosing one.
    for name in ("momentum", "fade", "long_only"):
        g = {}
        for s in STOPS:
            for t in TARGETS:
                c = f"{s:g}/{t:g}"
                fn = {"momentum": lambda rs, c=c: pick(rs, "momentum", c), "fade": lambda rs, c=c: pick(rs, "fade", c),
                      "long_only": lambda rs, c=c: long_only(rs, c)}[name]
                g[c] = {"train": stats([v for _, v in fn(train)]), "holdout": stats([v for _, v in fn(test)])}
        g["none (close only)"] = {"train": stats([v for _, v in sets_fn(name, "none")(train)]),
                                  "holdout": stats([v for _, v in sets_fn(name, "none")(test)])}
        rep["grid"][name] = g
    # Features, at the live limits, long-only and momentum, on the training period.
    for name in ("long_only", "momentum"):
        rv = sets[name](train)
        rep["features"][name] = {
            "move vs normal": bucket(rv, lambda r: r["ratio"], [4, 6], ["2.5-4x", "4-6x", "6x+"]),
            "relative volume": bucket(rv, lambda r: r["rvol"], [1, 2], ["<1x", "1-2x", "2x+"]),
            "entry time (ET)": bucket(rv, lambda r: r["hour"], [11, 14], ["before 11", "11-14", "after 14"]),
            "stock moved": bucket(rv, lambda r: 1 if r["up"] else 0, [1, 2], ["down", "up"]),
            "market (SPY) same time": bucket(rv, lambda r: None if r["spy"] is None else r["spy"], [-0.3, 0.3], ["SPY down", "SPY flat", "SPY up"]),
        }
    return rep


def sets_fn(name, combo):
    return {"momentum": lambda rs: pick(rs, "momentum", combo), "fade": lambda rs: pick(rs, "fade", combo),
            "long_only": lambda rs: long_only(rs, combo)}[name]


def daily_avg_stats_pairs(pairs):
    """Trades on the same day move together (a market-wide drop hits many at once), so also look at one number
    per day: the average result of that day's trades."""
    per = defaultdict(list)
    for r, v in pairs:
        per[r["date"]].append(v)
    s = stats([statistics.mean(v) for v in per.values()])
    if "n" in s:
        s["days"] = s.pop("n")
    return s


def report_text(rep):
    L = [f"Backtest: {rep['trades']} triggered symbol-days ({rep['train_trades']} training, {rep['test_trades']} holdout from {rep['holdout_from']}).",
         f"Results are net of an assumed {rep['cost_pct']}% round-trip cost. Live limits (stop/target): {rep['primary']}.", "",
         "== Live limits, by strategy (mean % per trade, ± is a 95% margin) =="]
    for name, s in rep["strategies"].items():
        L += [f"{name}:", f"  all      {fmt(s['all'])}", f"  training {fmt(s['train'])}", f"  holdout  {fmt(s['holdout'])}",
              f"  one number per day: {s['per_day_all']}"]
    L += ["", "== By quarter (live limits) =="]
    for name, qs in rep["by_quarter"].items():
        L.append(f"{name}:")
        L += [f"  {q} {fmt(v)}" for q, v in qs.items()]
    L += ["", "== Stop/target grid: mean % net (training | holdout). Exploratory: many tries means some look good by luck =="]
    for name, g in rep["grid"].items():
        L.append(f"{name}:")
        for c, v in g.items():
            L.append(f"  {c:>17}  train {v['train'].get('mean', 'n/a')!s:>7} (n={v['train'].get('n', 0)})  holdout {v['holdout'].get('mean', 'n/a')!s:>7} (n={v['holdout'].get('n', 0)})")
    L += ["", "== Features at the live limits, training period only =="]
    for name, feats in rep["features"].items():
        L.append(f"{name}:")
        for feat, bs in feats.items():
            L.append(f"  {feat}:")
            L += [f"    {lab:>10} {fmt(s)}" for lab, s in bs.items()]
    L += ["", "Caveats: IEX-only prices (thin, bars with no trades are missing), today's S&P 500 list (names that left the index "
          "are absent, which flatters long results), 30-minute bars (stop/target order inside a bar is a guess), "
          "no position cap or borrow fees for shorts."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--rate", type=float, default=60.0, help="Alpaca calls per minute (limit is 200 and is shared)")
    ap.add_argument("--limit-symbols", type=int, default=0, help="for a quick trial")
    a = ap.parse_args()
    names, _, _ = d.load_universe()
    symbols = sorted(names)[: a.limit_symbols or None]
    if "SPY" not in symbols:
        symbols.append("SPY")
    today = datetime.now(d.ET).date()
    start = (today - timedelta(days=int(a.years * 365))).isoformat()
    warm = (today - timedelta(days=int(a.years * 365) + 140)).isoformat()
    end = today.isoformat()
    th = Throttle(a.rate)
    print(f"Fetching daily bars {warm}..{end} for {len(symbols)} symbols", flush=True)
    daily = fetch_bars(th, symbols, "1Day", warm, end, 100)
    intraday_syms = sorted(s for s in symbols if len(daily.get(s, [])) >= 200)
    print(f"{len(intraday_syms)} symbols have enough daily history", flush=True)
    rows, spy_by_day = [], {}
    for i in range(0, len(intraday_syms), 50):
        part = intraday_syms[i:i + 50]
        if "SPY" not in part and not spy_by_day:
            part = part + ["SPY"]  # needed once, for the market-direction feature
        intra = fetch_bars(th, part, "30Min", start, end, 50)
        if not spy_by_day and intra.get("SPY"):
            spy_daily = {x["t"][:10]: x["c"] for x in daily.get("SPY", [])}
            prev_close = {}
            days = sorted(spy_daily)
            for p, c in zip(days, days[1:]):
                prev_close[c] = spy_daily[p]
            for b in regular_session(intra["SPY"]):
                day = b["t"].date().isoformat()
                if day in prev_close:
                    spy_by_day.setdefault(day, {})[b["t"].strftime("%H:%M")] = round((b["c"] - prev_close[day]) / prev_close[day] * 100, 2)
        for s in part:
            if s in intraday_syms:
                rows += trades_for_symbol(s, daily[s], intra.get(s, []), spy_by_day)
        print(f"  {len(rows)} triggered days so far", flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    rep = analyse(rows, today)
    text = report_text(rep)
    with open(os.path.join(OUT_DIR, "report.txt"), "w") as f:
        f.write(text)
    with open(os.path.join(OUT_DIR, "results.json"), "w") as f:
        json.dump({"report": rep, "trades": [{k: v for k, v in r.items() if k != "res"} | {"res": r["res"]} for r in rows]}, f, separators=(",", ":"))
    print("\n" + text)
    print(f"\nTotal Alpaca calls: {th.calls}")


if __name__ == "__main__":
    main()
