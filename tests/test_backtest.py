"""Run: python3 -m unittest discover -s tests -v"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backtest"))
import backtest as bt  # noqa: E402


def bar(h, l, c):
    return {"h": h, "l": l, "c": c}


class Exits(unittest.TestCase):
    def test_long_hits_target_and_stop(self):
        self.assertEqual(bt.exit_pct("long", 100, [bar(101, 99, 100), bar(107, 100, 106)], 3, 6), 6)
        self.assertEqual(bt.exit_pct("long", 100, [bar(101, 96, 97)], 3, 6), -3)

    def test_short_mirrors_long(self):
        self.assertEqual(bt.exit_pct("short", 100, [bar(101, 93, 94)], 3, 6), 6)
        self.assertEqual(bt.exit_pct("short", 100, [bar(104, 99, 103)], 3, 6), -3)

    def test_same_bar_stop_wins_and_close_settles_the_rest(self):
        self.assertEqual(bt.exit_pct("long", 100, [bar(107, 96, 100)], 3, 6), -3)
        self.assertAlmostEqual(bt.exit_pct("long", 100, [bar(101, 99, 101.5)], 3, 6), 1.5)
        self.assertAlmostEqual(bt.exit_pct("short", 100, [bar(101, 99, 101.5)], None, None), -1.5)


class Trigger(unittest.TestCase):
    def day(self, closes, start_hour=9, start_min=30):
        t0 = datetime(2026, 3, 2, start_hour, start_min, tzinfo=bt.d.ET)
        return [{"t": t0 + timedelta(minutes=30 * i), "o": c, "h": c, "l": c, "c": c, "v": 1000} for i, c in enumerate(closes)]

    def test_first_bar_that_clears_both_bars_is_the_entry(self):
        bars = self.day([101, 102, 104, 105, 105, 105, 105, 105, 105, 105, 105, 105, 105])
        k, pct, ratio, _ = bt.find_trigger(bars, 100, 1.0, 10000)
        self.assertEqual((k, round(pct, 1), ratio), (2, 4.0, 4.0))  # 2% is 2x normal (not enough), 4% is 4x

    def test_nothing_when_normal_is_big_or_move_is_small(self):
        bars = self.day([101] * 13)
        self.assertIsNone(bt.find_trigger(bars, 100, 1.0, 10000))  # 1% is under the 1.5% floor
        self.assertIsNone(bt.find_trigger(self.day([104] * 13), 100, 2.0, 10000))  # only 2x normal

    def test_no_entry_in_the_last_half_hour(self):
        bars = self.day([100] * 12 + [105])  # the 15:30 bar starts after the last entry bar
        self.assertIsNone(bt.find_trigger(bars, 100, 1.0, 10000))


if __name__ == "__main__":
    unittest.main()
