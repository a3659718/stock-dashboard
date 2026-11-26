"""Offline tests for scripts/punctual_scheduler.py (no network: dispatch / clock / sleep are faked)."""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import punctual_scheduler as ps  # noqa: E402

UTC = dt.timezone.utc


class FireTimeTests(unittest.TestCase):
    def setUp(self):
        self.slots = ps.load_slots()

    def _names_between(self, a, b):
        return [(t.strftime("%m-%d %H:%M"), s["name"]) for t, s in ps.fire_times(self.slots, a, b)]

    def test_taipei_weekday_morning(self):
        # 2026-11-30 (週一) 台北 08:00~08:30 = UTC 00:00~00:30
        got = self._names_between(dt.datetime(2026, 11, 30, 0, 0, tzinfo=UTC),
                                  dt.datetime(2026, 11, 30, 0, 30, tzinfo=UTC))
        self.assertEqual(got, [("11-30 00:01", "morning_brief"), ("11-30 00:13", "pre_market_815"),
                               ("11-30 00:29", "pre_market_830")])

    def test_no_weekday_slots_on_saturday(self):
        # 2026-11-28 (週六) 台北 08:00~16:00 → 平日 slot 都不該觸發
        got = self._names_between(dt.datetime(2026, 11, 28, 0, 0, tzinfo=UTC),
                                  dt.datetime(2026, 11, 28, 8, 0, tzinfo=UTC))
        self.assertEqual(got, [])

    def test_us_dst_shift(self):
        # us_open 09:59 美東: 夏令 (2026-10-30 EDT) = 13:59 UTC; 冬令 (2026-11-30 EST) = 14:59 UTC
        def us_open_utc(day):
            a = dt.datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
            return [t.strftime("%H:%M") for t, s in ps.fire_times(self.slots, a, a + dt.timedelta(hours=6))
                    if s["name"] == "us_open"]
        self.assertEqual(us_open_utc(dt.date(2026, 10, 30)), ["13:59"])
        self.assertEqual(us_open_utc(dt.date(2026, 11, 30)), ["14:59"])

    def test_tw_monitor_every_30_min(self):
        got = [n for _, n in self._names_between(dt.datetime(2026, 11, 30, 1, 0, tzinfo=UTC),
                                                  dt.datetime(2026, 11, 30, 5, 10, tzinfo=UTC))
               if n.startswith("monitor_tw")]
        self.assertEqual(len(got), 9)   # 09:05 ~ 13:05


class RunLoopTests(unittest.TestCase):
    def _run(self, start, minutes, after=None):
        clock = {"now": start}
        calls = []

        def now():
            return clock["now"]

        def sleep(sec):
            clock["now"] += dt.timedelta(seconds=sec)

        def disp(repo, token, wf, ref, inputs):
            calls.append((clock["now"], wf, dict(inputs)))
            return True, "204"
        rc = ps.run(minutes, "o/r", "t", "main", now_fn=now, sleep_fn=sleep, dispatch_fn=disp, after=after)
        return rc, calls

    def test_dispatches_on_time_and_chains(self):
        start = dt.datetime(2026, 11, 30, 0, 0, tzinfo=UTC)   # 週一 台北 08:00
        rc, calls = self._run(start, 60)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][1], ps.SELF_WORKFLOW)                 # 先排下一輪接力
        self.assertIn("after", calls[0][2])
        fired = [(c[0].strftime("%H:%M"), c[2].get("slot") or c[2].get("market") or c[1]) for c in calls[1:]]
        self.assertEqual(fired, [("00:01", "morning_brief.yml"), ("00:13", "pre_market_815"),
                                 ("00:29", "pre_market_830")])
        self.assertTrue(all(c[2]["source"] == "punctual" for c in calls[1:]))

    def test_handoff_does_not_refire(self):
        # 上一輪做到 00:20 → 本輪 00:21 開跑, 00:13 那個不該再觸發, 00:29 要觸發
        start = dt.datetime(2026, 11, 30, 0, 21, tzinfo=UTC)
        _, calls = self._run(start, 30, after=dt.datetime(2026, 11, 30, 0, 20, tzinfo=UTC))
        names = [c[2].get("slot") for c in calls[1:]]
        self.assertEqual(names, ["pre_market_830"])

    def test_cold_start_catches_up_recent_slot(self):
        # 斷鏈後 00:18 才啟動 (沒有 after) → 補最近 10 分鐘內的 00:13
        start = dt.datetime(2026, 11, 30, 0, 18, tzinfo=UTC)
        _, calls = self._run(start, 20)
        self.assertEqual(calls[1][2].get("slot"), "pre_market_815")


if __name__ == "__main__":
    unittest.main()
