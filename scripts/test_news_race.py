"""#18: 同一則新聞同時命中持倉與主清單時, 一定保留持倉 (hold) 版本 — 與執行緒誰先跑完無關。"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.alerts import news_event_alert as ne  # noqa: E402

NEWS = [{"title": "TSMC wins major order, earnings beat expectations", "link": "https://x.test/a", "type": "YAHOO"}]


class NewsRaceTests(unittest.TestCase):
    def _run(self, universe):
        with patch.object(ne, "_is_tw_or_us_session", return_value=True), \
             patch.object(ne.watchlist_store, "load_monitor_state", return_value={}), \
             patch.object(ne, "_gather_universe", return_value=universe), \
             patch.object(ne.ds, "fetch_yahoo_news", side_effect=lambda *a, **k: [dict(n) for n in NEWS]), \
             patch("radar.data.event_sources.fetch_finnhub_8k", return_value=[]), \
             patch("radar.data.event_sources.fetch_us_press_releases", return_value=[]):
            return ne.check_news_events()

    def test_hold_wins_regardless_of_order(self):
        if not ne._match_keywords(NEWS[0]["title"]):
            self.skipTest("關鍵字表沒有命中測試標題")
        for uni in ([{"symbol": "AAA", "market": "US", "tag": "main"}, {"symbol": "BBB", "market": "US", "tag": "hold"}],
                    [{"symbol": "BBB", "market": "US", "tag": "hold"}, {"symbol": "AAA", "market": "US", "tag": "main"}]):
            got = self._run(uni)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["tag"], "hold")

    def test_already_alerted_is_skipped(self):
        h = ne._headline_hash(NEWS[0]["title"])
        out = ne._scan_news_for_symbol({"symbol": "AAA", "market": "US", "tag": "hold"}, frozenset({h}))
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
