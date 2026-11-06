"""Offline tests for 2026-11 delivery fixes (items 5-7): triggers, intraday rollback, news AI wait.

No network, no Telegram: notifier.send_message is patched, state lives in a temp JSON file.
"""
import datetime as dt
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.core import watchlist_store as ws  # noqa: E402


class _TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "monitor_state.json"
        self.file.write_text("{}", encoding="utf-8")
        self.patches = [patch.object(ws, "MONITOR_STATE_FILE", self.file),
                        patch.object(ws, "_get_sheet", return_value=None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def disk(self):
        return json.loads(self.file.read_text(encoding="utf-8"))


class WatchlistTriggerDeliveryTests(_TempState):
    """Item 5: 觸發要直接送達才算用掉; 送失敗要重新上膛。"""

    def _setup_trigger(self):
        from radar.alerts import watchlist_triggers as wt
        tid = wt.add_trigger("2330", "kd_golden_cross", market="TW")
        self.assertTrue(tid)
        fired = [{"id": tid, "stock_id": "2330", "name": "台積電", "type": "kd_golden_cross",
                  "type_label": "KD 黃金交叉", "msg": "K 上穿 D", "current": 1000, "value": None,
                  "fired_at": (dt.datetime.utcnow() + dt.timedelta(hours=8)).date().isoformat()}]
        return wt, tid, fired

    def _run(self, send_result):
        moa = importlib.import_module("scripts.market_open_alert")
        wt, tid, fired = self._setup_trigger()
        with patch.object(wt, "check_triggers", return_value=fired), \
             patch.object(moa.notifier, "send_message", return_value=send_result) as send:
            moa._run_watchlist_triggers("test")
        return tid, send

    def test_failed_send_rearms_trigger(self):
        tid, send = self._run((False, "HTTP 502"))
        send.assert_called_once()
        t = self.disk()["watchlist_triggers"][tid]
        self.assertTrue(t["armed"])           # 修正前: 排進佇列就解除警戒, 使用者從來沒收到
        self.assertIsNone(t.get("fired_at"))

    def test_successful_send_disarms_and_records(self):
        tid, send = self._run((True, "已送出"))
        d = self.disk()
        self.assertFalse(d["watchlist_triggers"][tid]["armed"])
        self.assertEqual(d["watchlist_triggers_today"][0]["stock_id"], "2330")
        self.assertIn("2330", send.call_args.args[0])

    def test_tw_close_check_after_1335(self):
        from radar.alerts import watchlist_triggers as wt

        class _FakeDT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime(2026, 11, 6, 5, 40, tzinfo=dt.timezone.utc)
        with patch.object(wt.dt, "datetime", _FakeDT):
            self.assertTrue(wt._is_market_closed_now("TW"))   # 13:40 TPE: 已收盤, 可判斷交叉


class IntradayRollbackTests(_TempState):
    """Item 6: 合併推播送失敗 → 三個 check_* 寫入的狀態要回滾。"""

    def test_snapshot_and_restore(self):
        moa = importlib.import_module("scripts.market_open_alert")
        self.file.write_text(json.dumps({"weak_open": {"weak_alerted": []}, "keep": 1}), encoding="utf-8")
        snap = moa._snapshot_state_keys(moa._INTRADAY_CLAIM_KEYS)
        st = ws.load_monitor_state()                       # 模擬 check_* 寫入 claim
        st["weak_open"] = {"weak_alerted": ["^TWII"]}
        st["systemic_crash_alerts"] = {"alerts_today": 1}
        ws.save_monitor_state(st)
        moa._restore_state_keys(snap, "test")
        d = self.disk()
        self.assertEqual(d["weak_open"], {"weak_alerted": []})
        self.assertEqual(d.get("systemic_crash_alerts", {}), {})  # 原本不存在 → 清成空 (等同沒有 claim)
        self.assertEqual(d["keep"], 1)


class NewsAiWaitTests(_TempState):
    """Item 7: 急報缺 AI 分析最多等 NEWS_AI_WAIT_MAX_MIN 分。"""

    def test_wait_clock(self):
        from radar.alerts import news_event_alert as ne
        alerts = [{"h_hash": "abc", "title": "8-K filed"}]
        self.assertLess(ne.note_ai_wait(alerts), 1)        # 第一次延後: 剛開始等
        st = ws.load_monitor_state()
        old = (dt.datetime.utcnow() - dt.timedelta(minutes=ne.NEWS_AI_WAIT_MAX_MIN + 5)).isoformat()
        st["news_event_alert"]["ai_wait"]["abc"] = old
        ws.save_monitor_state(st)
        self.assertGreaterEqual(ne.note_ai_wait(alerts), ne.NEWS_AI_WAIT_MAX_MIN)
        ne.clear_ai_wait(alerts)
        self.assertNotIn("abc", self.disk()["news_event_alert"]["ai_wait"])

    def test_day_roll_keeps_dedup_and_wait(self):
        from radar.alerts import news_event_alert as ne
        st = {"date": "2026-11-05", "alerted": ["h1"], "alerted_at": {"h1": "2026-11-05", "h0": "2026-11-01"},
              "ai_wait": {"h2": "2026-11-05T23:00:00"}}
        ne._roll_day(st, "2026-11-06")
        self.assertIn("h1", st["alerted"])                 # 修正前每天清空 → 隔天重推
        self.assertNotIn("h0", st["alerted"])              # 超過 3 天才丟
        self.assertIn("h2", st["ai_wait"])


if __name__ == "__main__":
    unittest.main()
