import sys
from pathlib import Path
import unittest
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.collectors import _require_full_market_coverage


class FullMarketCoverageTests(unittest.TestCase):
    def test_rejects_small_nonempty_provider_response(self):
        frame = pd.DataFrame({"symbol": [f"{i:06d}" for i in range(6)]})
        with self.assertRaisesRegex(RuntimeError, "覆盖不足 6/2500"):
            _require_full_market_coverage(frame, "stock_zh_a_spot_sina")

    def test_accepts_full_universe_response(self):
        frame = pd.DataFrame({"symbol": [f"{i:06d}" for i in range(2500)]})
        result = _require_full_market_coverage(frame, "test-provider")
        self.assertEqual(len(result), 2500)

    def test_rejects_empty_response(self):
        with self.assertRaisesRegex(RuntimeError, "返回空数据"):
            _require_full_market_coverage(pd.DataFrame(), "test-provider")


if __name__ == "__main__":
    unittest.main()
