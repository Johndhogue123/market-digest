"""Run: python3 -m unittest discover -s tests -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import digest as d  # noqa: E402

SETTINGS = {"near_52w_pct": d.NEAR_52W_PCT, "streak_min": d.STREAK_MIN}


class Analysis(unittest.TestCase):
    def test_normal_move_is_larger_of_month_and_quarter(self):
        quiet_month = [100 + (i % 2) * 4 for i in range(40)] + [100 + (i % 2) * 0.1 for i in range(21)]
        month, quarter = d.mean_move(quiet_month, d.LOOKBACK), d.mean_move(quiet_month, d.LONG_LOOKBACK)
        self.assertLess(month, quarter)
        self.assertEqual(d.normal_move(quiet_month), quarter)

    def test_normal_move_math(self):
        closes = [100, 102, 100, 102, 100, 102, 100]  # moves: 2%, ~1.96%, ...
        got = d.normal_move(closes)
        self.assertAlmostEqual(got, (2 + 2 / 102 * 100) / 2, places=6)

    def test_normal_move_needs_history(self):
        self.assertIsNone(d.normal_move([100, 101, 102]))

    def test_52w_range_is_low_then_high_and_stretches_for_today(self):
        t = d.trend(110, 111, 105, [100] * 6, (120, 80))  # baseline order is (high, low)
        self.assertEqual(t["w52"], [80, 120])
        t = d.trend(130, 131, 125, [100] * 6, (120, 80))  # today broke the old high
        self.assertEqual(t["w52"], [80, 131])
        self.assertEqual(t["near52"], "high")

    def test_near_52w_low_and_none(self):
        self.assertEqual(d.trend(81, 82, 80.5, [90] * 6, (120, 80))["near52"], "low")
        self.assertIsNone(d.trend(100, 101, 99, [100] * 6, (120, 80))["near52"])
        self.assertIsNone(d.trend(100, 101, 99, [100] * 6, None)["w52"])

    def test_streaks(self):
        self.assertEqual(d.trend(105, 106, 104, [100, 101, 102, 103, 104], None)["streak"], 5)
        self.assertEqual(d.trend(95, 96, 94, [100, 99, 98, 97, 96], None)["streak"], -5)
        self.assertEqual(d.trend(99, 100, 98, [100, 101, 102, 103, 100], None)["streak"], -2)
        self.assertEqual(d.trend(100, 100, 100, [100, 100, 100, 100], None)["streak"], 0)

    def test_chg3(self):
        self.assertEqual(d.trend(110, 111, 109, [90, 100, 100, 100], None)["chg3"], 10.0)

    def test_fade_read(self):
        self.assertEqual(d.fade_read(103, 110, 99, 100)["kind"], "fade")      # peaked +10%, gave back 70%
        self.assertEqual(d.fade_read(97, 101, 90, 100)["kind"], "recover")
        self.assertEqual(d.fade_read(100, 100, 100, 100), None)

    def test_rel_volume(self):
        self.assertEqual(d.rel_volume(2_000_000, 1_000_000, 1.0), 2.0)
        self.assertIsNone(d.rel_volume(1, None, 1.0))

    def test_session_fraction_bounds(self):
        et = d.ET
        self.assertEqual(d.session_fraction(datetime(2026, 10, 23, 16, 0, tzinfo=et)), 1.0)
        self.assertAlmostEqual(d.session_fraction(datetime(2026, 10, 23, 9, 30, tzinfo=et)), 0.03 ** 0.7)

    def test_pick_lists(self):
        def row(sym, pct, normal):
            return {"symbol": sym, "pct": pct, "normal": normal, "ratio": round(abs(pct) / normal, 1),
                    "streak": 0, "near52": None}
        rows = [row("CALM", 1.0, 0.5),    # ratio 2.0: not unusual
                row("SMALL", 1.2, 0.3),   # ratio 4 but under the 1.5% floor
                row("BIG", 4.0, 1.0),     # unusual
                row("TSLA", -2.0, 3.0)]   # swinger, normal day
        rows.append({**row("SPY", 0.1, 0.8)})
        unusual, swingers, market = d.pick_lists(rows, ["SPY", "QQQ"])
        self.assertEqual([r["symbol"] for r in unusual], ["BIG"])
        self.assertEqual([r["symbol"] for r in swingers], ["TSLA"])
        self.assertEqual(swingers[0]["usual"], "Normal day for it")
        self.assertEqual([r["symbol"] for r in market], ["SPY"])

    def test_rank_boost(self):
        self.assertGreater(d.rank_score({"ratio": 4, "near52": "high", "streak": 0}), 4)
        self.assertEqual(d.rank_score({"ratio": 4, "near52": None, "streak": 1}), 4)


class Validation(unittest.TestCase):
    syms = [f"S{i}" for i in range(100)]
    good_row = {"symbol": "S1", "price": 10, "prev": 9.5, "pct": 5.2}

    def test_good_scan_passes(self):
        d.validate_scan(self.syms, {s: {} for s in self.syms}, {s: [1] for s in self.syms}, [self.good_row] * 50)

    def test_partial_snapshots_blocked(self):
        with self.assertRaises(d.BadData):
            d.validate_scan(self.syms, {s: {} for s in self.syms[:50]}, {s: [1] for s in self.syms})

    def test_missing_history_blocked(self):
        with self.assertRaises(d.BadData):
            d.validate_scan(self.syms, {s: {} for s in self.syms}, {})

    def test_nonsense_rows_blocked(self):
        bad = {"symbol": "X", "price": 0, "prev": 9, "pct": -100}
        with self.assertRaises(d.BadData):
            d.validate_scan(self.syms, {s: {} for s in self.syms}, {s: [1] for s in self.syms}, [bad] * 10 + [self.good_row] * 40)

    def test_one_real_outlier_is_fine(self):
        wild = {"symbol": "X", "price": 30, "prev": 10, "pct": 200}
        d.validate_scan(self.syms, {s: {} for s in self.syms}, {s: [1] for s in self.syms}, [wild] + [self.good_row] * 99)


class Calendar(unittest.TestCase):
    fri = {"date": "2026-10-23", "open": "09:30", "close": "16:00"}
    mon = {"date": "2026-10-26", "open": "09:30", "close": "16:00"}

    def test_trading_day(self):
        m = d.market_status("2026-10-23", [self.fri, self.mon])
        self.assertFalse(m["closed"])
        self.assertEqual(m["close"], "2026-10-23T16:00-04:00")
        self.assertEqual(m["next"], {"date": "2026-10-26", "open": "09:30"})

    def test_weekend_or_holiday_is_closed_with_next_session(self):
        m = d.market_status("2026-10-24", [self.mon])
        self.assertTrue(m["closed"])
        self.assertIsNone(m["open"])
        self.assertEqual(m["next"]["date"], "2026-10-26")

    def test_early_close_is_kept(self):
        m = d.market_status("2026-11-27", [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}])
        self.assertTrue(m["close"].startswith("2026-11-27T13:00"))


class Publishing(unittest.TestCase):
    def test_write_json_is_atomic_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "x.json")
            d.write_json(p, {"a": 1})
            d.write_json(p, {"a": 2})
            with open(p) as f:
                self.assertEqual(json.load(f), {"a": 2})
            self.assertEqual(os.listdir(t), ["x.json"])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class WorkerParity(unittest.TestCase):
    """worker.js mirrors digest.py's list logic; both must give the same answers on the same inputs."""

    def run_js(self, cases):
        out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "harness.js")],
                             input=json.dumps(cases), capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_trend_parity(self):
        args = [
            (110, 111, 105, [100] * 6, (120, 80)), (130, 131, 125, [100] * 6, (120, 80)),
            (81, 82, 80.5, [90] * 6, (120, 80)), (105, 106, 104, [100, 101, 102, 103, 104], None),
            (95, 96, 94, [100, 99, 98, 97, 96], None), (99, 100, 98, [100, 101, 102, 103, 100], None),
            (100, 100, 100, [100, 100, 100, 100], None), (110, 111, 109, [90, 100, 100, 100], (150, 50)),
            (50, 51, 49, [], (60, 40)),
        ]
        js = self.run_js([{"fn": "trend", "args": [SETTINGS, *a]} for a in args])
        for a, got in zip(args, js):
            want = d.trend(*a)
            self.assertEqual(got, want, a)

    def test_fade_read_parity(self):
        args = [(103, 110, 99, 100), (97, 101, 90, 100), (100, 100, 100, 100), (98, 101, 98, 100), (102, 102.2, 98, 100)]
        js = self.run_js([{"fn": "fadeRead", "args": list(a)} for a in args])
        for a, got in zip(args, js):
            self.assertEqual(got, d.fade_read(*a), a)

    def test_rank_score_parity(self):
        rows = [{"ratio": 4, "near52": "high", "streak": 0}, {"ratio": 4, "near52": None, "streak": 3},
                {"ratio": 4, "near52": None, "streak": -1}]
        js = self.run_js([{"fn": "rankScore", "args": [r, SETTINGS]} for r in rows])
        for r, got in zip(rows, js):
            self.assertAlmostEqual(got, d.rank_score(r))

    def test_session_fraction_parity(self):
        mins = [570, 600, 720, 960, 1000]
        js = self.run_js([{"fn": "sessionFraction", "args": [m]} for m in mins])
        for m, got in zip(mins, js):
            et = datetime(2026, 10, 23, m // 60, m % 60, tzinfo=d.ET)
            self.assertAlmostEqual(got, d.session_fraction(et))


if __name__ == "__main__":
    unittest.main()
