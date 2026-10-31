"""Event study: after a big drop (or big spike) in a stock, how far and how fast does it move back?

Standalone, like backtest.py: reads Alpaca history slowly, writes only under backtest/out/, and nothing here is
imported by the digest, the worker or the page.

    python backtest/events.py --years 2 --rate 100        # needs ALPACA_KEY_ID / ALPACA_SECRET_KEY
    python -m unittest discover -s tests                  # unit tests need no keys

An event is the first 5-minute bar of a day that is at least DROP_PCT away from yesterday's close and at least
RATIO times the stock's normal daily move (down = "drop", up = "spike"), entered at that bar's close before 15:00 ET.
Every number is a "reversal": positive means the stock moved back toward yesterday's close. Results are shown
relative to SPY over the same window, so a rising market does not look like a bounce. Free IEX bars: thin, and
today's S&P 500 list. This measures the stock, not option prices.
"""

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402  (Throttle, fetch_bars, same live rules via digest)

d = bt.d
OUT_DIR = bt.OUT_DIR
MOVE_PCT = 3.0                    # at least this far from yesterday's close...
RATIO = 4.0                       # ...and this many times the stock's normal daily move
HORIZONS = [15, 30, 60, 120]      # minutes after entry
LAST_ENTRY_END_MIN = 15 * 60      # entry bar must close by 15:00 ET
HOLDOUT_DAYS = bt.HOLDOUT_DAYS
BAR = 5


def regular_5(bars):
    """Regular-session 5-minute bars, oldest first, with ET start/end times."""
    out = []
    for b in bars:
        t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(d.ET)
        if (9, 30) <= (t.hour, t.minute) <= (15, 55):
            out.append({"t": t, "end": t + timedelta(minutes=BAR), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
    return out


def candidates(daily, start_day):
    """Days where the daily range alone reached the threshold: the cheap filter that says which days need 5-minute bars.
    Returns [(day, kind, prev_close, normal_move, avg_volume, prior_trend_pct)]."""
    closes = [x["c"] for x in daily]
    vols = [x["v"] for x in daily]
    out = []
    for i, x in enumerate(daily):
        day = x["t"][:10]
        if i < 61 or day < start_day:
            continue
        prev = closes[i - 1]
        nm = d.normal_move(closes[max(0, i - d.LONG_LOOKBACK - 1):i])
        if not nm or not prev:
            continue
        need = max(MOVE_PCT, RATIO * nm)
        low, high = (x["l"] - prev) / prev * 100, (x["h"] - prev) / prev * 100
        trend = (prev - closes[i - 6]) / closes[i - 6] * 100 if closes[i - 6] else None   # the 5 days before today
        base = (day, prev, nm, d.avg_volume(vols[max(0, i - d.LOOKBACK):i]), trend)
        if low <= -need:
            out.append(base[:1] + ("drop",) + base[1:])
        if high >= need:
            out.append(base[:1] + ("spike",) + base[1:])
    return out


def find_event(bars, kind, prev, nm):
    """Index of the first entry bar, or None."""
    sgn = -1 if kind == "drop" else 1
    for k, b in enumerate(bars):
        if b["end"].hour * 60 + b["end"].minute > LAST_ENTRY_END_MIN:
            return None
        pct = (b["c"] - prev) / prev * 100
        if sgn * pct >= max(MOVE_PCT, RATIO * nm):
            return k
    return None


def measure(bars, k, kind, prev, spy=None):
    """What happened after bar k. All reversal figures: positive = moved back toward yesterday's close.
    spy: {end_time_string: close} for the same day (to take the market out), or None."""
    sgn = 1 if kind == "drop" else -1            # +1: a rise is a reversal
    entry = bars[k]["c"]
    end0 = bars[k]["end"]
    after = bars[k + 1:]
    out = {"entry": entry, "move": round((entry - prev) / prev * 100, 2), "fwd": {}, "excess": {}}
    spy0 = (spy or {}).get(end0.strftime("%H:%M"))
    for h in HORIZONS + ["close"]:
        if h == "close":
            b = bars[-1]
        else:
            target = end0 + timedelta(minutes=h)
            b = next((x for x in bars if x["end"] == target), None)
        if not b or (h != "close" and b["end"].hour * 60 + b["end"].minute > 16 * 60):
            continue
        r = sgn * (b["c"] - entry) / entry * 100
        out["fwd"][str(h)] = round(r, 3)
        s = (spy or {}).get(b["end"].strftime("%H:%M"))
        if spy0 and s:
            out["excess"][str(h)] = round(r - sgn * (s - spy0) / spy0 * 100, 3)
    # Halfway back to yesterday's close, and all the way: did it happen, and after how many minutes?
    for name, frac in (("half", 0.5), ("full", 1.0)):
        tgt = entry + frac * (prev - entry)
        hit = next((x for x in after if (x["h"] >= tgt if kind == "drop" else x["l"] <= tgt)), None)
        out[name] = round((hit["end"] - end0).total_seconds() / 60) if hit else None
    # How much worse did it get before the next hour was over (what a stop would have faced)?
    window = [x for x in after if (x["end"] - end0).total_seconds() <= 3600]
    if window:
        worst = min(x["l"] for x in window) if kind == "drop" else max(x["h"] for x in window)
        out["worse_1h"] = round(max(0.0, -sgn * (worst - entry) / entry * 100), 3)
    return out


def spy_for_day(spy_bars):
    return {b["end"].strftime("%H:%M"): b["c"] for b in regular_5(spy_bars)}


# ---------- statistics ----------

def cluster_stats(rows, key):
    """Mean of key(row) over rows with the day-clustered 95% margin (events on the same day move together)."""
    pts = [(r["date"], key(r)) for r in rows if key(r) is not None]
    n = len(pts)
    if n < 5:
        return {"n": n}
    m = statistics.mean(v for _, v in pts)
    per = defaultdict(float)
    for day, v in pts:
        per[day] += v - m
    se = math.sqrt(sum(s * s for s in per.values())) / n
    return {"n": n, "days": len(per), "mean": round(m, 3), "ci": round(1.96 * se, 3),
            "median": round(statistics.median(v for _, v in pts), 3)}


def share(rows, key):
    got = [key(r) for r in rows]
    return round(sum(g is not None for g in got) / len(got) * 100, 1) if got else None


def fmt(s):
    return f"n={s.get('n', 0):>5}" if s.get("n", 0) < 5 else (
        f"n={s['n']:>5} days={s['days']:>4} mean={s['mean']:+.3f}% (±{s['ci']:.2f}) median={s['median']:+.2f}%")


def slices(rows):
    def b(x, edges, labels):
        if x is None:
            return "n/a"
        for e, lab in zip(edges, labels):
            if x < e:
                return lab
        return labels[-1]
    return {
        "size of move vs normal": lambda r: b(r["ratio"], [6, 10], ["4-6x", "6-10x", "10x+"]),
        "relative volume": lambda r: b(r["rvol"], [1, 2.5], ["<1x", "1-2.5x", "2.5x+"]),
        "time of day (ET)": lambda r: b(r["hour"], [10.5, 12.5], ["before 10:30", "10:30-12:30", "after 12:30"]),
        "market (SPY) that day so far": lambda r: b(r["spy_move"], [-0.7, 0.3], ["SPY down 0.7%+", "SPY roughly flat", "SPY up"]),
        "5 days before": lambda r: b(r["trend"], [-4, 4], ["fell 4%+", "flat", "rose 4%+"]),
        "move size (%)": lambda r: b(abs(r["move"]), [5, 8], ["3-5%", "5-8%", "8%+"]),
    }


def analyse(events, today):
    cutoff = (today - timedelta(days=HOLDOUT_DAYS)).isoformat()
    rep = {"events": len(events), "holdout_from": cutoff, "kinds": {}}
    for kind in ("drop", "spike"):
        rows = [e for e in events if e["kind"] == kind]
        train, test = [e for e in rows if e["date"] < cutoff], [e for e in rows if e["date"] >= cutoff]
        k = {"n": len(rows), "train": len(train), "holdout": len(test), "excess": {}, "raw": {}, "half": {}, "full": {}, "worse_1h": {}, "slices": {}}
        for h in [str(x) for x in HORIZONS] + ["close"]:
            k["excess"][h] = {"train": cluster_stats(train, lambda r, h=h: r["excess"].get(h)),
                              "holdout": cluster_stats(test, lambda r, h=h: r["excess"].get(h))}
            k["raw"][h] = {"train": cluster_stats(train, lambda r, h=h: r["fwd"].get(h)),
                           "holdout": cluster_stats(test, lambda r, h=h: r["fwd"].get(h))}
        for name in ("half", "full"):
            k[name] = {"train_pct": share(train, lambda r, n=name: r[n]), "holdout_pct": share(test, lambda r, n=name: r[n]),
                       "median_minutes": (statistics.median([r[name] for r in rows if r[name] is not None])
                                          if any(r[name] is not None for r in rows) else None)}
        k["worse_1h"] = cluster_stats(rows, lambda r: r.get("worse_1h"))
        for sname, fn in slices(rows).items():
            out = {}
            for label in sorted({fn(r) for r in rows}):
                grp_tr, grp_te = [r for r in train if fn(r) == label], [r for r in test if fn(r) == label]
                out[label] = {h: {"train": cluster_stats(grp_tr, lambda r, h=h: r["excess"].get(h)),
                                  "holdout": cluster_stats(grp_te, lambda r, h=h: r["excess"].get(h))} for h in ("60", "close")}
            k["slices"][sname] = out
        rep["kinds"][kind] = k
    return rep


def report_text(rep):
    L = [f"Event study: {rep['events']} events; holdout (never used to pick anything) = from {rep['holdout_from']}.",
         "Reversal = move back toward yesterday's close. 'Excess' = reversal minus what SPY did in the same window. ± is a 95% margin "
         "counting same-day events as one cluster.", ""]
    names = {"drop": "BIG DROPS (does it bounce?)", "spike": "BIG SPIKES (does it fade back?)"}
    for kind, k in rep["kinds"].items():
        L += [f"=== {names[kind]}: {k['n']} events ({k['train']} earlier, {k['holdout']} held out) ==="]
        L.append("Excess reversal vs SPY, by time after entry:")
        for h, v in k["excess"].items():
            L += [f"  {h:>5}  earlier: {fmt(v['train'])}", f"         held out: {fmt(v['holdout'])}"]
        L.append("Raw reversal (not market-adjusted), held out:")
        L += [f"  {h:>5}  {fmt(v['holdout'])}" for h, v in k["raw"].items()]
        for name, label in (("half", "got halfway back to yesterday's close"), ("full", "got all the way back")):
            s = k[name]
            L.append(f"  {label}: earlier {s['train_pct']}% of events, held out {s['holdout_pct']}%, median {s['median_minutes']} min when it did")
        L.append(f"  how much worse it got in the next hour (the stop a trade would face): {fmt(k['worse_1h'])}")
        L.append("By condition (excess reversal at 60 minutes and at the close; earlier | held out):")
        for sname, groups in k["slices"].items():
            L.append(f"  {sname}:")
            for label, hs in groups.items():
                a, c = hs["60"], hs["close"]
                L.append(f"    {label:>18}  60m: {a['train'].get('mean', 'n/a')!s:>7} (n={a['train'].get('n', 0)}) | {a['holdout'].get('mean', 'n/a')!s:>7} (n={a['holdout'].get('n', 0)})"
                         f"   close: {c['train'].get('mean', 'n/a')!s:>7} | {c['holdout'].get('mean', 'n/a')!s:>7}")
        L.append("")
    L.append("Caveats: IEX-only prices (thin, missing bars when nothing traded), today's S&P 500 list (names that left are absent), "
             "5-minute bars, no news or earnings flags, no costs. This is the stock's move, not an option's: an option also has to beat "
             "its spread and time decay.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--rate", type=float, default=60.0)
    ap.add_argument("--limit-symbols", type=int, default=0)
    a = ap.parse_args()
    names, _, _ = d.load_universe()
    symbols = sorted(names)[: a.limit_symbols or None]
    if "SPY" not in symbols:
        symbols.append("SPY")
    today = datetime.now(d.ET).date()
    start_day = (today - timedelta(days=int(a.years * 365))).isoformat()
    warm = (today - timedelta(days=int(a.years * 365) + 140)).isoformat()
    th = bt.Throttle(a.rate)
    print(f"Fetching daily bars for {len(symbols)} symbols", flush=True)
    daily = bt.fetch_bars(th, symbols, "1Day", warm, today.isoformat(), 100)
    by_day = defaultdict(list)       # day -> [(symbol, kind, prev, nm, avgvol, trend)]
    for s in symbols:
        if s == "SPY" or len(daily.get(s, [])) < 200:
            continue
        for day, kind, prev, nm, av, trend in candidates(daily[s], start_day):
            by_day[day].append((s, kind, prev, nm, av, trend))
    spy_prev = {}
    sd = sorted(x["t"][:10] for x in daily.get("SPY", []))
    closes = {x["t"][:10]: x["c"] for x in daily.get("SPY", [])}
    for p, c in zip(sd, sd[1:]):
        spy_prev[c] = closes[p]
    total = sum(len(v) for v in by_day.values())
    print(f"{total} candidate stock-days on {len(by_day)} days need 5-minute bars", flush=True)
    events = []
    for n, (day, items) in enumerate(sorted(by_day.items())):
        syms = sorted({i[0] for i in items} | {"SPY"})
        y, m, dd = map(int, day.split("-"))
        t0 = datetime(y, m, dd, 9, 30, tzinfo=d.ET).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        t1 = datetime(y, m, dd, 16, 0, tzinfo=d.ET).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars = fetch_day(th, syms, t0, t1)
        spy = spy_for_day(bars.get("SPY", []))
        for s, kind, prev, nm, av, trend in items:
            b5 = regular_5(bars.get(s, []))
            if len(b5) < 40 or b5[0]["t"].hour * 60 + b5[0]["t"].minute != 9 * 60 + 30:
                continue
            k = find_event(b5, kind, prev, nm)
            if k is None:
                continue
            res = measure(b5, k, kind, prev, spy)
            cum = sum(x["v"] for x in b5[:k + 1])
            spy_close = spy.get(b5[k]["end"].strftime("%H:%M"))
            events.append({"sym": s, "date": day, "kind": kind, "ratio": round(abs(res["move"]) / nm, 1),
                           "rvol": d.rel_volume(cum, av, d.session_fraction(b5[k]["end"])),
                           "hour": b5[k]["end"].hour + b5[k]["end"].minute / 60, "trend": None if trend is None else round(trend, 2),
                           "spy_move": None if not spy_close or day not in spy_prev else round((spy_close - spy_prev[day]) / spy_prev[day] * 100, 2),
                           **res})
        if n % 25 == 0:
            print(f"  day {n + 1}/{len(by_day)}: {len(events)} events so far ({th.calls} calls)", flush=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    rep = analyse(events, today)
    text = report_text(rep)
    with open(os.path.join(OUT_DIR, "events_report.txt"), "w") as f:
        f.write(text)
    with open(os.path.join(OUT_DIR, "events.json"), "w") as f:
        json.dump(events, f, separators=(",", ":"))
    print("\n" + text)
    print(f"\nTotal Alpaca calls: {th.calls}")


def fetch_day(th, syms, t0, t1):
    """5-minute bars for a few symbols over one session."""
    out = defaultdict(list)
    for i in range(0, len(syms), 40):
        token = None
        while True:
            params = {"symbols": ",".join(syms[i:i + 40]), "timeframe": "5Min", "start": t0, "end": t1,
                      "feed": "iex", "adjustment": "split", "limit": 10000}
            if token:
                params["page_token"] = token
            res = th.get("/v2/stocks/bars", params)
            for sym, bars in (res.get("bars") or {}).items():
                out[sym].extend(bars)
            token = res.get("next_page_token")
            if not token:
                break
    return out


if __name__ == "__main__":
    main()
