"""Run real Streamlit pages offline with mocked data/API and isolated local state.

Run: python -X utf8 -m unittest discover -s scripts -p test_dashboard_ui.py -v
"""
import contextlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from streamlit.testing.v1 import AppTest


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(contextlib.chdir(tmp))
        # No network, credentials, Telegram delivery, or production state writes.
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Network disabled")))
        self.stack.enter_context(patch("curl_cffi.requests.Session.request", side_effect=AssertionError("Network disabled")))
        mocks = {
            "data_sources._secret": "", "data_sources.list_secret_keys": [],
            "data_sources.fetch_yf_history": pd.DataFrame(),
            "data_sources.fetch_tw_market_pulse": {},
            "data_sources.get_taiwan_stock_info": pd.DataFrame(),
            "watchlist_store.load_watchlist": [],
            "watchlist_store.load_watchlist_ids": [],
            "watchlist_store.load_monitor_state": {},
            "holdings_store.load_holdings": [],
            "holdings_intraday_alert.check_holdings_intraday_risk": [],
            "sector_rotation.compute_sector_rotation": {},
            "system_health.record_dashboard_open": None,
            "system_health.collect_health": {"cron_health": {"status": "Test"}},
            "signal_tracker.accuracy_summary": {"n": 0},
            "tracker.csv_for_download": "",
            "notifier.is_configured": True,
        }
        for name, value in mocks.items():
            self.stack.enter_context(patch(name, return_value=value))
        import notifier
        self.real_send = notifier.send_message
        self.send = self.stack.enter_context(patch("notifier.send_message", return_value=(True, "test sent")))
        self.app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
        self.run_app()

    def run_app(self):
        self.app.run()
        self.assertEqual([e.message for e in self.app.exception], [])

    def click(self, *, key=None, label=None):
        button = self.app.button(key=key) if key else next(b for b in self.app.button if b.label == label)
        button.click()
        self.run_app()

    def test_all_twelve_outer_tabs_render(self):
        self.assertEqual(len(self.app.tabs), 14)  # 12 outer + backtest/tracking
        self.assertTrue(any("系統健康" in t.label for t in self.app.tabs))
        self.assertFalse(any("FinMind 套件沒裝" in e.value for e in self.app.error))

    def test_tw_top10_button_reaches_http_with_all_ten_stocks(self):
        import notifier
        picks = [{"stock_id": str(2300 + i), "name": "Test & Co", "theme": "AI",
                  "score": 7, "rr": "2.5", "current": 100,
                  "entry_low": 99, "entry_high": 101, "target": 110, "stop": 95,
                  "entry_timing_label": "等回測 < 100 & 再確認 " + "🎯" * 200}
                 for i in range(10)]
        with patch("actionable_picks.compute_actionable_picks", return_value=picks) as scan:
            self.click(key="actionable_load")
            with patch("notifier.send_message", wraps=self.real_send), \
                 patch("notifier._bot_token", return_value="fake"), \
                 patch("notifier._chat_id", return_value="fake"), \
                 patch("notifier._record_push_safe"), \
                 patch("requests.post", return_value=types.SimpleNamespace(status_code=200)) as post:
                self.click(key="actionable_tg")
                self.assertGreater(post.call_count, 1)
                payloads = [c.kwargs["data"]["text"] for c in post.call_args_list]
                self.assertTrue(all(notifier._u16_len(msg) <= 4000 for msg in payloads))
                sent = "".join(payloads)
                for pick in picks:
                    self.assertIn(pick["stock_id"], sent)
                self.assertIn("&lt; 100 &amp;", sent)
            scan.assert_called_once()  # 發送不重跑分析
        self.assertTrue(self.app.session_state["actionable_tg_result"]["ok"])

    def test_tw_top10_empty_missing_config_and_failure_have_visible_status(self):
        self.click(key="actionable_tg")
        self.send.assert_not_called()
        self.assertEqual(self.app.session_state["actionable_tg_result"]["level"], "warning")
        self.app.session_state["actionable_picks_cache"] = [{"stock_id": "2330", "score": None}]
        with patch("notifier.is_configured", return_value=False):
            self.click(key="actionable_tg")
        self.send.assert_not_called()
        self.assertIn("TELEGRAM_BOT_TOKEN", self.app.session_state["actionable_tg_result"]["detail"])
        self.send.return_value = (False, "HTTP 403: Forbidden")
        self.click(key="actionable_tg")
        self.assertFalse(self.app.session_state["actionable_tg_result"]["ok"])
        self.run_app()
        self.assertTrue(any("HTTP 403" in e.value for e in self.app.error))
        self.assertFalse(any("已接受" in e.value for e in self.app.success))
        self.send.assert_called_once()  # 一般 rerun 不會重送

    def test_tw_top10_empty_market_banner_still_sends(self):
        self.app.session_state["actionable_picks_cache"] = [{"_no_picks_reason": "空頭保護"}]
        self.click(key="actionable_tg")
        self.send.assert_called_once()
        self.assertIn("空頭保護", self.send.call_args.args[0])

    def test_tw_top10_formatter_failure_is_visible_and_persistent(self):
        self.app.session_state["actionable_picks_cache"] = [{"stock_id": "2330"}]
        with patch("actionable_picks.fmt_actionable_picks_tg", side_effect=ValueError("test format error")):
            self.click(key="actionable_tg")
        self.run_app()
        self.send.assert_not_called()
        self.assertTrue(any("test format error" in e.value for e in self.app.error))

    def test_precursor_old_and_dedicated_buttons_send_same_results(self):
        picks = [{"stock_id": "2330", "name": "TEST", "theme": "AI", "score": 40,
                  "current": 100, "today_pct": 1, "reasons": ["吸籌"], "warnings": []}]
        with patch("limit_up_precursor.scan_limit_up_precursor", return_value=picks):
            self.click(label="🎯 漲停前兆")
        for key in ("send_pulse_tg", "send_lup_top", "send_lup_tg"):
            self.send.reset_mock()
            self.click(key=key)
            self.send.assert_called_once()
            self.assertIn("2330", self.send.call_args.args[0])
            self.assertIn("漲停前兆", self.send.call_args.args[0])

    def test_latest_failed_scan_does_not_send_old_results(self):
        self.app.session_state["limit_up_precursor"] = [{"stock_id": "2330"}]
        with patch("limit_up_precursor.scan_limit_up_precursor", side_effect=RuntimeError("test failure")):
            self.click(label="🎯 漲停前兆")
        self.click(key="send_pulse_tg")
        self.send.assert_not_called()
        self.assertTrue(any("沒有可推送內容" in w.value for w in self.app.warning))

    def test_tw_send_without_scan_has_feedback(self):
        # 台股 Send originally lived inside the nonempty-results branch.
        button = next(b for b in self.app.tabs[4].button if b.label == "✈️ Send to TG")
        button.click()
        self.run_app()
        self.send.assert_not_called()
        self.assertTrue(any("台股篩選：沒有可推送內容" in w.value for w in self.app.warning))

    def test_stock_failure_clears_previous_analysis(self):
        self.app.session_state["last_stock"] = {"sid": "OLD"}
        self.app.session_state["last_ai"] = {"sid": "OLD", "text": "old AI"}
        self.app.session_state["entry_result"] = {"verdict": {"verdict": "OLD"}}
        self.app.text_input(key="unified_stock_input").set_value("BAD")
        with patch("entry_evaluator.evaluate_entry", side_effect=RuntimeError("test failure")), \
             patch("stock_analyzer.fetch_stock_full", side_effect=RuntimeError("test failure")):
            self.click(key="unified_analyze_btn")
        for key in ("last_stock", "last_ai", "entry_result"):
            self.assertNotIn(key, self.app.session_state)

    def test_all_shared_button_routes(self):
        from dashboard_actions import format_pulse_send
        frame = pd.DataFrame([{"stock_id": "2330"}])
        states = [
            ("pulse", {"sectors": frame}, "notifier.fmt_strong_sectors"),
            ("themes", {"themes": frame}, "notifier.fmt_strong_sectors"),
            ("stealth", {"stealth": frame}, "notifier.fmt_stealth_picks"),
            ("smart_stealth", [{"stock_id": "2330"}], "smart_money_stealth.fmt_smart_stealth_msg"),
            ("limit_up_precursor", [{"stock_id": "2330"}], "limit_up_precursor.fmt_limit_up_precursor_msg"),
        ]
        for target, data, formatter in states:
            with self.subTest(target=target), patch(formatter, return_value="selected") as fmt:
                result, label = format_pulse_send({"_pulse_send_target": target, target: data})
                self.assertEqual(result, "selected")
                fmt.assert_called_once()

        # 更新前的 session 可同時留著空的產業分析和有資料的熱門題材。
        with patch("notifier.fmt_strong_sectors", return_value="legacy"):
            msg, _ = format_pulse_send({"pulse": {"sectors": pd.DataFrame()},
                                       "themes": {"themes": frame}})
        self.assertEqual(msg, "legacy")

    def test_us_send_matches_displayed_selection(self):
        self.app.session_state["us_result"] = {"top_picks": pd.DataFrame([{"symbol": "REJECTED"}])}
        self.app.session_state["us_actionable"] = [{"symbol": "SELECTED", "name": "Test", "score": 7,
                                                  "target_short": 123, "entry_score": 70}]
        self.click(key="send_us_tg")
        self.send.assert_called_once()
        message = self.send.call_args.args[0]
        self.assertIn("SELECTED", message)
        self.assertIn("123", message)
        self.assertNotIn("REJECTED", message)

    def test_backtest_and_tracking_results_survive_rerun(self):
        result = {"summary": pd.DataFrame([{"condition": "break_ma", "win_rate": 50}]),
                  "raw": pd.DataFrame([{"r5d": 1.0}])}
        with patch("data_sources.get_taiwan_stock_info", return_value=pd.DataFrame([{"stock_id": "2330"}])), \
             patch("data_sources.filter_tradeable_stocks", side_effect=lambda df: df), \
             patch("backtest.run_backtest", return_value=result):
            self.click(key="bt_run")
        with patch("tracker.load_history", return_value=pd.DataFrame()), \
             patch("tracker.evaluate_history_performance", return_value=pd.DataFrame([{"return%": 2.0}])):
            self.click(key="track_run_btn")
        self.run_app()
        metrics = {m.label: m.value for m in self.app.metric}
        self.assertEqual(metrics["總命中筆數"], "1")
        self.assertEqual(metrics["平均報酬"], "+2.00%")

    def test_main_analysis_buttons_handle_empty_data(self):
        cases = [
            ("actionable_picks.compute_actionable_picks", [], {"key": "actionable_load"}),
            ("sector_pulse.compute_strong_sectors", {"sectors": pd.DataFrame()}, {"label": "📊 產業分類"}),
            ("sector_pulse.compute_hot_themes", {"themes": pd.DataFrame()}, {"label": "🔥 熱門題材"}),
            ("sector_pulse.find_stealth_followers", {"stealth": pd.DataFrame()}, {"label": "🌱 潛伏題材股"}),
            ("market_open_picks.get_tw_open_picks", {}, {"key": "tw_open_btn"}),
            ("market_open_picks.get_us_open_picks", {}, {"key": "us_open_btn"}),
            ("news_picks.run_news_growth_picks", {"picks": pd.DataFrame()}, {"label": "🔄 更新成長動能榜"}),
            ("us_upside_screener.run_us_upside_screen", {"meta": {}}, {"key": "us_upside_btn"}),
            ("us_screener.run_us_recommendation", {}, {"key": "mood_btn_main"}),
            ("tx_futures_alert.check_tx_futures_alerts", [], {"key": "btn_tx_futures"}),
        ]
        for function, value, button in cases:
            with self.subTest(function=function), patch(function, return_value=value) as mock:
                self.click(**button)
                mock.assert_called_once()
                self.assertEqual([e.value for e in self.app.error], [])

        with patch("smart_money_stealth.scan_smart_money_stealth", return_value=[]), \
             patch("hot_theme_v2.find_all_patterns", return_value={}):
            self.click(label="🕵️ 大戶偷進場")
        with patch("tw_screener.run_all_screens", return_value={
            "latest_date": None, "ready": False, "results": {}, "combined": pd.DataFrame(),
        }), patch("tracker.save_snapshot", return_value={}):
            self.click(label="🔄 重新整理 / 開始掃描")
        self.assertEqual([e.value for e in self.app.error], [])

    def test_holdings_failure_clears_old_analysis(self):
        with patch("holdings_store.load_holdings", return_value=[{"stock_id": "2330", "name": "Test"}]), \
             patch("portfolio_risk.analyze_portfolio_risk", return_value={}), \
             patch("holdings_analyzer.analyze_all_holdings", side_effect=RuntimeError("test failure")):
            self.run_app()
            self.app.session_state["h_results"] = [{"stock_id": "OLD"}]
            self.click(key="h_run_analysis")
        self.assertNotIn("h_results", self.app.session_state)

    def test_mood_failure_does_not_fall_back_to_old_us_result(self):
        self.app.session_state["us_result"] = {"fear_greed": {"score": 50}}
        self.app.session_state["us_result_mood"] = {"fear_greed": {"score": 99}}
        with patch("us_screener.run_us_recommendation", side_effect=RuntimeError("test failure")):
            self.click(key="mood_btn_main")
        self.assertEqual(self.app.session_state["us_result_mood"], {})
        mood = next(t for t in self.app.tabs if "市場情緒" in t.label)
        self.assertTrue(any("尚未取得 CNN" in msg.value for msg in mood.info))

    def test_watchlist_add_button_calls_store(self):
        self.app.text_input(key="wl_sid").set_value("2330")
        with patch("watchlist_store.add_to_watchlist", return_value=True) as add:
            self.click(key="wl_add")
        add.assert_called_once_with("2330", "", "TW", None)

    def test_growth_refresh_failure_clears_previous_results(self):
        self.app.session_state["growth"] = {"picks": pd.DataFrame([{"代號": "OLD"}])}
        with patch("news_picks.run_news_growth_picks", side_effect=RuntimeError("test failure")):
            self.click(label="🔄 更新成長動能榜")
        self.click(key="send_growth")
        self.send.assert_not_called()

    def test_us_refresh_failure_clears_previous_results(self):
        self.app.session_state["us_actionable"] = [{"symbol": "OLD"}]
        with patch("us_screener.run_us_recommendation", side_effect=RuntimeError("test failure")):
            self.click(key="us_top10_refresh")
        self.click(key="send_us_tg")
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
