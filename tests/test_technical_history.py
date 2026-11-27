import unittest

import pandas as pd

from scripts.build_market_technicals import _extend_qfq_history


class QFQSnapshotExtensionTests(unittest.TestCase):
    def setUp(self):
        self.cache = pd.DataFrame([
            {"ts_code": "000001.SZ", "trade_date": "20261120", "close": 10.0, "vol": 100},
        ])

    def test_extends_cached_qfq_series_using_daily_return_ratios(self):
        bars = pd.DataFrame([
            {"ts_code": "000001.SZ", "trade_date": "20261120",
             "close_raw": 10.0, "pre_close_raw": 9.8, "vol": 100},
            {"ts_code": "000001.SZ", "trade_date": "20261123",
             "close_raw": 10.2, "pre_close_raw": 10.0, "vol": 110},
            {"ts_code": "000001.SZ", "trade_date": "20261124",
             "close_raw": 10.1, "pre_close_raw": 10.2, "vol": 120},
            {"ts_code": "000001.SZ", "trade_date": "20261125",
             "close_raw": 10.3, "pre_close_raw": 10.1, "vol": 130},
            {"ts_code": "000001.SZ", "trade_date": "20261126",
             "close_raw": 10.5, "pre_close_raw": 10.3, "vol": 140},
        ])
        extended, count = _extend_qfq_history(self.cache, bars, "20261126")
        self.assertEqual(count, 1)
        current = extended[extended["trade_date"] == "20261126"].iloc[0]
        self.assertAlmostEqual(current["close"], 10.5)
        self.assertEqual(current["vol"], 140)

    def test_does_not_bridge_missing_snapshot_sessions(self):
        bars = pd.DataFrame([
            {"ts_code": "000001.SZ", "trade_date": "20261120",
             "close_raw": 10.0, "pre_close_raw": 9.8, "vol": 100},
            {"ts_code": "000001.SZ", "trade_date": "20261123",
             "close_raw": 10.2, "pre_close_raw": 10.0, "vol": 110},
            # 20261124 is absent; pre_close on 20261125 proves the gap.
            {"ts_code": "000001.SZ", "trade_date": "20261125",
             "close_raw": 10.3, "pre_close_raw": 10.2, "vol": 130},
            {"ts_code": "000001.SZ", "trade_date": "20261126",
             "close_raw": 10.5, "pre_close_raw": 10.3, "vol": 140},
        ])
        extended, count = _extend_qfq_history(self.cache, bars, "20261126")
        self.assertEqual(count, 1)
        self.assertNotIn("20261125", set(extended["trade_date"]))
        self.assertNotIn("20261126", set(extended["trade_date"]))

    def test_does_not_mix_symbols_or_overwrite_existing_latest_rows(self):
        cache = pd.DataFrame([
            {"ts_code": "000001.SZ", "trade_date": "20261126", "close": 10.5, "vol": 140},
        ])
        bars = pd.DataFrame([
            {"ts_code": "000001.SZ", "trade_date": "20261126",
             "close_raw": 10.5, "pre_close_raw": 10.3, "vol": 140},
            {"ts_code": "000002.SZ", "trade_date": "20261120",
             "close_raw": 5.0, "pre_close_raw": 4.9, "vol": 90},
            {"ts_code": "000002.SZ", "trade_date": "20261123",
             "close_raw": 5.1, "pre_close_raw": 5.0, "vol": 95},
        ])
        extended, count = _extend_qfq_history(cache, bars, "20261126")
        self.assertEqual(count, 0)
        self.assertEqual(len(extended), 1)


if __name__ == "__main__":
    unittest.main()
