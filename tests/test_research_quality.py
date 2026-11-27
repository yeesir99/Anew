import unittest

from scripts.build_research_quality import evaluate_quality


class ResearchQualityTests(unittest.TestCase):
    def setUp(self):
        self.market = {
            "trade_date": "2026-11-20",
            "data_status": "validated",
            "coverage": {"valid_stock_count": 5575},
        }
        self.research = {"sections": {"themes": {"items": [
            {"industry_name": "主题甲"}, {"industry_name": "主题乙"}
        ]}}}
        self.technical = {"trade_date": "20261120", "status": "validated",
                          "stocks": 5575, "coverage": {
            key: 5575 for key in ("ma5", "ma10", "ma20", "ma60", "rsi14",
                                  "macd_dif", "macd_dea", "macd_hist", "volatility20")
        }}

    def test_stale_flow_rows_do_not_satisfy_current_period(self):
        old_rows = [{
            "trade_date": "2026-11-19", "indicator": "5日",
            "main_net_inflow_cny": "100", "source": "EastMoney",
        } for _ in range(60)]
        ths = {"trade_date": "2026-11-20", "status": "success", "records": 60, "items": [{
            "industry_name": f"行业{i}", "main_net_inflow_cny": 100,
            "source": "THS",
        } for i in range(60)]}
        evidence = {"market_trade_date": "2026-11-20", "themes": {"主题甲": {
            "constituent_codes": ["600000"], "constituent_count": 1,
            "mapping": {"status": "success", "mapping_source": "Sina taxonomy", "matched_symbol": "主题甲", "mapping_date": "2026-11-20"},
        }}}
        registry = {"trade_date": "2026-11-20", "sources": [
            {"source": "sector_flow_今日", "status": "failed", "detail": "EastMoney unavailable"}
        ]}
        report = evaluate_quality(self.market, self.research, old_rows, ths, evidence,
                                  self.technical, {}, registry)
        self.assertEqual(report["quality_level"], "L2_RESEARCH_DEGRADED")
        self.assertEqual(report["checks"]["capital_flow_今日"]["status"], "partial")
        self.assertTrue(report["checks"]["capital_flow_今日"]["available"])
        self.assertEqual(report["checks"]["capital_flow_5日"]["status"], "partial")
        self.assertFalse(report["checks"]["capital_flow_5日"]["available"])
        self.assertEqual(report["checks"]["theme_mapping"]["mapped"], 1)

    def test_complete_requires_all_dimensions(self):
        flows = []
        for period in ("今日", "5日", "10日"):
            flows.extend({
                "trade_date": "2026-11-20", "indicator": period,
                "main_net_inflow_cny": "100", "provider": "EastMoney", "source": "verified-provider",
            } for _ in range(50))
        evidence = {"market_trade_date": "2026-11-20", "themes": {
            name: {"constituent_codes": ["600000"], "constituent_count": 1,
                   "mapping": {"status": "success", "mapping_source": "verified taxonomy", "matched_symbol": name, "mapping_date": "2026-11-20"}}
            for name in ("主题甲", "主题乙")
        }}
        valuation = {"status": "validated", "trade_date": "2026-11-20",
                     "records": 5575, "coverage_pct": 100,
                     "source": "official valuation history", "provider": "Exchange",
                     "metric_definition": "PE/PB historical percentile"}
        registry = {"trade_date": "2026-11-20", "sources": [
            {"source": "sector_flow_" + period, "status": "success", "provider": "EastMoney", "records": 50, "coverage_pct": 100}
            for period in ("今日", "5日", "10日")
        ]}
        report = evaluate_quality(self.market, self.research, flows, {}, evidence,
                                  self.technical, valuation, registry)
        self.assertEqual(report["quality_level"], "L3_RESEARCH_COMPLETE")


    def test_ths_net_amount_fallback_is_available_but_not_main_net(self):
        ths = {
            "trade_date": "2026-11-20", "status": "success", "records": 90,
            "coverage_pct": 100,
            "items": [{
                "industry_name": f"行业{i}", "net_inflow_cny": 100,
                "main_net_inflow_cny": None, "metric_definition": "THS net amount",
                "source": "AKShare/THS live industry summary",
            } for i in range(90)],
        }
        registry = {"trade_date": "2026-11-20", "sources": [
            {"source": "sector_flow_今日", "status": "failed", "detail": "EastMoney timeout"}
        ]}
        report = evaluate_quality(self.market, self.research, [], ths, {}, self.technical,
                                  {}, registry)
        today = report["checks"]["capital_flow_今日"]
        self.assertEqual(today["status"], "validated")
        self.assertTrue(today["available"])
        self.assertEqual(today["fallback_provider"], "THS")
        self.assertEqual(today["records"], 90)

    def test_valuation_requires_dated_source_provider_and_coverage(self):
        valuation = {"status": "validated", "records": 5575}
        report = evaluate_quality(self.market, self.research, [], {}, {}, self.technical,
                                  valuation, {})
        check = report["checks"]["valuation_crowding"]
        self.assertEqual(check["status"], "unavailable")
        self.assertFalse(check["provenance_valid"])
        self.assertFalse(check["trade_date_matches_market"])

    def test_success_without_coverage_is_not_complete(self):
        flows = [{
            "trade_date": "2026-11-20", "indicator": "5日",
            "net_inflow_cny": "100", "source": "THS",
        } for _ in range(90)]
        registry = {"trade_date": "2026-11-20", "sources": [
            {"source": "sector_flow_5日", "status": "success", "provider": "THS", "records": 90}
        ]}
        report = evaluate_quality(self.market, self.research, flows, {}, {}, self.technical,
                                  {}, registry)
        check = report["checks"]["capital_flow_5日"]
        self.assertTrue(check["available"])
        self.assertEqual(check["status"], "partial")
        self.assertIsNone(check["coverage_pct"])


    def test_missing_registry_provider_cannot_validate_flow_rows(self):
        flows = [{
            "trade_date": "2026-11-20", "indicator": "5日",
            "main_net_inflow_cny": 10, "source": "unverified-source",
        }]
        registry = {"trade_date": "2026-11-20", "sources": [{
            "source": "sector_flow_5日", "status": "success",
            "records": 1, "coverage_pct": 100,
        }]}
        report = evaluate_quality(self.market, self.research, flows, {}, {},
                                  self.technical, {}, registry)
        check = report["checks"]["capital_flow_5日"]
        self.assertFalse(check["provider_contract_valid"])
        self.assertFalse(check["available"])
        self.assertEqual(check["records"], 0)

    def test_current_provider_excludes_retained_rows_from_previous_provider(self):
        flows = [{
            "trade_date": "2026-11-20", "indicator": "5日",
            "main_net_inflow_cny": 10, "provider": "EastMoney",
            "source": "AKShare/EastMoney public estimate",
        } for _ in range(20)]
        flows.extend({
            "trade_date": "2026-11-20", "indicator": "5日",
            "net_inflow_cny": 10, "provider": "THS",
            "source": "AKShare/THS industry fund-flow ranking",
        } for _ in range(90))
        registry = {"trade_date": "2026-11-20", "sources": [{
            "source": "sector_flow_5日", "status": "success",
            "provider": "THS", "records": 90, "coverage_pct": 100,
        }]}
        report = evaluate_quality(self.market, self.research, flows, {}, {},
                                  self.technical, {}, registry)
        check = report["checks"]["capital_flow_5日"]
        self.assertEqual(check["status"], "validated")
        self.assertEqual(check["records"], 90)
        self.assertEqual(check["providers"], ["AKShare/THS industry fund-flow ranking"])

    def test_mapping_requires_source_symbol_and_date(self):
        evidence = {"market_trade_date": "2026-11-20", "themes": {
            "主题甲": {"constituent_codes": ["600000"], "constituent_count": 1,
                       "mapping": {"status": "success", "mapping_source": "taxonomy only"}},
            "主题乙": {"constituent_codes": ["000001"], "constituent_count": 1,
                       "mapping": {"status": "success", "mapping_source": "taxonomy",
                                   "matched_symbol": "主题乙",
                                   "mapping_date": "2026-11-20"}},
        }}
        report = evaluate_quality(self.market, self.research, [], {}, evidence, self.technical)
        self.assertEqual(report["checks"]["theme_mapping"]["mapped"], 1)
        self.assertEqual(report["checks"]["theme_mapping"]["unresolved"], ["主题甲"])

    def test_stale_market_snapshot_is_not_market_valid(self):
        market = {**self.market, "data_status": "stale"}
        report = evaluate_quality(market, self.research, [], {}, {}, self.technical)
        self.assertEqual(report["quality_level"], "L0_INVALID")

    def test_flow_rows_must_match_current_registry_record_count(self):
        flows = [{
            "trade_date": "2026-11-20", "indicator": "5日",
            "main_net_inflow_cny": 100, "provider": "EastMoney",
            "source": "AKShare/EastMoney public estimate",
        }]
        registry = {"trade_date": "2026-11-20", "sources": [{
            "source": "sector_flow_5日", "status": "success",
            "provider": "EastMoney", "records": 90, "coverage_pct": 100,
        }]}
        report = evaluate_quality(self.market, self.research, flows, {}, {}, self.technical,
                                  {}, registry)
        check = report["checks"]["capital_flow_5日"]
        self.assertFalse(check["available"])
        self.assertFalse(check["record_count_matches"])
        self.assertEqual(check["records"], 0)

    def test_stale_theme_evidence_does_not_validate_current_mapping(self):
        evidence = {"market_trade_date": "2026-11-19", "themes": {
            "主题甲": {"constituent_codes": ["600000"], "constituent_count": 1,
                      "mapping": {"status": "success", "mapping_source": "taxonomy",
                                  "matched_symbol": "主题甲", "mapping_date": "2026-11-19"}},
            "主题乙": {"constituent_codes": ["000001"], "constituent_count": 1,
                      "mapping": {"status": "success", "mapping_source": "taxonomy",
                                  "matched_symbol": "主题乙", "mapping_date": "2026-11-19"}},
        }}
        report = evaluate_quality(self.market, self.research, [], {}, evidence, self.technical)
        check = report["checks"]["theme_mapping"]
        self.assertEqual(check["status"], "partial")
        self.assertFalse(check["trade_date_matches_market"])
        self.assertEqual(check["mapped"], 0)
        self.assertEqual(len(check["unresolved"]), 2)

    def test_stale_technical_snapshot_does_not_validate_current_market(self):
        technical = {**self.technical, "trade_date": "20261119"}
        report = evaluate_quality(self.market, self.research, [], {}, {}, technical)
        check = report["checks"]["technical_indicators"]
        self.assertEqual(check["status"], "partial")
        self.assertFalse(check["trade_date_matches_market"])
        self.assertEqual(check["technical_snapshot_trade_date"], "20261119")

    def test_unvalidated_technical_snapshot_does_not_validate_current_market(self):
        technical = {**self.technical, "status": "degraded"}
        report = evaluate_quality(self.market, self.research, [], {}, {}, technical)
        check = report["checks"]["technical_indicators"]
        self.assertEqual(check["status"], "partial")
        self.assertEqual(check["technical_snapshot_status"], "degraded")

    def test_technical_coverage_uses_market_universe_as_denominator(self):
        technical = {"stocks": 100, "coverage": {
            key: 100 for key in ("ma5", "ma10", "ma20", "ma60", "rsi14",
                                 "macd_dif", "macd_dea", "macd_hist", "volatility20")
        }}
        report = evaluate_quality(self.market, self.research, [], {}, {}, technical)
        check = report["checks"]["technical_indicators"]
        self.assertEqual(check["status"], "partial")
        self.assertEqual(check["stocks"], 5575)
        self.assertFalse(check["universe_matches_market"])
        self.assertEqual(check["coverage_pct"], 1.8)

    def test_invalid_market_is_l0(self):
        report = evaluate_quality({}, self.research, [], {}, {}, {}, {})
        self.assertEqual(report["quality_level"], "L0_INVALID")
        self.assertFalse(report["market_valid"])


if __name__ == "__main__":
    unittest.main()
