"""chip_advanced: 用 FinMind 文件格式的假資料驗證三個進階籌碼指標 (離線)。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.analysis import chip_advanced as ca  # noqa: E402

LEVELS = ["1-999", "1,000-5,000", "5,001-10,000", "800,001-1,000,000", "more than 1,000,001", "total"]


def _holding(pct_old, pct_new):
    rows = []
    for d, big in (("2026-10-30", pct_old), ("2026-11-20", pct_new)):
        for lv in LEVELS:
            rows.append({"date": d, "stock_id": "2330", "HoldingSharesLevel": lv, "people": 1,
                         "percent": big if lv.startswith("more") else (100.0 if lv == "total" else 5.0),
                         "unit": 1})
    return pd.DataFrame(rows)


class ChipAdvancedTests(unittest.TestCase):
    def setUp(self):
        ca._UNAVAILABLE.clear()

    def test_level_parser(self):
        self.assertEqual(ca._level_lower_bound("1,000-5,000"), 1000)
        self.assertEqual(ca._level_lower_bound("more than 1,000,001"), 1000001)
        self.assertEqual(ca._level_lower_bound("1,000,001以上"), 1000001)
        self.assertIsNone(ca._level_lower_bound("total"))

    def test_large_holders_uses_only_top_level(self):
        with patch.object(ca.ds, "_finmind_get", return_value=_holding(70.0, 72.5)) as g:
            r = ca.fetch_large_holders_change("2330")
        self.assertEqual(g.call_args.args[0], "TaiwanStockHoldingSharesPer")
        self.assertEqual(r["large_holder_pct_now"], 72.5)   # 不含 1,000-5,000 股的散戶級距
        self.assertEqual(r["trend"], "increasing")

    def test_lending_balance(self):
        df = pd.DataFrame({"date": pd.date_range("2026-11-10", periods=8).strftime("%Y-%m-%d"),
                           "SBLShortSalesCurrentDayBalance": [1000, 1000, 1000, 1000, 800, 700, 600, 500]})
        with patch.object(ca.ds, "_finmind_get", return_value=df) as g:
            r = ca.fetch_securities_lending("2330")
        self.assertEqual(g.call_args.args[0], "TaiwanDailyShortSaleBalances")
        self.assertEqual(r["signal"], "short_cover")

    def test_broker_shares_to_lots(self):
        df = pd.DataFrame([{"date": "2026-11-26", "securities_trader": "美林", "buy": 2_500_000, "sell": 500_000},
                           {"date": "2026-11-26", "securities_trader": "元大-台北", "buy": 0, "sell": 100_000}])
        with patch.object(ca.ds, "_finmind_get", return_value=df):
            r = ca.fetch_main_broker_flow("2330")
        self.assertEqual(r["foreign_proxy_net"], 2000)   # 2,000,000 股 = 2,000 張

    def test_paid_tier_error_disables_dataset_once(self):
        err = RuntimeError("FinMind API 回應錯誤: Your level is register. Please update your user level")
        with patch.object(ca.ds, "_finmind_get", side_effect=err) as g:
            self.assertEqual(ca.fetch_large_holders_change("1101"), {})
            self.assertEqual(ca.fetch_large_holders_change("1102"), {})
        self.assertEqual(g.call_count, 1)


if __name__ == "__main__":
    unittest.main()
