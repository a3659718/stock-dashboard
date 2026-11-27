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


class StaleNoticeTests(unittest.TestCase):
    """遲到被擋時每天一則靜音提醒; 準時觸發已推過的 slot 不提醒。"""

    def _run(self, claimed: set):
        from unittest.mock import patch
        from radar.core import push_dedup
        from radar.notify import notifier
        state = set(claimed)
        with patch.object(push_dedup, "was_claimed_recently", side_effect=lambda s, window_min=0: s in state), \
             patch.object(push_dedup, "force_claim", side_effect=lambda s: state.add(s)), \
             patch.object(notifier, "send_message", return_value=(True, "ok")) as send:
            g.notify_stale_skip("pre_market_815", "GitHub 排程延遲 400 分鐘")
            g.notify_stale_skip("tw_open", "GitHub 排程延遲 300 分鐘")
        return send

    def test_notice_once_per_day(self):
        send = self._run(set())
        self.assertEqual(send.call_count, 1)                       # 第二個被擋的 slot 不再重複通知
        self.assertTrue(send.call_args.kwargs["disable_notification"])
        self.assertFalse(send.call_args.kwargs["count_as_push"])
        self.assertIn("盤前 08:15", send.call_args.args[0])

    def test_no_notice_when_punctual_already_pushed(self):
        send = self._run({"pre_market_815", "tw_open"})
        send.assert_not_called()

    def test_no_notice_when_punctual_ran_without_pushing(self):
        # 準時 run 有跑但沒內容可推 (沒有 push claim) → 排程器正常, 不該提醒
        send = self._run({"punctual_ran:pre_market_815", "punctual_ran:tw_open"})
        send.assert_not_called()

    def test_no_notice_for_slot_outside_punctual_schedule(self):
        # 2026-11-27 實際誤報: 已停用的 07:32 morning_recap cron 遲到 342 分 → 發了「準時觸發沒在運作」
        from unittest.mock import patch
        from radar.notify import notifier
        with patch.object(notifier, "send_message", return_value=(True, "ok")) as send:
            g.notify_stale_skip("morning_recap", "GitHub 排程延遲 342 分鐘")
        send.assert_not_called()
        self.assertNotIn("morning_recap", g.STALE_LIMIT_MIN)

    def test_record_punctual_run_only_for_punctual_dispatch(self):
        from unittest.mock import patch
        from radar.core import push_dedup
        with patch.object(push_dedup, "force_claim") as fc:
            g.record_punctual_run("tw_open", {"GITHUB_EVENT_NAME": "schedule"})
            g.record_punctual_run("tw_open", {"GITHUB_EVENT_NAME": "workflow_dispatch", "DISPATCH_SOURCE": "manual"})
            fc.assert_not_called()
            g.record_punctual_run("tw_open", {"GITHUB_EVENT_NAME": "workflow_dispatch", "DISPATCH_SOURCE": "punctual"})
            fc.assert_called_once_with("punctual_ran:tw_open")

    def test_every_guarded_slot_is_in_punctual_schedule(self):
        # 有遲到守衛的 slot 都要有準時觸發, 否則守衛只會讓它永遠不推
        self.assertTrue(set(g.STALE_LIMIT_MIN) <= g._punctual_slot_names())


class PunctualSlotsTests(unittest.TestCase):
    """準時觸發表 (scripts/punctual_slots.json) 必須對得上 workflow 的 workflow_dispatch 輸入。"""

    def test_every_slot_is_dispatchable(self):
        import json
        import yaml
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import setup_cronjob_org as sc
        root = Path(__file__).resolve().parents[1]
        slots = json.loads((root / "scripts" / "punctual_slots.json").read_text(encoding="utf-8"))["slots"]
        self.assertEqual(len({s["name"] for s in slots}), len(slots))
        for s in slots:
            wf = yaml.safe_load((root / ".github" / "workflows" / s["workflow"]).read_text(encoding="utf-8"))
            inputs = (wf[True].get("workflow_dispatch") or {}).get("inputs") or {}
            self.assertIn("source", inputs, s["workflow"])
            for k, v in (s.get("inputs") or {}).items():
                self.assertIn(k, inputs, f"{s['name']}: {k}")
                opts = inputs[k].get("options")
                if opts:
                    self.assertIn(v, opts, f"{s['name']}: {k}={v}")
            job = sc.build_job(s, "owner/repo", "main", "tok")
            body = json.loads(job["extendedData"]["body"])
            self.assertEqual(body["inputs"]["source"], "punctual")
            self.assertTrue(job["schedule"]["hours"] and job["schedule"]["minutes"])

    def test_monitor_covers_tw_session(self):
        import json
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import setup_cronjob_org as sc
        root = Path(__file__).resolve().parents[1]
        slots = json.loads((root / "scripts" / "punctual_slots.json").read_text(encoding="utf-8"))["slots"]
        tw = sorted(f"{h:02d}:{m:02d}" for s in slots
                    if s["inputs"].get("market") == "monitor" and s["tz"] == "Asia/Taipei"
                    for h in sc.slot_times(s)[0] for m in sc.slot_times(s)[1])
        self.assertEqual(tw[0], "09:05")
        self.assertEqual(tw[-1], "13:05")
        self.assertEqual(len(tw), 9)


if __name__ == "__main__":
    unittest.main()
