"""
test_backtest.py — unit tests for the validation engine (pure, offline).
"""

import unittest

from feedstock import backtest


class TestSampleAtDates(unittest.TestCase):
    def test_as_of_join(self):
        daily = [("2026-01-05", 1.0), ("2026-01-20", 2.0), ("2026-02-10", 3.0)]
        # PPI prints at month-end-ish dates; take last daily on/before each.
        out = backtest.sample_at_dates(daily, ["2026-01-31", "2026-02-28"])
        self.assertEqual(out, [2.0, 3.0])

    def test_none_before_first(self):
        daily = [("2026-03-01", 5.0)]
        self.assertEqual(backtest.sample_at_dates(daily, ["2026-01-31"]), [None])


class TestValidate(unittest.TestCase):
    def _series(self):
        # Construct an index that clearly co-moves with a PPI, month over month.
        months = [f"2026-{m:02d}-28" for m in range(1, 11)]
        idx_vals = [40, 45, 55, 60, 52, 48, 58, 66, 70, 64]
        ppi_vals = [100, 101, 103, 104, 103, 102, 104, 106, 107, 106]
        index_daily = list(zip(months, idx_vals))
        ppi_monthly = list(zip(months, ppi_vals))
        return index_daily, ppi_monthly

    def test_report_shape_and_positive_corr(self):
        idx, ppi = self._series()
        rep = backtest.validate(idx, ppi)
        self.assertEqual(rep["n_ppi"], 10)
        self.assertEqual(rep["n_paired"], 10)
        # The series were built to co-move; change-correlation should be strong.
        self.assertGreater(rep["corr_changes"], 0.7)
        self.assertGreater(rep["direction_hit_rate"], 0.6)
        self.assertIn("lead_lag_corr", rep)

    def test_handles_sparse_index(self):
        # Index only starts halfway through the PPI history.
        _, ppi = self._series()
        idx = [("2026-06-15", 50.0), ("2026-09-15", 70.0)]
        rep = backtest.validate(idx, ppi)
        self.assertEqual(rep["n_ppi"], 10)     # all PPI prints seen
        self.assertEqual(rep["n_paired"], 5)   # index only overlaps Jun-Oct


if __name__ == "__main__":
    unittest.main(verbosity=2)
