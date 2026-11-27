import unittest

from scripts.smoke_research_providers import PERIOD_SYMBOLS, smoke_exit_code, validate_ths_flow_frame


class FakeFrame:
    def __init__(self, columns, rows):
        self.columns = columns
        self._rows = rows
        self.empty = not rows

    def iterrows(self):
        return enumerate(self._rows)


class ResearchProviderSmokeContractTests(unittest.TestCase):
    def test_flow_coverage_uses_exact_ths_industry_universe(self):
        frame = FakeFrame(
            ["行业", "净额"],
            [{"行业": "银行", "净额": "12.5"}, {"行业": "电子", "净额": "4.2"}],
        )
        result = validate_ths_flow_frame(frame, universe_names={"银行", "电子", "医药"})
        self.assertEqual(result["expected_industries"], 3)
        self.assertAlmostEqual(result["coverage_pct"], 66.67)
        self.assertEqual(result["status"], "success")

    def test_partial_provider_data_is_warning_but_schema_failure_is_error(self):
        self.assertEqual(smoke_exit_code([{"status": "success"}, {"status": "partial"}]), 0)
        self.assertEqual(smoke_exit_code([{"status": "success"}, {"status": "failed"}]), 1)

    def test_period_symbols_are_independent_and_explicit(self):
        self.assertEqual(PERIOD_SYMBOLS, {
            "今日": "即时", "5日": "5日排行", "10日": "10日排行"
        })

    def test_validates_provider_schema_and_counts_only_finite_rows(self):
        frame = FakeFrame(
            ["行业", "净额"],
            [
                {"行业": "银行", "净额": "12.5"},
                {"行业": "电子", "净额": "-3.2"},
                {"行业": "无效", "净额": "not-a-number"},
            ],
        )
        result = validate_ths_flow_frame(frame)
        self.assertEqual(result["records"], 2)
        self.assertEqual(result["unique_industries"], 2)
        self.assertEqual(result["net_amount_column"], "净额")

    def test_rejects_empty_or_schema_drift(self):
        with self.assertRaises(ValueError):
            validate_ths_flow_frame(FakeFrame([], []))
        with self.assertRaises(ValueError):
            validate_ths_flow_frame(FakeFrame(
                ["板块", "主力净流入"], [{"板块": "银行", "主力净流入": 10}]
            ))

    def test_deduplicates_identical_rows_and_marks_conflicting_names_partial(self):
        identical = FakeFrame(
            ["行业", "净额"],
            [{"行业": "银行", "净额": "12.5"}, {"行业": "银行", "净额": "12.5"}],
        )
        result = validate_ths_flow_frame(identical)
        self.assertEqual(result["raw_records"], 2)
        self.assertEqual(result["records"], 1)
        self.assertEqual(result["duplicate_rows_removed"], 1)
        conflicting = FakeFrame(
            ["行业", "净额"],
            [
                {"行业": "银行", "净额": "12.5"},
                {"行业": "银行", "净额": "13.0"},
                {"行业": "电子", "净额": "4.2"},
            ],
        )
        partial = validate_ths_flow_frame(conflicting)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["records"], 1)
        self.assertEqual(partial["conflicting_duplicates"][0]["industry"], "银行")

    def test_rejects_rows_without_numeric_net_amount(self):
        with self.assertRaises(ValueError):
            validate_ths_flow_frame(FakeFrame(
                ["行业", "净额"], [{"行业": "银行", "净额": "--"}]
            ))


if __name__ == "__main__":
    unittest.main()
