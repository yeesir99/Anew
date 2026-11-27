import unittest
from datetime import date

import pandas as pd

from scripts.smoke_research_providers import (
    PERIOD_SYMBOLS, smoke_exit_code, validate_ths_flow_frame, validate_ths_theme_concept,
)


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
        self.assertEqual(result["status"], "partial")

    def test_full_ths_flow_coverage_is_success(self):
        frame = FakeFrame(
            ["行业", "净额"],
            [{"行业": "银行", "净额": "12.5"}, {"行业": "电子", "净额": "4.2"}],
        )
        result = validate_ths_flow_frame(frame, universe_names={"银行", "电子"})
        self.assertEqual(result["coverage_pct"], 100.0)
        self.assertEqual(result["status"], "success")

    def test_ths_star_theme_requires_exact_board_and_one_year_listing_rule(self):
        class FakeAk:
            def stock_board_concept_name_ths(self):
                return pd.DataFrame([{"name": "科创次新股", "code": "885907"}])

            def stock_board_concept_cons_ths(self, symbol):
                return pd.DataFrame([{"代码": "688001"}, {"代码": "688002"}])

            def stock_ipo_info(self, symbol):
                return pd.DataFrame([{"item": "上市日期", "value": "2026-01-01"}])

        result = validate_ths_theme_concept(FakeAk(), "科创次新股", date(2026, 11, 26))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["eligible_within_one_calendar_year"], 2)

        class ExpiredAk(FakeAk):
            def stock_ipo_info(self, symbol):
                return pd.DataFrame([{"item": "上市日期", "value": "2025-10-01"}])

        expired = validate_ths_theme_concept(ExpiredAk(), "科创次新股", date(2026, 11, 26))
        self.assertEqual(expired["status"], "partial")
        self.assertEqual(expired["expired_or_outside_rule"], 2)

    def test_star_theme_smoke_can_bound_ipo_lookups_and_report_untested(self):
        class FakeAk:
            calls = 0

            def stock_board_concept_name_ths(self):
                return pd.DataFrame([{"name": "科创次新股", "code": "885907"}])

            def stock_board_concept_cons_ths(self, symbol):
                return pd.DataFrame([{"代码": "688001"}, {"代码": "688002"}])

            def stock_ipo_info(self, symbol):
                self.calls += 1
                return pd.DataFrame([{"item": "上市日期", "value": "2026-01-01"}])

        fake = FakeAk()
        result = validate_ths_theme_concept(
            fake, "科创次新股", date(2026, 11, 26), max_ipo_checks=1
        )
        self.assertEqual(fake.calls, 1)
        self.assertEqual(result["listing_checks_performed"], 1)
        self.assertEqual(result["untested_candidates"], 1)
        self.assertEqual(result["status"], "partial")

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
