import unittest

import pandas as pd

from scripts.build_valuation_snapshot import build_snapshot


class BroadMarketValuationTests(unittest.TestCase):
    def frames(self, pe_date="20261126", pb_date="20261126"):
        pe = pd.DataFrame([{
            "date": pe_date,
            "middlePETTM": 31.2,
            "quantileInAllHistoryMiddlePeTtm": 0.72,
            "quantileInRecent10YearsMiddlePeTtm": 0.66,
            "middlePELYR": 28.1,
            "quantileInAllHistoryMiddlePeLyr": 0.70,
            "quantileInRecent10YearsMiddlePeLyr": 0.62,
        }])
        pb = pd.DataFrame([{
            "date": pb_date,
            "middlePB": 2.1,
            "quantileInAllHistoryMiddlePB": 0.55,
            "quantileInRecent10YearsMiddlePB": 0.48,
        }])
        return pe, pb

    def test_exact_trade_date_and_provider_quantiles_validate(self):
        pe, pb = self.frames()
        result = build_snapshot("2026-11-26", pe, pb)
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["scope"], "broad_market")
        self.assertEqual(result["coverage_pct"], 100.0)
        self.assertEqual(result["provider_dates"], {"pe": "20261126", "pb": "20261126"})

    def test_stale_pe_or_pb_never_validates_current_market(self):
        pe, pb = self.frames(pe_date="20261125")
        result = build_snapshot("20261126", pe, pb)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["provider_dates"]["pe"], "20261125")

    def test_quantile_outside_unit_interval_is_rejected(self):
        pe, pb = self.frames()
        pe.loc[0, "quantileInAllHistoryMiddlePeTtm"] = 72
        with self.assertRaises(ValueError):
            build_snapshot("20261126", pe, pb)

    def test_missing_required_source_field_is_rejected(self):
        pe, pb = self.frames()
        pe = pe.drop(columns=["middlePETTM"])
        with self.assertRaises(ValueError):
            build_snapshot("20261126", pe, pb)


if __name__ == "__main__":
    unittest.main()
