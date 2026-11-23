import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_theme_evidence as collector


class ThemeHealthEvidenceTests(unittest.TestCase):
    def test_financial_breadth_requires_minimum_constituent_coverage(self):
        rows = [
            {"股票代码": "000001", "营业收入-同比增长": 12},
            {"股票代码": "000002", "营业收入-同比增长": -3},
        ]
        score, meta = collector._breadth_score(rows, {"000001", "000002", "000003"}, ("营业收入-同比增长",))
        self.assertIsNone(score)
        self.assertEqual(meta["required"], 3)

    def test_financial_breadth_scores_positive_share(self):
        rows = [
            {"股票代码": "000001", "净利润-同比增长": 10},
            {"股票代码": "000002", "净利润-同比增长": -5},
            {"股票代码": "000003", "净利润-同比增长": 0},
            {"股票代码": "000004", "净利润-同比增长": 20},
        ]
        score, meta = collector._breadth_score(rows, {"000001", "000002", "000003", "000004"},
            ("净利润-同比增长",))
        self.assertEqual(score, 50.0)
        self.assertEqual(meta["sample"], 4)

    def test_policy_requires_successful_official_full_text(self):
        score, _ = collector._policy_score({"records": [
            {"title": "半导体规划通知", "body_status": "not_fetched", "body_text": None}
        ]})
        self.assertIsNone(score)
        score, meta = collector._policy_score({"records": [
            {"title": "半导体规划通知", "body_status": "success", "body_text": "正式政策正文" * 30}
        ]})
        self.assertEqual(score, 75.0)
        self.assertEqual(meta["verified_documents"], 1)

    def test_missing_valuation_does_not_inflate_coverage(self):
        result = collector.model_score(1.0, 0.2, 0.3, {
            "constituent_codes": ["000001", "000002", "000003", "000004"],
            "financials": {"report_date": "2026-09-30", "records": [
                {"股票代码": "000001", "营业收入-同比增长": 10, "净利润-同比增长": 10},
                {"股票代码": "000002", "营业收入-同比增长": 10, "净利润-同比增长": 10},
                {"股票代码": "000003", "营业收入-同比增长": -5, "净利润-同比增长": -5},
                {"股票代码": "000004", "营业收入-同比增长": 10, "净利润-同比增长": 10},
            ]},
            "government_policy": {"records": []},
        })
        health = result["health"]
        self.assertEqual(health["coverage"], 75)
        self.assertIsNone(health["dimensions"]["valuation_crowding"])
        self.assertEqual(health["dimensions"]["industry_thesis"], 75.0)
        self.assertEqual(health["dimensions"]["earnings_delivery"], 75.0)


if __name__ == "__main__":
    unittest.main()
