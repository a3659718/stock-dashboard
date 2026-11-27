"""Offline tests for radar.core.clock (audit #16: UTC 伺服器上的台北/美東日期)."""
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.core import clock  # noqa: E402
from radar.core import holiday_check  # noqa: E402

UTC = dt.timezone.utc


def _at(*args):
    """固定 clock 的「現在」(aware UTC)。"""
    return patch.object(clock, "_utc_now", return_value=dt.datetime(*args, tzinfo=UTC))


class ClockTests(unittest.TestCase):
    def test_late_utc_evening_is_next_day_in_taipei(self):
        # 2026-11-27 (週五) 23:30 UTC = 台北 11-28 (週六) 07:30 = 美東 11-27 18:30 (EST)
        with _at(2026, 11, 27, 23, 30):
            self.assertEqual(clock.tpe_today(), dt.date(2026, 11, 28))
            self.assertEqual(clock.et_today(), dt.date(2026, 11, 27))
            self.assertEqual(clock.market_today("US"), dt.date(2026, 11, 27))
            self.assertEqual(clock.market_today("TW"), dt.date(2026, 11, 28))
            self.assertEqual(clock.market_today("us"), dt.date(2026, 11, 27))   # 大小寫不敏感
            self.assertEqual(clock.market_today(None), dt.date(2026, 11, 28))   # 預設台北
            self.assertEqual(clock.tpe_today_str(), "2026-11-28")
            self.assertEqual(clock.et_today_str(), "2026-11-27")
            self.assertEqual(clock.tpe_now().strftime("%H:%M"), "07:30")
            self.assertEqual(clock.et_now().strftime("%H:%M"), "18:30")
            self.assertEqual(clock.utc_now().date(), dt.date(2026, 11, 27))
            # holiday_check 的私有 helper 也走同一套
            self.assertEqual(holiday_check._today_tpe(), dt.date(2026, 11, 28))
            self.assertEqual(holiday_check._today_et(), dt.date(2026, 11, 27))
            self.assertEqual(holiday_check._today_for_market("US"), dt.date(2026, 11, 27))

    def test_now_argument_overrides_clock(self):
        t = dt.datetime(2026, 11, 27, 23, 30)  # naive 視為 UTC
        self.assertEqual(clock.tpe_today(t), dt.date(2026, 11, 28))
        self.assertEqual(clock.et_today(t), dt.date(2026, 11, 27))
        aware_tpe = dt.datetime(2026, 11, 28, 7, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.assertEqual(clock.et_today(aware_tpe), dt.date(2026, 11, 27))

    def test_et_dst(self):
        # 夏令 (EDT, UTC-4): 2026-07-01 03:30 UTC = 6/30 23:30 ET
        with _at(2026, 7, 1, 3, 30):
            self.assertEqual(clock.et_today(), dt.date(2026, 6, 30))
            self.assertEqual(clock.et_now().utcoffset(), dt.timedelta(hours=-4))
        # 同一 UTC 時刻在冬令 (EST, UTC-5): 2026-12-01 04:30 UTC = 11/30 23:30 ET
        with _at(2026, 12, 1, 4, 30):
            self.assertEqual(clock.et_today(), dt.date(2026, 11, 30))
            self.assertEqual(clock.et_now().utcoffset(), dt.timedelta(hours=-5))
        # 冬令 04:30 UTC 已是夏令的隔天: 7/1 04:30 UTC = 7/1 00:30 EDT
        with _at(2026, 7, 1, 4, 30):
            self.assertEqual(clock.et_today(), dt.date(2026, 7, 1))
        # DST 切換點: 2026-03-08 (3 月第二個週日) 07:00 UTC 起 EDT; 2026-11-01 06:00 UTC 起 EST
        self.assertEqual(clock.et_now(dt.datetime(2026, 3, 8, 6, 59)).utcoffset(), dt.timedelta(hours=-5))
        self.assertEqual(clock.et_now(dt.datetime(2026, 3, 8, 7, 0)).utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(clock.et_now(dt.datetime(2026, 11, 1, 5, 59)).utcoffset(), dt.timedelta(hours=-4))
        self.assertEqual(clock.et_now(dt.datetime(2026, 11, 1, 6, 0)).utcoffset(), dt.timedelta(hours=-5))

    def test_fallback_without_tzdata_matches_zoneinfo(self):
        # tzdata 缺失時的備援規則要跟 zoneinfo 一致 (掃 2026 全年每 6 小時)
        if clock.ET_TZ is None:
            self.skipTest("zoneinfo 不可用, 無從比對")
        t = dt.datetime(2026, 1, 1, tzinfo=UTC)
        while t.year == 2026:
            self.assertEqual(clock._et_offset_fallback(t).utcoffset(t),
                             t.astimezone(clock.ET_TZ).utcoffset(), t)
            t += dt.timedelta(hours=6)
        for edge in (dt.datetime(2026, 3, 8, 6, 59, tzinfo=UTC), dt.datetime(2026, 3, 8, 7, 0, tzinfo=UTC),
                     dt.datetime(2026, 11, 1, 5, 59, tzinfo=UTC), dt.datetime(2026, 11, 1, 6, 0, tzinfo=UTC)):
            self.assertEqual(clock._et_offset_fallback(edge).utcoffset(edge),
                             edge.astimezone(clock.ET_TZ).utcoffset(), edge)
        with patch.object(clock, "ET_TZ", None), _at(2026, 11, 27, 23, 30):
            self.assertEqual(clock.et_today(), dt.date(2026, 11, 27))

    def test_tpe_date_from_ts(self):
        ts = dt.datetime(2026, 11, 27, 23, 30, tzinfo=UTC).timestamp()
        self.assertEqual(clock.tpe_date_from_ts(ts), dt.date(2026, 11, 28))

    def test_naive_variants(self):
        with _at(2026, 11, 27, 23, 30):
            self.assertIsNone(clock.tpe_now_naive().tzinfo)
            self.assertEqual(clock.tpe_now_naive(), dt.datetime(2026, 11, 28, 7, 30))
            self.assertEqual(clock.et_now_naive(), dt.datetime(2026, 11, 27, 18, 30))


class CallSiteTests(unittest.TestCase):
    """改過的呼叫點: 依市場取交易日。"""

    def test_index_alerts_market_today_str(self):
        from radar.alerts import index_alerts as ia
        with _at(2026, 11, 27, 23, 30):
            self.assertEqual(ia._market_today_str("US"), "2026-11-27")
            self.assertEqual(ia._market_today_str("TW"), "2026-11-28")
            self.assertEqual(ia._market_today_str("JP"), "2026-11-28")
            self.assertEqual(ia._market_today_str(None), "2026-11-28")
        # 盤中時段: 各市場日期 == UTC 日期 (上線當天不會造成 state 重置)
        with _at(2026, 11, 27, 2, 0):    # 台股盤中
            self.assertEqual(ia._market_today_str("TW"), "2026-11-27")
        with _at(2026, 11, 27, 18, 0):   # 美股盤中
            self.assertEqual(ia._market_today_str("US"), "2026-11-27")

    def test_is_us_in_dst_default_uses_et(self):
        from radar.alerts import index_alerts as ia
        with _at(2026, 7, 1, 3, 30):
            self.assertTrue(ia._is_us_in_dst())
        with _at(2026, 12, 1, 3, 30):
            self.assertFalse(ia._is_us_in_dst())


if __name__ == "__main__":
    unittest.main()
