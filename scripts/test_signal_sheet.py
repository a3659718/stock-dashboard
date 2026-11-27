# -*- coding: utf-8 -*-
"""scripts/test_signal_sheet.py — signals 獨立 worksheet 的離線測試 (不打網路, 不需 secrets)

用假的 gspread worksheet 驗證:
  - 舊 blob 的 signals 只搬一次 (重跑不重複; 清 blob 失敗後重跑也不重複)
  - 兩個 runner 併發 append 不遺失 (含 compaction 進行中別人 append 的列)
  - sheet 讀 / 寫失敗時退回 blob 模式, 不會 crash, 之後再搬回 sheet
  - 讀取時依 signal_key 去重 (已驗收 > 未驗收; 不同 uuid 的同一訊號只算一筆)
  - record_batch 一批只打一次 append
  - Windows / Linux 檔案鎖 (#15)

用法: python scripts/test_signal_sheet.py   (exit 0 = 全過)
"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gspread.utils import a1_to_rowcol  # noqa: E402

from radar.core import signal_tracker as st  # noqa: E402
from radar.core import watchlist_store as ws  # noqa: E402


class FakeWorksheet:
    """最小 gspread Worksheet 替身: 一張 2D 字串格線, append 一律接在最後一列非空列之後。"""

    def __init__(self):
        self.grid = []          # list[list[str]]
        self.fail_read = False
        self.fail_append = False
        self.append_calls = 0
        self.before_update = None   # hook: compaction 改寫前呼叫 (模擬另一個 runner 插隊)

    # ---- 讀 ----
    def _trimmed(self):
        rows = [list(r) for r in self.grid]
        while rows and not any(str(c) for c in rows[-1]):
            rows.pop()
        return rows

    def get_all_values(self):
        if self.fail_read:
            raise RuntimeError("429 quota exceeded (fake)")
        width = max((len(r) for r in self._trimmed()), default=0)
        return [r + [""] * (width - len(r)) for r in self._trimmed()]

    def acell(self, label):
        r, c = a1_to_rowcol(label)
        v = self.grid[r - 1][c - 1] if r <= len(self.grid) and c <= len(self.grid[r - 1]) else ""

        class _Cell:
            value = v or None
        return _Cell()

    # ---- 寫 ----
    def _set(self, r, c, v):
        while len(self.grid) < r:
            self.grid.append([])
        row = self.grid[r - 1]
        while len(row) < c:
            row.append("")
        row[c - 1] = v

    def update_acell(self, label, value):
        r, c = a1_to_rowcol(label)
        self._set(r, c, str(value))

    def update(self, values=None, range_name=None, **_kw):
        if self.before_update is not None:
            hook, self.before_update = self.before_update, None
            hook()
        r0, c0 = a1_to_rowcol(range_name.split(":")[0])
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                self._set(r0 + i, c0 + j, v)

    def append_rows(self, values, value_input_option=None, insert_data_option=None, table_range=None):
        assert value_input_option == "RAW"
        assert insert_data_option == "INSERT_ROWS"
        self.append_calls += 1
        if self.fail_append:
            raise RuntimeError("503 backend error (fake)")
        n = len(self._trimmed())
        self.grid[n:n] = [list(map(str, row)) for row in values]

    def delete_rows(self, start, end=None):
        end = end or start
        del self.grid[start - 1:end]

    def clear(self):
        self.grid = []

    # ---- 測試輔助 ----
    def data_rows(self):
        return [r for r in self._trimmed()[1:] if any(str(c) for c in r)]


def _rec(sid, typ="morning_tw_top5", day=None, hit=None, rid=None, **kw):
    day = day or st._today_str()
    r = {"id": rid or f"id{sid}{typ[:3]}", "signal_type": typ, "stock_id": sid, "name": "",
         "predicted_at": day, "predicted_price": 100.0, "expected_direction": "up",
         "evaluate_after_days": 1, "evaluate_at": day, "actual_price": None,
         "actual_date": None, "actual_pct": None, "hit": hit, "extras": {"market": "TW"}}
    if hit is not None:
        r.update(actual_price=101.0, actual_date=day, actual_pct=1.0)
    r.update(kw)
    return r


class SignalSheetBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "monitor_state.json"
        self.sheet = FakeWorksheet()
        self.sheet_available = True
        self.patches = [
            patch.object(ws, "MONITOR_STATE_FILE", self.file),
            # monitor_state 走本地檔 (None), signals 走假 worksheet
            patch.object(ws, "_get_sheet", side_effect=self._get_sheet),
            patch.object(ws, "_SIG_LEASE_SETTLE_SEC", 0),
            patch("time.sleep", lambda *_a, **_k: None),
        ]
        for p in self.patches:
            p.start()
        ws._reset_signal_sheet_cache()
        ws._LOAD_SNAPSHOTS.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        ws._reset_signal_sheet_cache()
        self.tmp.cleanup()

    def _get_sheet(self, name, rows=200, cols=10):
        if name == ws.SIGNAL_SHEET_NAME and self.sheet_available:
            return self.sheet
        return None

    def _write_blob(self, state):
        self.file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def _blob(self):
        return json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}

    def _new_process(self):
        """模擬另一個 runner / 下一次 run: process 內快取全清。"""
        ws._reset_signal_sheet_cache()
        ws._LOAD_SNAPSHOTS.clear()
        st.clear_bars_cache()


class MigrationTests(SignalSheetBase):
    def test_migrates_once_and_stops_writing_blob(self):
        legacy = [_rec("2330", day="2026-10-01", hit=True), _rec("2454", day="2026-10-02"),
                  _rec("0050", day="2026-10-03")]
        self._write_blob({"signals": legacy, "slot_dedup": {"a": "2026-11-01T00:00:00"}})
        recs = ws.load_signal_records()
        self.assertEqual(len(recs), 3)
        self.assertEqual(len(self.sheet.data_rows()), 3)
        self.assertEqual(self._blob()["signals"], [])                  # blob 已清空
        self.assertEqual(self._blob()["slot_dedup"], {"a": "2026-11-01T00:00:00"})  # 其他 key 不動
        got = {r["stock_id"]: r for r in recs}
        self.assertEqual(got["0050"]["stock_id"], "0050")              # 前導零保留
        self.assertIs(got["2330"]["hit"], True)
        self.assertEqual(got["2330"]["actual_pct"], 1.0)
        self.assertEqual(got["2454"]["extras"], {"market": "TW"})

        # 重跑 (新 process): 不重複搬
        self._new_process()
        self.assertEqual(len(ws.load_signal_records()), 3)
        self.assertEqual(len(self.sheet.data_rows()), 3)

        # 清 blob 失敗的情境 (blob 還留著舊 signals) → 重跑: 不新增列, 只把 blob 清掉
        self._write_blob({"signals": legacy})
        self._new_process()
        self.assertEqual(len(ws.load_signal_records()), 3)
        self.assertEqual(len(self.sheet.data_rows()), 3)
        self.assertEqual(self._blob()["signals"], [])

        # 之後的新訊號只進 sheet, 不寫 blob
        self._new_process()
        st.record_signal("catalyst", "2603", predicted_price=50.0)
        self.assertEqual(self._blob().get("signals"), [])
        self.assertEqual(len(self.sheet.data_rows()), 4)
        self.assertIn("2603", {r["stock_id"] for r in st.load_records()})

    def test_legacy_resolved_beats_sheet_pending(self):
        today = st._today_str()
        self.sheet.grid = [list(ws.SIGNAL_COLS)]
        self.sheet.append_rows([ws._sig_to_row(_rec("2330", day=today), ws.SIGNAL_COLS)],
                               value_input_option="RAW", insert_data_option="INSERT_ROWS")
        self._write_blob({"signals": [_rec("2330", day=today, hit=False)]})
        recs = ws.load_signal_records()
        self.assertEqual(len(recs), 1)
        self.assertIs(recs[0]["hit"], False)


class ConcurrencyTests(SignalSheetBase):
    def test_two_runners_append_without_loss(self):
        self._write_blob({})
        # 兩個 runner 幾乎同時讀到同一份 (空的) 表
        a_view = ws.load_signal_records(fresh=True)
        self._new_process()
        b_view = ws.load_signal_records(fresh=True)
        self.assertEqual(a_view, [])
        self.assertEqual(b_view, [])
        # 各自 append 自己的紀錄 —— 不是整張覆寫, 所以兩邊都留下
        self.assertTrue(ws.append_signal_records([_rec("2330")]))
        self._new_process()
        self.assertTrue(ws.append_signal_records([_rec("NVDA", typ="us_buy_picks")]))
        self._new_process()
        ids = {r["stock_id"] for r in ws.load_signal_records()}
        self.assertEqual(ids, {"2330", "NVDA"})

    def test_same_signal_recorded_by_two_runners_counts_once(self):
        self._write_blob({})
        st.record_signal("catalyst", "2330", predicted_price=10.0)
        # 另一個 runner 在看到上面那筆之前就記了同一檔 (不同 uuid) → 直接 append 一列
        ws.append_signal_records([_rec("2330", typ="catalyst", rid="other123")])
        self._new_process()
        recs = [r for r in st.load_records() if r["signal_type"] == "catalyst"]
        self.assertEqual(len(recs), 1)

    def test_record_batch_single_append_call(self):
        self._write_blob({})
        n = st.record_batch("pre_market_buy", [{"stock_id": s, "current": 10.0}
                                               for s in ("2330", "2454", "2317", "2603", "3008")],
                            evaluate_after_days=1)
        self.assertEqual(n, 5)
        self.assertEqual(self.sheet.append_calls, 1)
        self.assertEqual(len(self.sheet.data_rows()), 5)
        # 同一天同一批重記 → 不新增, 不打 append
        n2 = st.record_batch("pre_market_buy", [{"stock_id": "2330", "current": 11.0}])
        self.assertEqual(n2, 1)
        self.assertEqual(self.sheet.append_calls, 1)

    def test_evaluation_appends_new_version_and_wins(self):
        self._write_blob({})
        st.record_signal("catalyst", "2330", predicted_price=10.0)
        rid = st.load_records()[0]["id"]

        def _mut(records):
            for r in records:
                if r["id"] == rid:
                    r.update(hit=True, actual_price=11.0, actual_pct=10.0, actual_date="2026-11-30")
                    return 1
            return 0
        st._mutate_signals_atomically(_mut)
        self.assertEqual(len(self.sheet.data_rows()), 2)              # 新版本是 append 的
        self._new_process()
        recs = st.load_records()
        self.assertEqual(len(recs), 1)
        self.assertIs(recs[0]["hit"], True)
        self.assertEqual(st.pending_count(), 0)

    def test_compaction_keeps_rows_appended_concurrently(self):
        self._write_blob({})
        today = ws._tpe_today()
        old_day = (today - dt.timedelta(days=ws.SIGNAL_RETENTION_DAYS + 60)).strftime("%Y-%m-%d")
        hdr = list(ws.SIGNAL_COLS)
        rows = [hdr, ws._sig_to_row(_rec("OLD1", day=old_day, hit=True), hdr)]
        # 300 筆訊號, 每筆有「未驗收 + 已驗收」兩個版本 → 300 列重複版本 → 觸發 compaction
        for i in range(300):
            rows.append(ws._sig_to_row(_rec(f"S{i}", day="2026-11-01"), hdr))
            rows.append(ws._sig_to_row(_rec(f"S{i}", day="2026-11-01", hit=True), hdr))
        self.sheet.grid = rows

        # compaction 改寫前, 另一個 runner append 了一筆新的
        def _other_runner():
            self.sheet.append_rows([ws._sig_to_row(_rec("LATE"), hdr)],
                                   value_input_option="RAW", insert_data_option="INSERT_ROWS")
        self.sheet.before_update = _other_runner

        recs = ws.load_signal_records(fresh=True)
        self.assertNotIn("OLD1", {r["stock_id"] for r in recs})      # 超過保留期限被裁
        self._new_process()
        after = {r["stock_id"]: r for r in ws.load_signal_records()}
        self.assertIn("LATE", after)                                  # 併發 append 的列沒被蓋/刪
        self.assertNotIn("OLD1", after)
        self.assertEqual(len(after), 301)
        self.assertTrue(all(after[f"S{i}"]["hit"] is True for i in range(300)))
        self.assertEqual(len(self.sheet.data_rows()), 301)            # 重複版本已清掉
        self.assertFalse(self.sheet.acell(ws._SIG_LEASE_CELL).value)  # lease 已釋放

    def test_compaction_skipped_when_lease_held(self):
        self._write_blob({})
        hdr = list(ws.SIGNAL_COLS)
        rows = [hdr]
        for i in range(300):
            rows.append(ws._sig_to_row(_rec(f"S{i}", day="2026-11-01"), hdr))
            rows.append(ws._sig_to_row(_rec(f"S{i}", day="2026-11-01", hit=True), hdr))
        self.sheet.grid = rows
        self.sheet.update_acell(ws._SIG_LEASE_CELL,
                                dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S") + "|other")
        ws.load_signal_records(fresh=True)
        self.assertEqual(len(self.sheet.data_rows()), 600)            # 別人持有 lease → 不動


class FallbackTests(SignalSheetBase):
    def test_read_error_falls_back_to_blob(self):
        self._write_blob({"signals": []})
        self.sheet.fail_read = True
        self.assertIsNone(ws.load_signal_records())
        rid = st.record_signal("catalyst", "2330", predicted_price=10.0)  # 不可 crash
        self.assertTrue(rid)
        self.assertEqual([r["stock_id"] for r in self._blob()["signals"]], ["2330"])
        self.assertEqual(self.sheet.append_calls, 0)                  # 讀失敗就不寫 sheet
        self.assertEqual(len(st.load_records()), 1)

        # sheet 恢復 → 下一個 process 把 fallback 期間寫進 blob 的搬回 sheet
        self.sheet.fail_read = False
        self._new_process()
        self.assertEqual([r["stock_id"] for r in st.load_records()], ["2330"])
        self.assertEqual(len(self.sheet.data_rows()), 1)
        self.assertEqual(self._blob()["signals"], [])

    def test_no_sheet_at_all_keeps_local_json_mode(self):
        self.sheet_available = False
        self._write_blob({})
        st.record_batch("pre_market_buy", [{"stock_id": "2330", "current": 10.0},
                                           {"stock_id": "2454", "current": 20.0}])
        self.assertEqual(len(self._blob()["signals"]), 2)
        self.assertEqual(len(st.load_records()), 2)
        self.assertEqual(st.reset_all(), 2)
        self.assertEqual(self._blob()["signals"], [])

    def test_append_error_parks_changes_in_blob(self):
        self._write_blob({})
        self.sheet.fail_append = True
        st.record_signal("catalyst", "2330", predicted_price=10.0)    # 不可 crash
        self.assertEqual([r["stock_id"] for r in self._blob()["signals"]], ["2330"])
        self.sheet.fail_append = False
        self._new_process()
        self.assertEqual([r["stock_id"] for r in st.load_records()], ["2330"])
        self.assertEqual(len(self.sheet.data_rows()), 1)

    def test_unexpected_exception_never_crashes(self):
        self._write_blob({})
        with patch.object(ws, "load_signal_records", side_effect=RuntimeError("boom")):
            self.assertTrue(st.record_signal("catalyst", "2330", predicted_price=10.0))
            self.assertEqual(len(st.load_records()), 1)


class DedupeTests(SignalSheetBase):
    def test_dedupe_on_read(self):
        hdr = list(ws.SIGNAL_COLS)
        resolved = _rec("2330", day="2026-11-01", hit=True, updated_at="2026-11-02T07:00:00")
        pending = _rec("2330", day="2026-11-01", updated_at="2026-11-03T07:00:00")  # 較新但未驗收
        other_id = _rec("2330", day="2026-11-01", rid="zzz", updated_at="2026-11-01T01:00:00")
        diff_type = _rec("2330", typ="catalyst", day="2026-11-01")
        self.sheet.grid = [hdr] + [ws._sig_to_row(r, hdr) for r in
                                   (pending, resolved, other_id, diff_type)]
        self._write_blob({})
        recs = ws.load_signal_records()
        self.assertEqual(len(recs), 2)
        top5 = [r for r in recs if r["signal_type"] == "morning_tw_top5"][0]
        self.assertIs(top5["hit"], True)                              # 已驗收 > 未驗收

    def test_dedupe_prefers_newer_update_same_rank(self):
        a = _rec("2330", day="2026-11-01", hit=False, updated_at="2026-11-02T07:00:00")
        b = _rec("2330", day="2026-11-01", hit=True, updated_at="2026-11-02T08:00:00")
        self.assertIs(ws._dedupe_signals([b, a])[0]["hit"], True)
        self.assertIs(ws._dedupe_signals([a, b])[0]["hit"], True)

    def test_row_roundtrip(self):
        r = _rec("0050", hit=False, name="元大台灣50", extras={"market": "TW", "asof": "2026-11-01"})
        r["actual_pct"] = -1.25
        idx = {c: i for i, c in enumerate(ws.SIGNAL_COLS)}
        back = ws._row_to_sig(ws._sig_to_row(r, ws.SIGNAL_COLS), idx)
        for k in ("id", "stock_id", "name", "predicted_at", "predicted_price", "hit",
                  "actual_pct", "extras", "evaluate_after_days", "expected_direction"):
            self.assertEqual(back[k], r[k], k)


class FileLockTests(unittest.TestCase):
    """#15: Windows 上原本把 int fd 傳給 portalocker → 永遠無鎖。"""

    def test_lock_acquired_and_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "x.lock")
            h1, k1 = st._try_lock(path)
            try:
                self.assertIsNotNone(k1)                              # 真的拿到鎖
                with patch("time.sleep", lambda *_a: None):
                    h2, k2 = st._try_lock(path)
                self.assertIsNone(k2)                                 # 第二個拿不到 (排他)
                st._release_lock(h2, k2)
            finally:
                st._release_lock(h1, k1)
            h3, k3 = st._try_lock(path)                               # 釋放後可再拿
            self.assertIsNotNone(k3)
            st._release_lock(h3, k3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
