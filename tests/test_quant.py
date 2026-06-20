"""
test_quant.py — unit tests for the statistical layer (pure, offline).

    python3 -m unittest -v test_quant test_backtest
"""

import unittest

from feedstock import quant


class TestReturnsVol(unittest.TestCase):
    def test_pct_returns(self):
        r = quant.pct_returns([100, 110, 99])
        self.assertAlmostEqual(r[0], 0.10, places=6)
        self.assertAlmostEqual(r[1], -0.10, places=6)

    def test_ewma_needs_data(self):
        self.assertIsNone(quant.ewma_volatility([100]))
        self.assertIsNone(quant.ewma_volatility([100, 101]))  # 1 return < 2

    def test_ewma_higher_for_choppier_series(self):
        calm = [100, 100.2, 100.1, 100.3, 100.2, 100.4, 100.3]
        wild = [100, 104, 97, 103, 96, 105, 98]
        self.assertGreater(quant.ewma_volatility(wild), quant.ewma_volatility(calm))

    def test_ewma_from_returns_matches_price_path(self):
        prices = [100, 102, 99, 103, 101, 104]
        rets = quant.pct_returns(prices)
        self.assertAlmostEqual(quant.ewma_volatility(prices),
                               quant.ewma_vol_returns(rets), places=9)

    def test_ewma_returns_needs_data(self):
        self.assertIsNone(quant.ewma_vol_returns([0.01]))


class TestPercentile(unittest.TestCase):
    def test_extremes_and_median(self):
        s = [1, 2, 3, 4, 5]
        self.assertEqual(quant.historical_percentile(s, 5), 100.0)
        self.assertEqual(quant.historical_percentile(s, 1), 20.0)
        self.assertEqual(quant.historical_percentile(s, 3), 60.0)  # 3 of 5 <= 3

    def test_defaults_to_latest(self):
        self.assertEqual(quant.historical_percentile([10, 20, 30]), 100.0)

    def test_empty(self):
        self.assertIsNone(quant.historical_percentile([]))


class TestVolRegime(unittest.TestCase):
    def test_elevated_and_subdued(self):
        hist = [0.1, 0.12, 0.11, 0.13, 0.1, 0.12, 0.11, 0.1, 0.12, 0.13, 0.11]
        self.assertEqual(quant.vol_regime(0.30, hist), "elevated")
        self.assertEqual(quant.vol_regime(0.05, hist), "subdued")
        self.assertEqual(quant.vol_regime(0.115, hist), "normal")

    def test_unknown_without_history(self):
        self.assertEqual(quant.vol_regime(0.2, [0.1, 0.1]), "unknown")
        self.assertEqual(quant.vol_regime(None, list(range(20))), "unknown")


class TestCurve(unittest.TestCase):
    def test_backwardation(self):
        self.assertEqual(quant.curve_state(100, 95), "backwardation")
        roll = quant.roll_yield(100, 95, 6)
        self.assertAlmostEqual(roll, -0.10, places=6)  # -5% over 6m -> -10% ann

    def test_contango(self):
        self.assertEqual(quant.curve_state(95, 100), "contango")
        self.assertGreater(quant.roll_yield(95, 100, 6), 0)

    def test_roll_guards(self):
        self.assertIsNone(quant.roll_yield(0, 100, 6))
        self.assertIsNone(quant.roll_yield(100, 95, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
