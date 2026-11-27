"""Offline tests for radar/data/intraday_volume.py (時間校正量比, audit #2). 不連網."""
import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radar.data import intraday_volume as iv  # noqa: E402

TPE = "Asia/Taipei"
NY = "America/New_York"


def _u_shape(n_bars):
    """每根 5m bar 的量 (U 型: 頭尾大, 中間小), 讓「時間校正」跟「整日」差很多."""
    out = []
    for i in range(n_bars):
        edge = min(i, n_bars - 1 - i)
        out.append(1000.0 + 9000.0 / (1 + edge))
    return out


def _session_bars(day, tz, open_hm, n_bars, scale=1.0, upto=None, last_partial=None):
    """產生某日的 5m bars. upto=只產生前 upto 根; last_partial=最後一根乘上這個比例."""
    start = pd.Timestamp(f"{day} {open_hm[0]:02d}:{open_hm[1]:02d}", tz=tz)
    vols = _u_shape(n_bars)
    k = n_bars if upto is None else upto
    rows = []
    for i in range(k):
        v = vols[i] * scale
        if last_partial is not None and i == k - 1:
            v *= last_partial
        rows.append({"Datetime": start + dt.timedelta(minutes=5 * i), "Open": 10, "High": 10,
                     "Low": 10, "Close": 10, "Volume": v})
    return rows


def _frame(prev_days, today, tz, open_hm, n_bars, **today_kw):
    rows = []
    for d in prev_days:
        rows += _session_bars(d, tz, open_hm, n_bars)
    rows += _session_bars(today, tz, open_hm, n_bars, **today_kw)
    return pd.DataFrame(rows)


TW_PREV = ["2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24"]
TW_TODAY = "2026-11-25"         # 週三
TW_BARS = 54                    # 09:00..13:25 共 54 根 5m


def tpe(hm, day=TW_TODAY):
    return pd.Timestamp(f"{day} {hm}", tz=TPE).tz_convert("UTC").to_pydatetime()


class TwIntradayHistoryTests(unittest.TestCase):
    def test_normal_pace_30min_after_open_is_one(self):
        # 09:00..09:25 共 6 根已走完, now = 09:30
        df = _frame(TW_PREV, TW_TODAY, TPE, (9, 0), TW_BARS, upto=6)
        d = iv.intraday_volume_ratio_detail(df, "TW", now=tpe("09:30"))
        self.assertEqual(d["method"], "history")
        self.assertAlmostEqual(d["ratio"], 1.0, places=6)
        # 舊算法 (÷ 整日平均) 會遠小於 1 — 確認測試資料真的有反映問題
        full = sum(_u_shape(TW_BARS))
        self.assertLess(d["today_vol"] / full, 0.4)

    def test_double_pace_is_two(self):
        df = _frame(TW_PREV, TW_TODAY, TPE, (9, 0), TW_BARS, upto=6, scale=2.0)
        self.assertAlmostEqual(iv.intraday_volume_ratio(df, "TW", now=tpe("09:30")), 2.0, places=6)

    def test_partial_last_bar_prorated(self):
        # now = 09:32:30, 09:30 那根只走了一半 (量也只有一半) → 仍是正常步調
        df = _frame(TW_PREV, TW_TODAY, TPE, (9, 0), TW_BARS, upto=7, last_partial=0.5)
        self.assertAlmostEqual(iv.intraday_volume_ratio(df, "TW", now=tpe("09:32:30")), 1.0, places=6)

    def test_after_close_equals_full_day_ratio(self):
        df = _frame(TW_PREV, TW_TODAY, TPE, (9, 0), TW_BARS, scale=1.5)
        d = iv.intraday_volume_ratio_detail(df, "TW", now=tpe("14:05"))
        self.assertEqual(d["method"], "full_day")
        self.assertAlmostEqual(d["ratio"], 1.5, places=6)

    def test_stale_data_from_previous_day_is_full_day(self):
        # 資料最後一天是昨天 (假日 / 延遲) → 今日早上跑也要當完整一天
        df = _frame(TW_PREV[:-1], TW_PREV[-1], TPE, (9, 0), TW_BARS)
        d = iv.intraday_volume_ratio_detail(df, "TW", now=tpe("09:30"))
        self.assertEqual(d["method"], "full_day")
        self.assertAlmostEqual(d["ratio"], 1.0, places=6)

    def test_utc_timestamps_converted_to_market_tz(self):
        # bars 以 UTC 表示 (runner 環境), 日期分組仍要用台北日期
        df = _frame(TW_PREV, TW_TODAY, TPE, (9, 0), TW_BARS, upto=6)
        df["Datetime"] = df["Datetime"].dt.tz_convert("UTC")
        self.assertAlmostEqual(iv.intraday_volume_ratio(df, "TW", now=tpe("09:30")), 1.0, places=6)

    def test_no_previous_days_returns_none(self):
        df = pd.DataFrame(_session_bars(TW_TODAY, TPE, (9, 0), TW_BARS, upto=6))
        self.assertIsNone(iv.intraday_volume_ratio(df, "TW", now=tpe("09:30")))


class UsDstTests(unittest.TestCase):
    US_BARS = 78  # 09:30..15:55

    def _check(self, prev, today, utc_now):
        df = _frame(prev, today, NY, (9, 30), self.US_BARS, upto=6)
        d = iv.intraday_volume_ratio_detail(df, "US", now=utc_now)
        self.assertEqual(d["method"], "history")
        self.assertAlmostEqual(d["ratio"], 1.0, places=6)
        self.assertAlmostEqual(d["elapsed_min"], 30.0, places=6)

    def test_summer_edt(self):
        # 10:00 EDT = 14:00 UTC
        self._check(["2026-06-25", "2026-06-26", "2026-06-29", "2026-06-30"], "2026-07-01",
                    dt.datetime(2026, 7, 1, 14, 0))

    def test_winter_est(self):
        # 10:00 EST = 15:00 UTC
        self._check(["2026-12-07", "2026-12-08", "2026-12-09", "2026-12-10"], "2026-12-11",
                    dt.datetime(2026, 12, 11, 15, 0))

    def test_dst_switch_week(self):
        # 2026-03-08 美國開始夏令時間: 前幾天是 EST, 當天是 EDT, 同一個「當地時鐘時間」比較
        self._check(["2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"], "2026-03-09",
                    dt.datetime(2026, 3, 9, 14, 0))

    def test_elapsed_minutes_dst(self):
        self.assertAlmostEqual(iv.elapsed_minutes("US", dt.datetime(2026, 7, 1, 14, 0)), 30.0)
        self.assertAlmostEqual(iv.elapsed_minutes("US", dt.datetime(2026, 12, 11, 15, 0)), 30.0)
        # 冬令時 14:00 UTC = 09:00 EST → 還沒開盤
        self.assertLess(iv.elapsed_minutes("US", dt.datetime(2026, 12, 11, 14, 0)), 0)


class ProfileFallbackTests(unittest.TestCase):
    def _daily(self, last_date, last_vol, avg=1_000_000.0):
        dates = pd.bdate_range(end=last_date, periods=7)
        vols = [avg] * 6 + [last_vol]
        return pd.DataFrame({"Date": dates, "Close": 10.0, "Volume": vols})

    def test_curve_monotonic_and_bounded(self):
        for m in ("TW", "US"):
            length = iv.SESSIONS[m]["length"]
            vals = [iv.cum_fraction(x, m) for x in range(0, length + 30)]
            self.assertTrue(all(b >= a for a, b in zip(vals, vals[1:])))
            self.assertAlmostEqual(vals[-1], 1.0)
            self.assertGreater(vals[0], 0)          # elapsed 夾到 MIN_ELAPSED_MIN, 分母不為 0

    def test_tw_daily_normal_pace_30min(self):
        frac = iv.cum_fraction(30, "TW")
        df = self._daily(TW_TODAY, 1_000_000.0 * frac)
        self.assertAlmostEqual(iv.daily_volume_ratio(df, "TW", now=tpe("09:30")), 1.0, places=6)
        df2 = self._daily(TW_TODAY, 2_000_000.0 * frac)
        self.assertAlmostEqual(iv.daily_volume_ratio(df2, "TW", now=tpe("09:30")), 2.0, places=6)

    def test_us_daily_dst(self):
        frac = iv.cum_fraction(30, "US")
        for day, now in (("2026-07-01", dt.datetime(2026, 7, 1, 14, 0)),
                         ("2026-12-11", dt.datetime(2026, 12, 11, 15, 0))):
            df = self._daily(day, 1_000_000.0 * frac)
            self.assertAlmostEqual(iv.daily_volume_ratio(df, "US", now=now), 1.0, places=6)

    def test_daily_after_close_or_premarket_is_plain(self):
        df = self._daily(TW_TODAY, 1_500_000.0)
        self.assertAlmostEqual(iv.daily_volume_ratio(df, "TW", now=tpe("14:00")), 1.5, places=6)
        # 隔天開盤前: 最後一根是完整的昨天
        self.assertAlmostEqual(iv.daily_volume_ratio(df, "TW", now=tpe("08:30", "2026-11-26")),
                               1.5, places=6)
        # 隔天盤中但最後一根仍是昨天 (今天的 K 還沒出來) → 不可校正
        self.assertAlmostEqual(iv.daily_volume_ratio(df, "TW", now=tpe("09:30", "2026-11-26")),
                               1.5, places=6)

    def test_adjust_column_in_session_and_idempotent(self):
        frac = iv.cum_fraction(30, "TW")
        df = pd.DataFrame({"stock_id": ["A", "B", "C"], "量比": [round(frac, 4), round(2 * frac, 4), None]})
        with patch("radar.core.holiday_check.is_market_closed_today", return_value=False):
            out = iv.adjust_ratio_column(df, "TW", now=tpe("09:30"))
            self.assertAlmostEqual(out["量比"].iloc[0], 1.0, places=2)
            self.assertAlmostEqual(out["量比"].iloc[1], 2.0, places=2)
            self.assertTrue(pd.isna(out["量比"].iloc[2]))
            again = iv.adjust_ratio_column(out, "TW", now=tpe("09:30"))
            self.assertAlmostEqual(again["量比"].iloc[0], 1.0, places=2)   # 不會再除一次
            after = iv.adjust_ratio_column(df, "TW", now=tpe("14:00"))
            self.assertAlmostEqual(after["量比"].iloc[0], round(frac, 4), places=6)
        self.assertAlmostEqual(df["量比"].iloc[0], round(frac, 4))          # 原 df 不被改

    def test_adjust_skips_holiday(self):
        with patch("radar.core.holiday_check.is_market_closed_today", return_value=True):
            self.assertAlmostEqual(iv.adjust_full_day_ratio(0.3, "TW", now=tpe("09:30")), 0.3)


class CallSiteTests(unittest.TestCase):
    """呼叫端改用 helper 後, 盤中正常步調的股票量比 ≈ 1 (原本 ≈ 0.2)."""

    def test_strong_stock_metrics_uses_time_adjusted_ratio(self):
        from radar.alerts import strong_stock_alert as ssa
        df = _frame(TW_PREV, TW_TODAY, TPE, (9, 0), TW_BARS, upto=6)
        fixed = pd.Timestamp(tpe("09:30")).tz_convert(TPE)
        with patch.object(ssa.ds, "fetch_yf_history", return_value=df), \
                patch.object(iv, "_now_local", lambda market, now=None: fixed if now is None
                             else pd.Timestamp(now).tz_convert(iv.SESSIONS[market]["tz"])):
            m = ssa._stock_strength_metrics("2330")
        self.assertAlmostEqual(m["vol_ratio"], 1.0, places=2)

    def test_us_stock_metrics_uses_profile(self):
        from radar.screeners import market_open_picks as mop
        frac = iv.cum_fraction(30, "US")
        dates = pd.bdate_range(end="2026-07-01", periods=30)
        df = pd.DataFrame({"Date": dates, "Close": 10.0, "Volume": [1e6] * 29 + [2e6 * frac]})
        fixed = pd.Timestamp(dt.datetime(2026, 7, 1, 14, 0), tz="UTC").tz_convert(NY)
        with patch.object(mop.ds, "fetch_yf_history", return_value=df), \
                patch.object(iv, "_now_local", lambda market, now=None: fixed if now is None
                             else pd.Timestamp(now).tz_convert(iv.SESSIONS[market]["tz"])):
            m = mop._us_stock_metrics("NVDA")
        self.assertAlmostEqual(m["量比"], 2.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
