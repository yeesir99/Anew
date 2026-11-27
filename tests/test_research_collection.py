import unittest
from datetime import date

import pandas as pd

from scripts.collect_research_modules import is_eastmoney_rate_limit, number, ths_industry_flow_rows


class THSIndustryFlowNormalizationTests(unittest.TestCase):
    def test_eastmoney_rate_limit_opens_circuit_breaker(self):
        self.assertTrue(is_eastmoney_rate_limit(RuntimeError("Push2RateLimitError")))
        self.assertTrue(is_eastmoney_rate_limit(RuntimeError("HTTP 429 too many requests")))
        self.assertTrue(is_eastmoney_rate_limit(RuntimeError("短时间内请求过多")))
        self.assertFalse(is_eastmoney_rate_limit(RuntimeError("temporary timeout")))

    def test_amount_suffix_is_parsed_without_losing_unit(self):
        self.assertEqual(number("12.5亿元"), 12.5)

    def test_ths_net_amount_is_converted_from_100m_cny_and_kept_distinct(self):
        frame = pd.DataFrame([
            {"行业": "银行", "净额": 12.5, "涨跌幅": 1.2},
            {"行业": "电子", "净额": -3.0, "涨跌幅": -0.5},
        ])
        rows = ths_industry_flow_rows(
            frame, "5日", date(2026, 11, 26), "2026-11-27T10:00:00+08:00"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["net_inflow_cny"], 1_250_000_000)
        self.assertIsNone(rows[0]["main_net_inflow_cny"])
        self.assertEqual(rows[0]["provider"], "THS")
        self.assertEqual(rows[0]["indicator"], "5日")
        self.assertIn("not relabelled as EastMoney", rows[0]["metric_definition"])

    def test_ths_normalizer_deduplicates_identical_industries(self):
        frame = pd.DataFrame([
            {"行业": "银行", "净额": 12.5, "涨跌幅": 1.2},
            {"行业": "银行", "净额": 12.5, "涨跌幅": 1.2},
        ])
        rows = ths_industry_flow_rows(
            frame, "5日", date(2026, 11, 26), "2026-11-27T10:00:00+08:00"
        )
        self.assertEqual(len(rows), 1)

    def test_ths_normalizer_rejects_conflicting_duplicate_industries(self):
        frame = pd.DataFrame([
            {"行业": "银行", "净额": 12.5, "涨跌幅": 1.2},
            {"行业": "银行", "净额": 13.0, "涨跌幅": 1.2},
        ])
        with self.assertRaises(RuntimeError):
            ths_industry_flow_rows(
                frame, "5日", date(2026, 11, 26), "2026-11-27T10:00:00+08:00"
            )

    def test_ths_normalizer_keeps_unambiguous_rows_and_reports_conflicts(self):
        frame = pd.DataFrame([
            {"行业": "银行", "净额": 12.5, "涨跌幅": 1.2},
            {"行业": "银行", "净额": 13.0, "涨跌幅": 1.2},
            {"行业": "电子", "净额": 4.2, "涨跌幅": 0.5},
        ])
        rows, diagnostics = ths_industry_flow_rows(
            frame, "今日", date(2026, 11, 26), "2026-11-27T10:00:00+08:00",
            return_diagnostics=True,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["industry"], "电子")
        self.assertEqual(diagnostics["records"], 1)
        self.assertEqual(diagnostics["raw_records"], 3)
        self.assertEqual(diagnostics["conflicting_duplicates"][0]["industry"], "银行")

    def test_ths_normalizer_rejects_empty_or_schema_drift(self):
        with self.assertRaises(RuntimeError):
            ths_industry_flow_rows(
                pd.DataFrame(), "今日", date(2026, 11, 26), "2026-11-27T10:00:00+08:00"
            )
        with self.assertRaises(RuntimeError):
            ths_industry_flow_rows(
                pd.DataFrame([{"板块": "银行", "主力净流入": 10}]),
                "今日", date(2026, 11, 26), "2026-11-27T10:00:00+08:00"
            )


if __name__ == "__main__":
    unittest.main()
