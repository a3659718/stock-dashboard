"""2026-11-27 實際事故重現: 昨天 15:03 的 tw_close cron 遲到 10.5 小時 (01:33 才跑) 並推播,
今天 15:00 準時觸發不該被它擋掉; 同一天的晚到 cron 仍要被擋。"""
import datetime as dt
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.core import push_dedup as pd  # noqa: E402
from radar.core import clock  # noqa: E402

UTC = dt.timezone.utc


class DayDedupTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.p = [patch.object(pd, "_load", side_effect=lambda: self.state),
                  patch.object(pd, "_save", side_effect=lambda s: self.state.update(s))]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def _at(self, utc_iso, env):
        t = dt.datetime.fromisoformat(utc_iso)
        return [patch.object(pd, "_now", return_value=t.replace(tzinfo=None)),
                patch.object(clock, "_utc_now", return_value=t.replace(tzinfo=UTC)),
                patch.dict(os.environ, env, clear=False),
                patch("radar.core.schedule_guard.dt.datetime", wraps=dt.datetime)]

    def _run(self, utc_iso, env, fn):
        ps = self._at(utc_iso, env)
        for p in ps:
            p.start()
        try:
            import radar.core.schedule_guard as sg
            with patch.object(sg, "intended_market_date",
                              side_effect=lambda m, now_utc=None, env=None: sg.__class__ and _imd(m, utc_iso)):
                return fn()
        finally:
            for p in ps:
                p.stop()

    def test_late_yesterday_cron_does_not_block_today(self):
        cron_env = {"GITHUB_EVENT_NAME": "schedule", "SCHEDULE_CRON": "3 7 * * 1-5"}
        punct_env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "DISPATCH_SOURCE": "punctual", "SCHEDULE_CRON": ""}
        # 11-26 15:03 cron, 11-27 01:33 台北 (= 11-26 17:33 UTC) 才跑 → 推播並登記
        self._run("2026-11-26T17:33:00", cron_env, lambda: pd.claim_day("tw_close"))
        self.assertIn("tw_close@2026-11-26", self.state["slot_dedup"])
        # 11-27 15:00 台北 準時觸發 → 不能被擋
        blocked = self._run("2026-11-27T07:00:00", punct_env, lambda: pd.claimed_for_day("tw_close"))
        self.assertFalse(blocked)
        self._run("2026-11-27T07:00:00", punct_env, lambda: pd.claim_day("tw_close"))
        # 今天 15:03 的 cron 晚 3 小時才到 → 要被擋 (同一交易日已推過)
        self.assertTrue(self._run("2026-11-27T10:03:00", cron_env, lambda: pd.claimed_for_day("tw_close")))

    def test_us_slot_uses_et_date(self):
        env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "DISPATCH_SOURCE": "punctual", "SCHEDULE_CRON": ""}
        # us_close 17:59 ET 11-25 = 22:59 UTC → 交易日 11-25 (台北已是 11-26)
        k = self._run("2026-11-25T22:59:00", env, lambda: pd.day_key("us_close"))
        self.assertEqual(k, "us_close@2026-11-25")


class RealIntendedDateTests(unittest.TestCase):
    def test_real_intended_market_date_for_the_incident(self):
        from radar.core import schedule_guard as sg
        env = {"GITHUB_EVENT_NAME": "schedule", "SCHEDULE_CRON": "3 7 * * 1-5"}
        # 11-27 01:33 台北 跑的其實是 11-26 那班
        self.assertEqual(sg.intended_market_date("TW", dt.datetime(2026, 11, 26, 17, 33), env), dt.date(2026, 11, 26))


def _imd(market, utc_iso):
    """模擬 schedule_guard.intended_market_date: cron run 回排定時間的當地日期, 其餘 None。"""
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return None
    from radar.core import schedule_guard as sg
    fire = sg.intended_fire_time(os.environ["SCHEDULE_CRON"], dt.datetime.fromisoformat(utc_iso))
    if market == "US":
        return clock.et_today(now=fire.replace(tzinfo=UTC))
    return (fire + dt.timedelta(hours=8)).date()


if __name__ == "__main__":
    unittest.main()
