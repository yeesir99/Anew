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
        self.technical = {"stocks": 5575, "coverage": {"ma5": 5575, "ma20": 5575}}

    def test_stale_flow_rows_do_not_satisfy_current_period(self):
        old_rows = [{
            "trade_date": "2026-11-19", "indicator": "5日",
            "main_net_inflow_cny": "100", "source": "EastMoney",
        } for _ in range(60)]
        ths = {"trade_date": "2026-11-20", "status": "success", "records": 60, "items": [{
            "industry_name": f"行业{i}", "main_net_inflow_cny": 100,
            "source": "THS",
        } for i in range(60)]}
        evidence = {"themes": {"主题甲": {
            "constituent_codes": ["600000"],
            "mapping": {"mapping_source": "Sina taxonomy", "matched_symbol": "主题甲", "mapping_date": "2026-11-20"},
        }}}
        report = evaluate_quality(self.market, self.research, old_rows, ths, evidence, self.technical)
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
                "main_net_inflow_cny": "100", "source": "verified-provider",
            } for _ in range(50))
        evidence = {"themes": {
            name: {"constituent_codes": ["600000"],
                   "mapping": {"mapping_source": "verified taxonomy", "matched_symbol": name, "mapping_date": "2026-11-20"}}
            for name in ("主题甲", "主题乙")
        }}
        valuation = {"status": "validated", "records": 5575}
        registry = {"sources": [
            {"source": "sector_flow_" + period, "status": "success", "records": 50, "coverage_pct": 100}
            for period in ("今日", "5日", "10日")
        ]}
        report = evaluate_quality(self.market, self.research, flows, {}, evidence,
                                  self.technical, valuation, registry)
        self.assertEqual(report["quality_level"], "L3_RESEARCH_COMPLETE")

    def test_invalid_market_is_l0(self):
        report = evaluate_quality({}, self.research, [], {}, {}, {}, {})
        self.assertEqual(report["quality_level"], "L0_INVALID")
        self.assertFalse(report["market_valid"])


if __name__ == "__main__":
    unittest.main()
