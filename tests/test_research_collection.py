import unittest
from datetime import date

import pandas as pd

from scripts.collect_research_modules import number, ths_industry_flow_rows


class THSIndustryFlowNormalizationTests(unittest.TestCase):
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
