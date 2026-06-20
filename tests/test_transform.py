"""
test_transform.py — unit tests for the margin-pressure logic.

Pure and offline: no network, no database, no config file. Run with:

    python3 -m unittest -v test_transform

These tests pin down the *behaviour* of the index — neutral midpoint, the
direction signs, weight-normalization invariance, forward-fill alignment, and
the degenerate cases (flat baseline, too little history) — so future tweaks
can't silently change what the score means.
"""

import unittest

from feedstock import transform


def flat_config(components, window=20, k=1.0):
    """Build a minimal config dict for tests."""
    return {"trailing_window": window, "logistic_k": k, "components": components}


class TestZScore(unittest.TestCase):
    def test_known_value(self):
        # baseline [1,2,3,4,5]: mean 3, sample stdev sqrt(2.5)=1.5811388
        # z of 6 = (6-3)/1.5811388 = 1.897367
        self.assertAlmostEqual(transform.zscore(6, [1, 2, 3, 4, 5]), 1.897367, places=5)

    def test_at_mean_is_zero(self):
        self.assertEqual(transform.zscore(3, [1, 2, 3, 4, 5]), 0.0)

    def test_zero_variance_returns_zero(self):
        # Flat baseline -> std 0 -> would divide by zero; must return 0.0.
        self.assertEqual(transform.zscore(99, [5, 5, 5, 5]), 0.0)

    def test_insufficient_history_returns_zero(self):
        self.assertEqual(transform.zscore(10, []), 0.0)
        self.assertEqual(transform.zscore(10, [5]), 0.0)


class TestLogistic(unittest.TestCase):
    def test_midpoint(self):
        self.assertAlmostEqual(transform.logistic(0.0), 0.5)

    def test_monotonic_and_bounded(self):
        self.assertTrue(0 < transform.logistic(-3) < transform.logistic(3) < 1)

    def test_overflow_guard(self):
        # Extreme inputs must saturate, not raise OverflowError.
        self.assertEqual(transform.logistic(1000), 1.0)
        self.assertEqual(transform.logistic(-1000), 0.0)


class TestAlignByDate(unittest.TestCase):
    def test_forward_fill_and_union(self):
        series_map = {
            "a": [("2026-01-01", 1.0), ("2026-01-03", 3.0)],
            "b": [("2026-01-02", 20.0)],
        }
        dates, aligned = transform.align_by_date(series_map)
        self.assertEqual(dates, ["2026-01-01", "2026-01-02", "2026-01-03"])
        # 'a' has no value on 01-02 -> carry 1.0 forward.
        self.assertEqual(aligned["a"], [1.0, 1.0, 3.0])
        # 'b' has nothing before 01-02 -> None, then 20.0 carried forward.
        self.assertEqual(aligned["b"], [None, 20.0, 20.0])


class TestPressureIndex(unittest.TestCase):
    def test_neutral_when_at_baseline_mean(self):
        # Current value equals the baseline mean -> z 0 -> index exactly 50.
        series = [(f"2026-01-{d:02d}", v) for d, v in
                  enumerate([10, 12, 8, 10, 12, 8, 10], start=1)]
        # next day printed at the mean (10) -> z 0
        series.append(("2026-01-08", 10.0))
        cfg = flat_config({"x": {"weight": 1.0, "direction": 1}})
        snap = transform.compute_latest_pressure({"x": series}, cfg)
        self.assertAlmostEqual(snap["index"], 50.0, places=6)
        self.assertTrue(snap["sufficient"])

    def test_rising_cost_pushes_above_50(self):
        base = [80, 81, 79, 80, 82, 78, 80, 81, 79, 80]
        series = [(f"2026-02-{d:02d}", v) for d, v in enumerate(base, start=1)]
        series.append(("2026-02-20", 95.0))  # sharp spike well above baseline
        cfg = flat_config({"x": {"weight": 1.0, "direction": 1}})
        snap = transform.compute_latest_pressure({"x": series}, cfg)
        self.assertGreater(snap["index"], 90.0)

    def test_direction_inverts_sign(self):
        # Same upward spike, but direction -1 (an "FX strengthens" metric)
        # must push the index BELOW 50 instead of above.
        base = [1.10, 1.11, 1.09, 1.10, 1.12, 1.08, 1.10, 1.11, 1.09, 1.10]
        series = [(f"2026-03-{d:02d}", v) for d, v in enumerate(base, start=1)]
        series.append(("2026-03-20", 1.25))
        cfg = flat_config({"fx": {"weight": 1.0, "direction": -1}})
        snap = transform.compute_latest_pressure({"fx": series}, cfg)
        self.assertLess(snap["index"], 10.0)

    def test_weight_magnitude_invariance(self):
        # Doubling every weight must not change the index — only ratios matter.
        a = [(f"2026-04-{d:02d}", v) for d, v in
             enumerate([80, 82, 78, 80, 81, 79, 80, 95], start=1)]
        b = [(f"2026-04-{d:02d}", v) for d, v in
             enumerate([3.0, 3.1, 2.9, 3.0, 3.2, 2.8, 3.0, 3.5], start=1)]
        sm = {"a": a, "b": b}
        cfg1 = flat_config({"a": {"weight": 1, "direction": 1},
                            "b": {"weight": 1, "direction": 1}})
        cfg2 = flat_config({"a": {"weight": 2, "direction": 1},
                            "b": {"weight": 2, "direction": 1}})
        s1 = transform.compute_latest_pressure(sm, cfg1)
        s2 = transform.compute_latest_pressure(sm, cfg2)
        self.assertAlmostEqual(s1["index"], s2["index"], places=9)

    def test_single_point_is_neutral_and_flagged(self):
        # Exactly our live-DB situation: one observation per metric.
        cfg = flat_config({"x": {"weight": 1.0, "direction": 1}})
        snap = transform.compute_latest_pressure({"x": [("2026-06-19", 80.0)]}, cfg)
        self.assertEqual(snap["index"], 50.0)
        self.assertFalse(snap["sufficient"])

    def test_empty_is_neutral(self):
        cfg = flat_config({"x": {"weight": 1.0, "direction": 1}})
        snap = transform.compute_latest_pressure({"x": []}, cfg)
        self.assertEqual(snap["index"], 50.0)
        self.assertFalse(snap["sufficient"])

    def test_higher_k_is_more_sensitive(self):
        # Same data, larger logistic_k -> index further from 50 in same direction.
        base = [80, 81, 79, 80, 82, 78, 80, 81, 79, 80]
        series = [(f"2026-05-{d:02d}", v) for d, v in enumerate(base, start=1)]
        series.append(("2026-05-20", 88.0))
        sm = {"x": series}
        low = transform.compute_latest_pressure(sm, flat_config({"x": {"weight": 1, "direction": 1}}, k=0.5))
        high = transform.compute_latest_pressure(sm, flat_config({"x": {"weight": 1, "direction": 1}}, k=2.0))
        self.assertGreater(high["index"], low["index"])
        self.assertGreater(low["index"], 50.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
