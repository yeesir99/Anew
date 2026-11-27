import unittest

from scripts.research_provider_utils import fetch_baidu_concept_tags, fetch_eastmoney_concept_members


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
            if params["fs"] == "b:BK0999":
                return FakeResponse({"data": {"diff": [
                    {"f12": "600000", "f14": "浦发银行"},
                ], "total": 1}})
            return FakeResponse({"data": {"diff": [], "total": 0}})

        members, meta = fetch_eastmoney_concept_members("烟草", get=fake_get)
        self.assertEqual(members, [{"code": "600000", "name": "浦发银行"}])
        self.assertEqual(meta["provider"], "EastMoney")
        self.assertEqual(meta["taxonomy_node_id"], "BK0999")
        self.assertEqual(meta["matched_symbol"], "烟草")
        self.assertTrue(any("79.push2.eastmoney.com" in url for url, _ in calls))
        self.assertTrue(meta["source_url"].startswith("https://quote.eastmoney.com/"))

    def test_no_exact_name_does_not_use_parent_concept(self):
        def fake_get(url, params, headers, timeout):
            return FakeResponse({"data": {"diff": [
                {"f12": "BK0001", "f14": "医药"},
            ], "total": 1}})

        with self.assertRaises(LookupError):
            fetch_eastmoney_concept_members("工业大麻", get=fake_get)

    def test_baidu_per_stock_concept_tags_are_batched_and_parsed_exactly(self):
        def fake_get(url, params, headers, timeout):
            import json
            stocks = json.loads(params["stock"])
            result = {}
            for item in stocks:
                code = item["code"]
                result[code] = [{"name": "概念", "list": [
                    {"name": "烟草", "xcx_query": "code=11062228"},
                    {"name": "其他概念", "xcx_query": "code=11000000"},
                ]}]
            return FakeResponse({"Result": result})

        tags, meta = fetch_baidu_concept_tags(
            ["600000", "000001"], get=fake_get, batch_size=1, max_workers=1
        )
        self.assertEqual(set(tags), {"000001", "600000"})
        self.assertEqual(tags["600000"][0]["name"], "烟草")
        self.assertEqual(tags["600000"][0]["concept_code"], "11062228")
        self.assertEqual(meta["coverage_pct"], 100.0)
        self.assertEqual(meta["status"], "success")
        self.assertEqual(meta["batch_count"], 2)

    def test_baidu_batch_size_respects_provider_limit(self):
        with self.assertRaises(ValueError):
            fetch_baidu_concept_tags(["600000"], batch_size=21, get=lambda *a, **k: None)

    def test_baidu_all_batch_failures_are_unavailable(self):
        def fake_get(url, params, headers, timeout):
            return FakeResponse(error="connection timeout")

        tags, meta = fetch_baidu_concept_tags(
            ["600000", "000001"], get=fake_get, batch_size=2, max_workers=1
        )
        self.assertEqual(tags, {})
        self.assertEqual(meta["status"], "unavailable")
        self.assertEqual(len(meta["failed_batches"]), 1)

    def test_all_taxonomy_mirrors_failed_is_transport_error(self):
        def fake_get(url, params, headers, timeout):
            return FakeResponse(error="502")

        with self.assertRaisesRegex(RuntimeError, "mirrors failed"):
            fetch_eastmoney_concept_members("科创次新股", get=fake_get)


if __name__ == "__main__":
    unittest.main()
