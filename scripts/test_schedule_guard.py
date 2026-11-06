"""Offline tests for radar/core/schedule_guard (GitHub cron 遲到守衛)."""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.core import schedule_guard as g  # noqa: E402

U = dt.datetime
SCHED = {"GITHUB_EVENT_NAME": "schedule", "SCHEDULE_CRON": "17 0 * * 1-5"}


class ScheduleGuardTests(unittest.TestCase):
    def test_real_case_six_hours_late_is_skipped(self):
        # 2026-11-06 實測: 08:17 TPE 的盤前 cron 在 UTC 06:57 (TPE 14:57) 才跑
        reason = g.check_stale("pre_market_815", U(2026, 11, 6, 6, 57), SCHED)
        self.assertIsNotNone(reason)
        self.assertIn("400", reason)

    def test_on_time_runs(self):
        self.assertIsNone(g.check_stale("pre_market_815", U(2026, 11, 6, 0, 22), SCHED))

    def test_dispatch_and_disable_never_blocked(self):
        late = U(2026, 11, 6, 6, 57)
        self.assertIsNone(g.check_stale("pre_market_815", late, {**SCHED, "GITHUB_EVENT_NAME": "workflow_dispatch"}))
        self.assertIsNone(g.check_stale("pre_market_815", late, {**SCHED, "STALE_GUARD": "0"}))

    def test_unguarded_slot_and_bad_cron_fail_open(self):
        self.assertIsNone(g.check_stale("tw_close", U(2026, 11, 6, 13, 0), {**SCHED, "SCHEDULE_CRON": "3 7 * * 1-5"}))
        self.assertIsNone(g.check_stale("tw_open", U(2026, 11, 6, 3, 0), {**SCHED, "SCHEDULE_CRON": "garbage"}))

    def test_cron_parsing(self):
        self.assertEqual(g.lateness_minutes("32 23 * * 0-5", U(2026, 11, 9, 0, 40)), 68)  # 週日 UTC → 週一
        self.assertEqual(g.lateness_minutes("8,38 1,2,3,4 * * 1-5", U(2026, 11, 6, 2, 50)), 12)
        self.assertTrue(g.cron_matches("*/15 * * * *", U(2026, 11, 6, 3, 45)))


if __name__ == "__main__":
    unittest.main()
