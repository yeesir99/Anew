import unittest

from scripts.theme_mapping_rules import (
    has_mapping_provenance,
    merge_exact_components,
    star_next_new_eligible,
    verified_private_bank_relationship,
)


class SpecialThemeMappingTests(unittest.TestCase):
    def test_composite_theme_requires_both_exact_components(self):
        complete = {
            "烟草": {"status": "success", "members": [{"code": "600001"}]},
            "工业大麻": {"status": "success", "members": [{"code": "000002"}]},
        }
        self.assertEqual(len(merge_exact_components(complete)), 2)
        incomplete = {**complete, "工业大麻": {"status": "failed", "members": []}}
        self.assertEqual(merge_exact_components(incomplete), [])

    def test_star_new_requires_explicit_positive_age_and_star_code(self):
        self.assertTrue(star_next_new_eligible("688001", "2026-01-01", "2026-01-20", 30))
        self.assertFalse(star_next_new_eligible("688001", "2025-01-01", "2026-01-20", 30))
        self.assertFalse(star_next_new_eligible("600001", "2026-01-01", "2026-01-20", 30))
        self.assertFalse(star_next_new_eligible("688001", "2026-01-01", "2026-01-20", 0))

    def test_private_bank_requires_primary_dated_equity_disclosure(self):
        valid = {
            "listed_company_code": "600001",
            "investee_bank_name": "某民营银行",
            "relationship_type": "equity_investment",
            "holding_pct": 5.0,
            "disclosure_date": "2026-03-31",
            "source_url": "https://example.com/annual-report",
            "source_type": "annual_report",
            "source_excerpt": "公司持有某民营银行股份，报告期末持股比例为百分之五，具体投资情况见年度报告附注。",
        }
        self.assertTrue(verified_private_bank_relationship(valid))
        self.assertFalse(verified_private_bank_relationship({**valid, "holding_pct": None}))
        self.assertFalse(verified_private_bank_relationship({**valid, "source_type": "concept_tag"}))

    def test_mapping_provenance_requires_source_symbol_and_date(self):
        self.assertTrue(has_mapping_provenance({
            "status": "success",
            "mapping_source": "Sina taxonomy",
            "matched_symbol": "某主题",
            "mapping_date": "2026-11-20",
        }))
        self.assertFalse(has_mapping_provenance({"status": "success", "mapping_source": "Sina taxonomy"}))


if __name__ == "__main__":
    unittest.main()
