"""Run: python3 -m unittest discover -s tests -v"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backtest"))
import events as ev  # noqa: E402

ET = ev.d.ET


def bars(closes, highs=None, lows=None):
    t0 = datetime(2026, 3, 2, 9, 30, tzinfo=ET)
    out = []
    for i, c in enumerate(closes):
        t = t0 + timedelta(minutes=5 * i)
        out.append({"t": t, "end": t + timedelta(minutes=5), "o": c, "h": (highs or closes)[i], "l": (lows or closes)[i], "c": c, "v": 100})
    return out


class FindEvent(unittest.TestCase):
    def test_first_bar_past_threshold(self):
        b = bars([99, 98, 95, 94])
        self.assertEqual(ev.find_event(b, "drop", 100, 1.0), 2)
        self.assertIsNone(ev.find_event(b, "spike", 100, 1.0))

    def test_needs_ratio_not_just_percent(self):
        self.assertIsNone(ev.find_event(bars([95]), "drop", 100, 2.0))   # 5% < 4 x 2%


class Measure(unittest.TestCase):
    def test_drop_that_bounces(self):
        b = bars([94] + [95] * 12 + [97] * 12)
        m = ev.measure(b, 0, "drop", 100)
        self.assertAlmostEqual(m["fwd"]["15"], (95 - 94) / 94 * 100, places=2)
        self.assertEqual(m["half"], 5 * 13)       # needs 97 = halfway; first reached 13 bars later
        self.assertIsNone(m["full"])

    def test_spike_that_fades_counts_as_positive(self):
        b = bars([106, 104, 104, 104])
        self.assertGreater(ev.measure(b, 0, "spike", 100)["fwd"]["close"], 0)

    def test_worse_is_never_negative(self):
        b = bars([94, 95, 96, 97, 98, 99, 99, 99, 99, 99, 99, 99, 99])
        self.assertEqual(ev.measure(b, 0, "drop", 100)["worse_1h"], 0.0)

    def test_excess_removes_the_market(self):
        b = bars([94, 94, 94, 94, 99])
        spy = {x["end"].strftime("%H:%M"): 100 * (1 + 0.001 * i) for i, x in enumerate(b)}
        m = ev.measure(b, 0, "drop", 100, spy)
        self.assertLess(m["excess"]["15"], m["fwd"]["15"] + 1e-9)


class Stats(unittest.TestCase):
    def test_cluster_margin_wider_than_independent(self):
        rows = [{"date": f"d{i // 5}", "x": (1 if i // 5 % 2 else -1) * 1.0} for i in range(40)]
        s = ev.cluster_stats(rows, lambda r: r["x"])
        self.assertEqual(s["n"], 40)
        self.assertGreater(s["ci"], 0.5)

    def test_too_few_returns_only_n(self):
        self.assertEqual(ev.cluster_stats([{"date": "a", "x": 1}], lambda r: r["x"]), {"n": 1})


if __name__ == "__main__":
    unittest.main()
