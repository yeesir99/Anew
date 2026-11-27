import unittest
from datetime import date

import pandas as pd
import requests

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
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["eligible_within_one_calendar_year"], 2)
        self.assertEqual(result["expired_or_outside_rule"], 0)
        self.assertEqual(result["listing_lookup_failures"], [])
        self.assertEqual(result["untested_candidates"], 0)
        self.assertEqual(result["status"], "success")

        class ExpiredAk(FakeAk):
            def stock_ipo_info(self, symbol):
                return pd.DataFrame([{"item": "上市日期", "value": "2025-10-01"}])

        expired = validate_ths_theme_concept(ExpiredAk(), "科创次新股", date(2026, 11, 26))
        self.assertEqual(expired["status"], "partial")
        self.assertEqual(expired["expired_or_outside_rule"], 2)

    def test_ths_theme_constituents_use_live_html_page_when_akshare_wrapper_removed(self):
        class FakeAk:
            def stock_board_concept_name_ths(self):
                return pd.DataFrame([{"name": "烟草", "code": "885999"}])

        class FakeResponse:
            text = "<table><tr><th>代码</th><th>名称</th></tr><tr><td>600000</td><td>浦发银行</td></tr></table>"

            def raise_for_status(self):
                return None

        calls = []
        def fake_get(url, headers, timeout):
            calls.append(url)
            return FakeResponse()

        result = validate_ths_theme_concept(
            FakeAk(), "烟草", date(2026, 11, 26), http_get=fake_get
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["candidate_count"], 1)
        self.assertIn("885999", calls[0])
        self.assertIn("10jqka.com.cn", result["source_url"])

    def test_eastmoney_exact_concept_fallback_when_ths_page_is_forbidden(self):
        class FakeAk:
            def stock_board_concept_name_ths(self):
                return pd.DataFrame([{"name": "烟草", "code": "885999"}])

            def stock_board_concept_name_em(self):
                return pd.DataFrame([{"板块名称": "烟草", "板块代码": "BK0999"}])

            def stock_board_concept_cons_em(self, symbol):
                if symbol != "烟草":
                    raise AssertionError(f"unexpected exact board name: {symbol}")
                return pd.DataFrame([{"代码": "600000", "名称": "浦发银行"}])

        class ForbiddenResponse:
            text = ""

            def raise_for_status(self):
                raise RuntimeError("403 Forbidden")

        result = validate_ths_theme_concept(
            FakeAk(), "烟草", date(2026, 11, 26),
            http_get=lambda *args, **kwargs: ForbiddenResponse()
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["provider"], "EastMoney")
        self.assertEqual(result["candidate_count"], 1)
        self.assertIn("THS", result["fallback_detail"])
        self.assertIn("403", result["fallback_detail"])

    def test_ths_constituent_403_falls_back_to_exact_eastmoney_concept(self):
        class FakeAk:
            def stock_board_concept_name_ths(self):
                return pd.DataFrame([{"name": "烟草", "code": "885999"}])

            def stock_board_concept_name_em(self):
                return pd.DataFrame([{"板块名称": "烟草", "板块代码": "BK1234"}])

            def stock_board_concept_cons_em(self, symbol):
                self.symbol = symbol
                return pd.DataFrame([{"代码": "600000", "名称": "浦发银行"}])

        class ForbiddenResponse:
            text = ""

            def raise_for_status(self):
                raise requests.HTTPError("403 Forbidden")

        def forbidden_get(url, headers, timeout):
            return ForbiddenResponse()

        fake = FakeAk()
        result = validate_ths_theme_concept(
            fake, "烟草", date(2026, 11, 26), http_get=forbidden_get
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["provider"], "EastMoney")
        self.assertEqual(fake.symbol, "烟草")
        self.assertEqual(result["candidate_count"], 1)

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
