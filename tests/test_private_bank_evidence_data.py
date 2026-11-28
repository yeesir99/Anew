import json
import unittest
from pathlib import Path

from scripts.theme_mapping_rules import private_bank_records_for_trade_date


class PrivateBankEvidenceDataTests(unittest.TestCase):
    def test_checked_in_records_are_primary_sourced_and_match_market_date(self):
        path = Path(__file__).resolve().parents[1] / "data" / "private_bank_equity_evidence.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = private_bank_records_for_trade_date(
            payload.get("records", []), "2026-11-26"
        )
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["listed_company_code"], "601933")
        self.assertEqual(record["investee_bank_name"], "福建华通银行股份有限公司")
        self.assertEqual(record["holding_pct"], 29.8)
        self.assertEqual(record["source_type"], "semiannual_report")
        self.assertTrue(record["source_url"].startswith("https://static.cninfo.com.cn/"))
        self.assertTrue(record.get("cross_check_urls"))

    def test_evidence_carries_forward_to_next_trade_date_with_age_metadata(self):
        path = Path(__file__).resolve().parents[1] / "data" / "private_bank_equity_evidence.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = private_bank_records_for_trade_date(
            payload.get("records", []), "2026-11-27"
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["evidence_status"], "latest_disclosure_within_freshness_window")
        self.assertEqual(records[0]["review_age_days"], 1)
        self.assertGreater(records[0]["evidence_age_days"], 90)

    def test_evidence_expires_after_180_days_without_a_new_disclosure(self):
        path = Path(__file__).resolve().parents[1] / "data" / "private_bank_equity_evidence.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = private_bank_records_for_trade_date(
            payload.get("records", []), "2027-03-01"
        )
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
