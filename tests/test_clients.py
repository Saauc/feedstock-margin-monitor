"""
test_clients.py — offline tests for the I/O modules, with HTTP mocked.

These cover the parsing/wiring of the data clients (Yahoo, EIA, FRED, the WTI
curve) and the backtest runner, without touching the network. We patch
`feedstock.http.get` to return canned payloads.
"""

import tempfile
import unittest
from unittest import mock

from feedstock import backtest, ingest, official_data, store, term_structure


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _yahoo_payload(closes, offset=-14400):
    return {"chart": {"result": [{
        "meta": {"gmtoffset": offset},
        "timestamp": [1718774400 + i * 86400 for i in range(len(closes))],
        "indicators": {"quote": [{"close": closes}]},
    }], "error": None}}


class TestYahoo(unittest.TestCase):
    @mock.patch("feedstock.http.get")
    def test_parses_latest_nonnull_close(self, get):
        get.return_value = FakeResp(_yahoo_payload([80.0, None, 81.5]))
        out = ingest.fetch_yahoo_metric("CL=F", "yahoo_finance", "wti_crude_usd_bbl")
        self.assertEqual(len(out), 1)
        _, source, metric, value = out[0]
        self.assertEqual((source, metric), ("yahoo_finance", "wti_crude_usd_bbl"))
        self.assertAlmostEqual(value, 81.5)

    @mock.patch("feedstock.http.get")
    def test_result_null_does_not_crash(self, get):
        # 200 with result: null -> TypeError internally, must return [].
        get.return_value = FakeResp({"chart": {"result": None, "error": {"code": "x"}}})
        self.assertEqual(
            ingest.fetch_yahoo_metric("BAD", "yahoo_finance", "x"), [])


class TestFredEia(unittest.TestCase):
    @mock.patch.dict("os.environ", {"FRED_API_KEY": "k"})
    @mock.patch("feedstock.http.get")
    def test_fred_history_skips_missing(self, get):
        get.return_value = FakeResp({"observations": [
            {"date": "2026-01-01", "value": "100.0"},
            {"date": "2026-02-01", "value": "."},     # missing -> skipped
            {"date": "2026-03-01", "value": "101.2"},
        ]})
        cfg = {"sources": {"fred": {"series": {"coatings_ppi": "PCU325510325510"}}}}
        out = official_data._fetch_fred(cfg, history=True)
        self.assertEqual([(d, v) for d, _, _, v in out],
                         [("2026-01-01", 100.0), ("2026-03-01", 101.2)])

    @mock.patch.dict("os.environ", {"EIA_API_KEY": "k"})
    @mock.patch("feedstock.http.get")
    def test_eia_latest_picks_most_recent(self, get):
        get.return_value = FakeResp({"response": {"data": [
            {"period": "2026-06-15", "value": 1.00},
            {"period": "2026-06-17", "value": 1.10},
        ]}})
        cfg = {"sources": {"eia": {"series": {"propane_x": "PET.X.D"}}}}
        out = official_data._fetch_eia(cfg, history=False)
        self.assertEqual(out, [("2026-06-17", "eia", "propane_x", 1.10)])

    @mock.patch.dict("os.environ", {}, clear=True)
    def test_no_key_skips(self):
        cfg = {"sources": {"fred": {"series": {"x": "y"}}}}
        self.assertEqual(official_data._fetch_fred(cfg), [])


class TestCurve(unittest.TestCase):
    @mock.patch("feedstock.http.get")
    def test_backwardation_detected_and_stored(self, get):
        # First call = front (CL=F), second = deferred contract.
        get.side_effect = [FakeResp(_yahoo_payload([76.5])),
                           FakeResp(_yahoo_payload([72.9]))]
        db = tempfile.mktemp(suffix=".db")
        store.init_db(db)
        conn = store.get_connection(db)
        curve = term_structure.fetch_and_store_curve(conn)
        self.assertEqual(curve["state"], "backwardation")
        self.assertLess(curve["roll_yield_ann_pct"], 0)
        rows = store.fetch_metric_history(conn, "wti_roll_yield_ann_pct")
        self.assertEqual(len(rows), 1)
        conn.close()


class TestBacktestRunner(unittest.TestCase):
    def test_run_backtest_persists_report(self):
        db = tempfile.mktemp(suffix=".db")
        store.init_db(db)
        conn = store.get_connection(db)
        months = [f"2026-{m:02d}-28" for m in range(1, 9)]
        idx = [40, 48, 56, 52, 60, 66, 70, 64]
        ppi = [100, 101, 103, 102, 104, 106, 107, 106]
        for d, v in zip(months, idx):
            store.record_observation(conn, d, "computed", "margin_pressure_index", v)
        for d, v in zip(months, ppi):
            store.record_observation(conn, d, "fred", "coatings_ppi", v)
        conn.commit()
        cfg = {"backtest": {"target_metric": "coatings_ppi", "max_lag_months": 2,
                            "min_paired": 4}}
        report = backtest.run_backtest(conn, cfg)
        self.assertIsNotNone(report)
        self.assertGreaterEqual(report["n_paired"], 4)
        self.assertIsNotNone(store.latest_backtest(conn))
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
