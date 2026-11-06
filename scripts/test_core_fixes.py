"""Offline regression tests for the 2026-11 bug-fix round (no network, no Telegram)."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.core import schedule_guard as sg  # noqa: E402
from radar.core import text_match as tm  # noqa: E402
from radar.core import watchlist_store as ws  # noqa: E402


class StateStoreTests(unittest.TestCase):
    """load → (別的模組寫了別的 key) → save 舊快照, 不可把別人剛寫的 key 回滾。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "monitor_state.json"
        self.file.write_text(json.dumps({"secondary_push_cap": {"n": 1}, "mine": {}}), encoding="utf-8")
        self.p1 = patch.object(ws, "MONITOR_STATE_FILE", self.file)
        self.p2 = patch.object(ws, "_get_sheet", return_value=None)
        self.p1.start(); self.p2.start()

    def tearDown(self):
        self.p1.stop(); self.p2.stop(); self.tmp.cleanup()

    def _disk(self):
        return json.loads(self.file.read_text(encoding="utf-8"))

    def test_stale_snapshot_does_not_roll_back_other_keys(self):
        mine = ws.load_monitor_state()            # 呼叫端先 load
        other = ws.load_monitor_state()           # 期間別的模組 (push_cap) 更新自己的 key
        other["secondary_push_cap"] = {"n": 2}
        ws.save_monitor_state(other)
        mine["mine"] = {"sent": ["x"]}            # 呼叫端只改自己的 key 再存回整包舊快照
        ws.save_monitor_state(mine)
        d = self._disk()
        self.assertEqual(d["secondary_push_cap"], {"n": 2})   # 修正前會被回滾成 1
        self.assertEqual(d["mine"], {"sent": ["x"]})

    def test_unchanged_state_is_not_rewritten(self):
        s = ws.load_monitor_state()
        before = self.file.stat().st_mtime_ns
        ws.save_monitor_state(s)
        self.assertEqual(self.file.stat().st_mtime_ns, before)

    def test_batch_mode_keeps_keys_written_inside_batch(self):
        ws.open_batched_state()
        try:
            a = ws.load_monitor_state()
            b = ws.load_monitor_state()
            b["alert_dedup"] = {"2330|up": "t"}
            ws.save_monitor_state(b)
            a["mine"] = {"v": 1}
            ws.save_monitor_state(a)
        finally:
            ws.close_batched_state()
        d = self._disk()
        self.assertEqual(d["alert_dedup"], {"2330|up": "t"})
        self.assertEqual(d["mine"], {"v": 1})

    def test_shrink_to_fit_keeps_blob_under_cell_limit(self):
        big = {"push_history": [{"ts": "x" * 90}] * 500,
               "signals": [{"hit": True, "pad": "y" * 300} for _ in range(400)]
                          + [{"hit": None, "pad": "z" * 300} for _ in range(20)]}
        blob = ws._shrink_to_fit(big, json.dumps(big))
        self.assertLessEqual(len(blob), ws._STATE_SOFT_LIMIT)
        self.assertEqual(len(big["push_history"]), 150)
        self.assertEqual(sum(1 for r in big["signals"] if r["hit"] is None), 20)  # 未驗收的保留


class ScheduleDateTests(unittest.TestCase):
    def test_intended_market_date_uses_scheduled_time(self):
        env = {"GITHUB_EVENT_NAME": "schedule", "SCHEDULE_CRON": "2 23 * * 1-5"}
        # 週五 18:02 EST 的 us_close, 晚 6.5 小時在 UTC 週六 05:32 才跑 → 仍是週五
        late = dt.datetime(2026, 11, 14, 5, 32)
        self.assertEqual(sg.intended_market_date("US", late, env), dt.date(2026, 11, 13))
        env_tw = {"GITHUB_EVENT_NAME": "schedule", "SCHEDULE_CRON": "32 14 * * *"}
        # 22:32 TPE 的 holiday_news 晚 3 小時 → 台北日期仍是排定當天
        self.assertEqual(sg.intended_market_date("TW", dt.datetime(2026, 11, 6, 17, 40), env_tw),
                         dt.date(2026, 11, 6))
        self.assertIsNone(sg.intended_market_date("US", late, {"GITHUB_EVENT_NAME": "workflow_dispatch"}))


class TextMatchTests(unittest.TestCase):
    def test_word_boundaries(self):
        self.assertFalse(tm.kw_hit("deal", "an ideal setup"))
        self.assertFalse(tm.kw_hit("settled", "Markets unsettled ahead of CPI"))
        self.assertFalse(tm.kw_hit("ft", "Microsoft Start"))
        self.assertFalse(tm.kw_hit("ai", "X Retail Holdings"))
        self.assertTrue(tm.kw_hit("tariff", "new tariffs announced"))
        self.assertTrue(tm.kw_hit("併購", "宣布併購案"))

    def test_overlapping_keywords_count_once(self):
        found = tm.hits(["trump", "tariff", "tariffs", "china tariff"], "Trump's China tariffs")
        self.assertEqual(tm.distinct_concepts(found), ["trump", "china tariff"])


class AnalysisFixTests(unittest.TestCase):
    def test_follower_band_on_down_day(self):
        from radar.analysis import sector_role_classifier as src
        lo, hi = sorted((-2 * 0.5, -2 * 1.5))
        self.assertTrue(lo <= -2 <= hi)
        self.assertTrue(hasattr(src, "FOLLOWER_VOL_RATIO"))

    def test_failed_entry_evaluation_abstains(self):
        from radar.analysis import ensemble_voter as ev
        with patch("radar.analysis.entry_evaluator.quick_evaluate",
                   return_value={"entry_label": "—", "entry_score": None}):
            self.assertEqual(ev._vote_entry_evaluator("2330", "TW")["vote"], "—")

    def test_buffett_vote_uses_producer_scale(self):
        from radar.analysis import ensemble_voter as ev
        res = {"score": 9, "reasons": ["ok"], "roe": 30.0, "debt_equity": 0.2,
               "fcf_yield": 5.0, "profit_margin": 25.0}
        with patch("radar.screeners.buffett_quality_filter.check_quality", return_value=res):
            self.assertEqual(ev._vote_buffett("AAPL", "US")["vote"], "BUY")
        empty = {"score": 0, "reasons": [], "roe": None, "debt_equity": None,
                 "fcf_yield": None, "profit_margin": None}
        with patch("radar.screeners.buffett_quality_filter.check_quality", return_value=empty):
            self.assertEqual(ev._vote_buffett("AAPL", "US")["vote"], "—")

    def test_policy_regime_without_benchmark(self):
        from radar.analysis import policy_regime as pr

        def fake(symbol, **_):
            if symbol in ("SPY", "0050.TW"):
                return pd.DataFrame()
            return pd.DataFrame({"Date": range(41), "Close": [100.0] * 40 + [110.0]})
        with patch("radar.data.data_sources.fetch_yf_history", side_effect=fake):
            rows = pr.scan_policy_themes()
        self.assertTrue(all(r["verdict"] in ("無基準", "無資料") for r in rows))

    def test_liquidity_sort(self):
        from radar.data import data_sources as ds
        info = pd.DataFrame({"stock_id": ["1101", "2330", "2454"]})
        rank = pd.DataFrame({"stock_id": ["2330", "2454"], "avg_amount": [9, 8]})
        with patch.object(ds, "fetch_tw_liquidity_rank", return_value=rank):
            self.assertEqual(ds.sort_by_liquidity(info)["stock_id"].tolist(), ["2330", "2454", "1101"])
        with patch.object(ds, "fetch_tw_liquidity_rank", return_value=pd.DataFrame()):
            self.assertEqual(ds.sort_by_liquidity(info)["stock_id"].tolist(), ["1101", "2330", "2454"])


class DataSchemaTests(unittest.TestCase):
    """依 2026-11 實際抓到的 FinMind / MoneyDJ 回應格式寫的回歸測試 (資料是縮小版的合成樣本)。"""

    def test_active_etf_parses_html_table(self):
        from radar.alerts import active_etf_monitor as m
        rows = "".join(
            f"<tr><td class=\"col05\"><a href='/etf/ea/ETZCW.djhtm?c=1&etfid={sid}.TW&back=00981A.TW'>"
            f"{nm}({sid}.TW)</a></td><td class=\"col06\">{pct}</td><td class=\"col07\">{sh}</td></tr>"
            for sid, nm, pct, sh in [("2330", "台積電", "14.27", "15,680,000.00"),
                                     ("2454", "聯發科", "7.89", "5,615,000.00"),
                                     ("2383", "台光電", "7.21", "4,328,000.00"),
                                     ("3017", "奇鋐", "6.93", "6,655,000.00"),
                                     ("3037", "欣興", "6.42", "19,739,000.00"),
                                     ("3653", "健策", "5.92", "2,512,000.00")])
        page = ("<html><head><meta charset=\"big5\"></head><body><div>持股明細</div>"
                "<div>資料日期：2026/11/05</div><table class=\"datalist\"><tbody>" + rows +
                "</tbody></table></body></html>").encode("big5")
        r = m.parse_holdings_html(m._decode_page(page, "ISO-8859-1"))
        self.assertEqual(r["data_date"], "2026/11/05")
        self.assertEqual(r["stocks"]["2330"], {"name": "台積電", "pct": 14.27, "shares": 15680000.0})
        self.assertEqual(len(r["stocks"]), 6)

    def test_shares_outstanding_uses_ordinary_share(self):
        from radar.data import data_sources as ds
        bs = pd.DataFrame([
            {"date": "2026-06-30", "stock_id": "2330", "type": "OrdinaryShare", "value": 259323701000.0},
            {"date": "2026-06-30", "stock_id": "2330", "type": "OrdinaryShare_per", "value": 4.2},
            {"date": "2026-06-30", "stock_id": "2330", "type": "CapitalStock", "value": 259323701000.0},
        ])
        fn = getattr(ds.fetch_shares_outstanding, "__wrapped__", ds.fetch_shares_outstanding)
        with patch.object(ds, "_fetch_universe", return_value=bs):
            self.assertAlmostEqual(fn(("2330",))["2330"], 25932370.1, places=1)

    def test_long_targets_never_below_price(self):
        from radar.analysis import indicators as ind
        with patch.object(ind, "fibonacci_extension_targets",
                          return_value={"fib_127": 135.4, "fib_162": 152.4, "fib_262": 200.0}), \
             patch.object(ind, "measured_move_target", return_value={"target": 170.0}):
            t = ind.tiered_long_targets(pd.Series([1.0] * 300), current=150, target_short=155)
        self.assertEqual(t, {"target_mid": 170.0, "target_long": 200.0})


if __name__ == "__main__":
    unittest.main()
