import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from scripts.collect_research_modules import (
    flow_quality_status, is_eastmoney_rate_limit, number, replace_flow_period_rows, ths_industry_flow_rows,
)


class THSIndustryFlowNormalizationTests(unittest.TestCase):
    def test_eastmoney_rate_limit_opens_circuit_breaker(self):
        self.assertTrue(is_eastmoney_rate_limit(RuntimeError("Push2RateLimitError")))
        self.assertTrue(is_eastmoney_rate_limit(RuntimeError("HTTP 429 too many requests")))
        self.assertTrue(is_eastmoney_rate_limit(RuntimeError("短时间内请求过多")))
        self.assertFalse(is_eastmoney_rate_limit(RuntimeError("temporary timeout")))

    def test_current_flow_period_replacement_removes_stale_rows_only_in_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capital_flows.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    "trade_date", "indicator", "industry", "provider", "net_inflow_cny"
                ])
                writer.writeheader()
                writer.writerows([
                    {"trade_date": "2026-11-26", "indicator": "今日", "industry": "银行",
                     "provider": "EastMoney", "net_inflow_cny": 10},
                    {"trade_date": "2026-11-26", "indicator": "5日", "industry": "银行",
                     "provider": "EastMoney", "net_inflow_cny": 20},
                    {"trade_date": "2026-11-25", "indicator": "今日", "industry": "银行",
                     "provider": "THS", "net_inflow_cny": 30},
                ])
            replace_flow_period_rows(path, "2026-11-26", "今日", [{
                "trade_date": "2026-11-26", "indicator": "今日", "industry": "电子",
                "provider": "THS", "net_inflow_cny": 40,
            }])
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                {(row["trade_date"], row["indicator"], row["industry"]) for row in rows},
                {
                    ("2026-11-26", "今日", "电子"),
                    ("2026-11-26", "5日", "银行"),
                    ("2026-11-25", "今日", "银行"),
                },
            )
            replace_flow_period_rows(path, "2026-11-26", "今日", [])
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertNotIn(("2026-11-26", "今日", "电子"), {
                (row["trade_date"], row["indicator"], row["industry"]) for row in rows
            })

    def test_ths_flow_coverage_below_95_percent_is_partial(self):
        self.assertEqual(flow_quality_status({"coverage_pct": 94.99}), "partial")
        self.assertEqual(flow_quality_status({"coverage_pct": 100}), "success")
        self.assertEqual(flow_quality_status({"coverage_pct": None}), "partial")
        self.assertEqual(flow_quality_status({"coverage_pct": 100,
                                              "conflicting_duplicates": [{"industry": "银行"}]}),
                         "partial")

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

    def test_ths_normalizer_calculates_coverage_from_provider_universe(self):
        frame = pd.DataFrame([
            {"行业": "银行", "净额": 12.5, "涨跌幅": 1.2},
            {"行业": "电子", "净额": 4.2, "涨跌幅": 0.5},
        ])
        rows, diagnostics = ths_industry_flow_rows(
            frame, "10日", date(2026, 11, 26), "2026-11-27T10:00:00+08:00",
            expected_industry_names={"银行", "电子", "医药"},
            return_diagnostics=True,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(diagnostics["expected_industries"], 3)
        self.assertAlmostEqual(diagnostics["coverage_pct"], 66.67)

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
