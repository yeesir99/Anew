import unittest

from scripts.research_provider_utils import fetch_eastmoney_concept_members


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise RuntimeError(self.error)

    def json(self):
        return self.payload


class EastMoneyConceptMirrorTests(unittest.TestCase):
    def test_exact_concept_uses_mirror_after_primary_host_502(self):
        calls = []

        def fake_get(url, params, headers, timeout):
            calls.append((url, dict(params)))
            if "82.push2.eastmoney.com" in url:
                return FakeResponse(error="502")
            if "79.push2.eastmoney.com" in url and params["fs"] == "m:90+t:3+f:!50":
                return FakeResponse({"data": {"diff": [
                    {"f12": "BK0999", "f14": "烟草"},
                ], "total": 1}})
            if "29.push2.eastmoney.com" in url and params["fs"] == "b:BK0999":
                return FakeResponse({"data": {"diff": [
                    {"f12": "600000", "f14": "浦发银行"},
                ], "total": 1}})
            return FakeResponse({"data": {"diff": [], "total": 0}})

        members, meta = fetch_eastmoney_concept_members("烟草", get=fake_get)
        self.assertEqual(members, [{"code": "600000", "name": "浦发银行"}])
        self.assertEqual(meta["provider"], "EastMoney")
        self.assertEqual(meta["taxonomy_node_id"], "BK0999")
        self.assertEqual(meta["matched_symbol"], "烟草")
        self.assertIn("79.push2.eastmoney.com", calls[1][0])
        self.assertTrue(meta["source_url"].startswith("https://quote.eastmoney.com/"))

    def test_no_exact_name_does_not_use_parent_concept(self):
        def fake_get(url, params, headers, timeout):
            return FakeResponse({"data": {"diff": [
                {"f12": "BK0001", "f14": "医药"},
            ], "total": 1}})

        with self.assertRaises(LookupError):
            fetch_eastmoney_concept_members("工业大麻", get=fake_get)

    def test_all_taxonomy_mirrors_failed_is_transport_error(self):
        def fake_get(url, params, headers, timeout):
            return FakeResponse(error="502")

        with self.assertRaisesRegex(RuntimeError, "mirrors failed"):
            fetch_eastmoney_concept_members("科创次新股", get=fake_get)


if __name__ == "__main__":
    unittest.main()
