import unittest

import pandas as pd

from scripts.smoke_research_providers import PERIOD_SYMBOLS, validate_ths_flow_frame


class ResearchProviderSmokeContractTests(unittest.TestCase):
    def test_period_symbols_are_independent_and_explicit(self):
        self.assertEqual(PERIOD_SYMBOLS, {
            "今日": "即时", "5日": "5日排行", "10日": "10日排行"
        })

    def test_validates_provider_schema_and_counts_only_finite_rows(self):
        frame = pd.DataFrame({
            "行业": ["银行", "电子", "无效"],
            "净额": ["12.5", "-3.2", "not-a-number"],
        })
        result = validate_ths_flow_frame(frame)
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["unique_industries"], 2)
        self.assertEqual(result["net_amount_column"], "净额")

    def test_rejects_empty_or_schema_drift(self):
        with self.assertRaises(ValueError):
            validate_ths_flow_frame(pd.DataFrame())
        with self.assertRaises(ValueError):
            validate_ths_flow_frame(pd.DataFrame({"板块": ["银行"], "主力净流入": [10]}))

    def test_rejects_rows_without_numeric_net_amount(self):
        with self.assertRaises(ValueError):
            validate_ths_flow_frame(pd.DataFrame({"行业": ["银行"], "净额": ["--"]}))


if __name__ == "__main__":
    unittest.main()
