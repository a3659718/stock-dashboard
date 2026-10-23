"""
watchlist_store.py
自選股持久化 + monitor state 管理。

儲存策略:
  1. 優先 Google Sheets (跨環境共享, 永久保存)
  2. Fallback: local JSON (Streamlit Cloud / GitHub Actions ephemeral)

資料格式:
  watchlist.json: [{stock_id, name, market, entry_price, added_date}, ...]
  monitor_state.json: {
    "watchlist_alerts": {stock_id: {last_pct: int, base_price: float}},
    "index_alerts": {symbol: {last_level: float, last_direction: str}},
    "crypto_alerts": {symbol: {last_pct: float, base_price: float, base_date: str}},
  }
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st

import data_sources as ds


WATCHLIST_FILE = Path("watchlist.json")
MONITOR_STATE_FILE = Path("monitor_state.json")
MAX_WATCHLIST = 15


# ===================================================================
# I1 fix: 批次 state I/O (避免每 check 都打 GSheet API 一次)
# ===================================================================
# 預設 mode: load_monitor_state / save_monitor_state 各打一次 GSheet (4 reads + 6 writes/tick)
# 批次 mode (in batched_state_writes() context):
#   - 第一次 load 從 GSheet 抓, 之後從 cache 拿
#   - save 只更新 cache, 不立即 GSheet write
#   - context 結束時 atomic flush 一次 GSheet write
# = 從每 tick ~10 個 GSheet API call 降到 2 個 (1 read + 1 write)
_BATCH_MODE = False
_BATCH_CACHE: Optional[Dict] = None
_BATCH_DIRTY = False
# Bug fix: batch mode 下 _BATCH_CACHE 是「進入 batch 時的整份 state 快照 + 本次改動」,
# 直接整包丟給 _flush_state 會讓它的 top-level shallow merge 把「monitor 執行期間其他
# workflow 寫入的 key」全部回捲到快照時的值。留一份原始快照, close 時只 flush 真的改過的
# top-level key (詳見 close_batched_state)。
_BATCH_ORIG: Optional[Dict] = None


@contextmanager
def batched_state_writes():
    """Context manager 版 (給可以 indent 的新 code 用).

    在 context 內: load 共享 cache, save 只更新 cache, 不立即寫 GSheet.
    Context 結束時一次性 flush.

    For existing code with deep nested try/except/return, use open_batched_state()
    + close_batched_state() + try/finally 的 pattern.
    """
    open_batched_state()
    try:
        yield
    finally:
        close_batched_state()


def open_batched_state() -> None:
    """開啟 batched mode. 必須跟 close_batched_state 配對 (用 try/finally 保護)."""
    global _BATCH_MODE, _BATCH_CACHE, _BATCH_DIRTY, _BATCH_ORIG
    if _BATCH_MODE:
        # 巢狀 open — 已在 batch 內, no-op
        return
    _BATCH_MODE = True
    _BATCH_CACHE = None
    _BATCH_ORIG = None
    _BATCH_DIRTY = False


def close_batched_state() -> None:
    """關閉 batched mode + flush 已修改的 state. Idempotent (重複呼叫 OK)."""
    global _BATCH_MODE, _BATCH_CACHE, _BATCH_DIRTY, _BATCH_ORIG
    if not _BATCH_MODE:
        return  # 不在 batch 內 (重複呼叫 / 沒開過)
    _BATCH_MODE = False
    if _BATCH_DIRTY and _BATCH_CACHE is not None:
        try:
            # Bug fix: 原本把整份 _BATCH_CACHE 丟進 _flush_state。_BATCH_CACHE 是 monitor
            # 啟動瞬間的整份 state 快照 (load 時 deepcopy) 加上本次改動, 而 _flush_state 的
            # merge 是 top-level shallow「我們手上的 key 覆蓋持久化版本」→ 等於把 monitor
            # 執行期間其他 workflow 寫入的 top-level key 全部回捲到啟動時的值 (只有
            # slot_dedup 因為 union 特例倖存)。monitor 單次可達數分鐘, 與 tw_close 15:03 /
            # pre_market / tw_foreign_chips 重疊時, secondary_push_cap 的每日計數、
            # alert_dedup、strong_stock_alert[today]、holdings_tracker 的 fired_date
            # 都會被回滾 → 推播超過每日上限、同一則重複推。
            # 改成只 flush「本次真的改過的 top-level key」, 其餘交給 merge 保留持久化版本。
            # 註: 沒改過的 key 這次就不會被 _prune_date_keys 清 —— 體積防線改由下一次
            #     非 batch 寫入 (或下次真的改到那個 key 時) 處理, 不影響正確性。
            _payload = _BATCH_CACHE
            if isinstance(_BATCH_ORIG, dict):
                _payload = {k: v for k, v in _BATCH_CACHE.items()
                            if k not in _BATCH_ORIG or _BATCH_ORIG.get(k) != v}
            if _payload:
                _flush_state(_payload)
            else:
                print("[watchlist_store] batched flush: 無實際變更, 略過寫入", flush=True)
        except Exception as _e:
            print(f"[watchlist_store] batched flush failed: {_e}", flush=True)
    _BATCH_CACHE = None
    _BATCH_ORIG = None
    _BATCH_DIRTY = False


def _read_persisted_raw() -> Dict:
    """直接從 backend 讀目前持久化的 monitor_state (不碰 batch cache) — 給 _flush_state merge 用."""
    sheet = _get_sheet("monitor_state")
    if sheet is not None:
        try:
            cell = sheet.acell("A1").value
            if cell:
                return json.loads(cell)
        except Exception:
            pass
    if MONITOR_STATE_FILE.exists():
        try:
            return json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# monitor_state 體積防線 (2026-09-20)
# ---------------------------------------------------------------------------
# 背景: 整份 monitor_state 以一個 JSON 字串存在 Google Sheet 的「單一儲存格」(A1),
# 而 Google Sheets 單格上限是 50,000 字元。state 裡有 5 個「以日期當 key」的累積器
# 完全沒有任何清理, 每個交易日各長一個 key:
#     tx_futures_alert[YYYY-MM-DD]              (tx_futures_alert.py:267)
#     strong_stock_alert[YYYY-MM-DD]            (strong_stock_alert.py:293)
#     intraday_strong_alert[YYYY-MM-DD]         (strong_stock_alert.py:533)
#     intraday_weak_alert[YYYY-MM-DD]           (short_candidates.py:244)
#     trump_policy_alert.daily_count[YYYY-MM-DD](trump_policy_alert.py:632)
# 再加上原本用 indent=2 序列化 (實測比 compact 大 ~40%), 估算約 500 字元/交易日 →
# 一年 ~12 萬字元 → 幾個月內就會撐爆 50,000。
#
# 撐爆後的失效模式才是真正危險的地方: update_acell 拋例外只被 except 印一行 log,
# 但「讀取」仍然成功 (A1 還留著最後一份合法 blob) → 全系統從此讀到一份凍結的 state:
#   - push_cap 每次看到 date 不符 → 重置 count=0 → 每日推播上限等於取消
#   - slot_dedup / alert_dedup 凍結 → 備援 cron 與跨類去重各自重推
# 也就是「某一天開始被推播洗版, 而 log 裡只有一行 gsheets update failed」。
#
# 這裡集中處理三件事, 讓它不再是靜默失敗:
#   1. 緊湊序列化 (separators) — 對現況實測省 ~29%, 日期 key 越多省越多
#   2. 泛用 prune — 掃出所有「YYYY-MM-DD」形狀的 key 依保留天數清掉 (含未來新增的)
#   3. 體積告警 — 超過軟門檻就大聲印出來, 不要等它撞上硬上限
_STATE_SOFT_LIMIT = 35000      # 超過就告警
_STATE_HARD_LIMIT = 50000      # Google Sheets 單格硬上限
_DATE_KEY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 保留天數例外 — 跟該模組自己的邏輯一致 (holdings_tracker 自己就留 60 天給準確率統計)
_PRUNE_RETENTION_DAYS = {"holdings_tracker.predictions": 60}
_PRUNE_DEFAULT_DAYS = 21


def _prune_date_keys(state: Dict) -> int:
    """清掉 state 裡所有「以日期當 key」且過期的項目, 回傳清掉幾個。

    只處理深度 1 與 2 (state[k] 或 state[k][k2] 本身是 {日期: ...} 的 dict),
    其餘結構一概不動。日期以 TPE 為準 (與各模組寫入時一致)。
    這支是泛用的 — 日後任何新模組再開一個日期累積器, 不必改這裡就會被涵蓋。
    """
    removed = 0
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()

    def _prune_one(d: Dict, path: str) -> int:
        keys = [k for k in d.keys() if isinstance(k, str) and _DATE_KEY_RE.match(k)]
        if not keys:
            return 0
        days = _PRUNE_RETENTION_DAYS.get(path, _PRUNE_DEFAULT_DAYS)
        cutoff = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        cnt = 0
        for k in keys:
            if k < cutoff:
                d.pop(k, None)
                cnt += 1
        return cnt

    for k, v in list(state.items()):
        if not isinstance(v, dict):
            continue
        removed += _prune_one(v, k)
        for k2, v2 in list(v.items()):
            if isinstance(v2, dict):
                removed += _prune_one(v2, f"{k}.{k2}")
    return removed


def _flush_state(state: Dict) -> None:
    """實際寫入 state (本地檔案 + GSheet). 跟舊版 save_monitor_state 邏輯相同.

    Bug fix (lost-update): 各模組各自管自己的 top-level key (push_cap→secondary_push_cap /
    push_dedup→slot_dedup / alert_priority→alert_dedup …), 但原本每次都「整包覆寫」, 後寫的
    會蓋掉前一個模組/另一個併發 runner 剛寫的 key → 去重表/每日上限/計數器無聲遺失 → 重複或漏推。
    這裡寫入前先讀回目前持久化的版本, 做 shallow top-level merge: 我們手上的 key 覆蓋, 其餘保留。
    monitor_state 沒有任何「刪頂層 key」的用法 (刪除都在子 dict 內), 故 merge 安全。
    註: 仍非真正的鎖, 同一 key 的併發更新 (e.g. 兩個 runner 同時 +1 計數) 仍可能 last-writer-wins;
        但「不同 key 互相覆蓋」這個主要 lost-update 已消除。
    """
    try:
        persisted = _read_persisted_raw()
        if isinstance(persisted, dict) and persisted:
            merged = dict(persisted)
            merged.update(state)  # 我們的 top-level key 覆蓋, 其餘 (別人寫的) 保留
            # 併發安全 (slot_dedup): 這個 key 只會「新增 claim + prune 舊的」, 沒有語意刪除,
            # 所以兩個 runner 的 claim 用 union 合併 (同 slot 取較新 ISO 時間) → 不會因 last-writer-wins
            # 丟掉對方剛寫的 dedup claim (那會導致同一 slot 被重複推播)。其餘 key 維持 shallow merge。
            try:
                _pd_a = persisted.get("slot_dedup") or {}
                _pd_b = state.get("slot_dedup") or {}
                if isinstance(_pd_a, dict) and isinstance(_pd_b, dict) and (_pd_a or _pd_b):
                    _u = dict(_pd_a)
                    for _k, _v in _pd_b.items():
                        if _k not in _u or str(_v) > str(_u[_k]):  # 同格式 ISO 字串比大小 = 比時間
                            _u[_k] = _v
                    merged["slot_dedup"] = _u
            except Exception:
                pass
            state = merged
    except Exception as _me:
        print(f"[watchlist_store] monitor_state merge skip (non-fatal): {_me}", flush=True)
    # 體積防線 (見上方 _prune_date_keys 的說明): 先清過期日期 key, 再緊湊序列化。
    try:
        _pruned = _prune_date_keys(state)
        if _pruned:
            print(f"[watchlist_store] monitor_state prune: 清掉 {_pruned} 個過期日期 key",
                  flush=True)
    except Exception as _pe:
        print(f"[watchlist_store] monitor_state prune skip (non-fatal): {_pe}", flush=True)
    # separators 去掉 indent 的空白 — 同樣內容, 對現況實測省 ~29%。
    # (要人眼看 monitor_state.json 時用 `python -m json.tool monitor_state.json`)
    blob = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(blob) > _STATE_SOFT_LIMIT:
        print(f"[watchlist_store] ⚠️ monitor_state 已達 {len(blob)} 字元 / 上限 "
              f"{_STATE_HARD_LIMIT} (Google Sheets 單格)。超過上限後 GSheet 寫入會失敗而"
              f"讀取仍成功 → 去重表與每日上限會凍結在舊值, 症狀是推播洗版。"
              f"請檢查是哪個 key 在長大。", flush=True)
    try:
        _atomic_write_text(MONITOR_STATE_FILE, blob)
    except Exception as _e:
        print(f"[watchlist_store] monitor_state local write failed: {_e}", flush=True)
    sheet = _get_sheet("monitor_state")
    if sheet is not None:
        try:
            # Bug fix: 原本 clear() 再 update_acell, 中間有空窗, 併發 reader 會讀到空 → state 當預設值
            #          (去重表/每日上限瞬間被當成清空). update_acell 本就會整格覆寫, clear() 多餘且有害, 移除.
            sheet.update_acell("A1", blob)
        except Exception as _e:
            print(f"[watchlist_store] monitor_state gsheets update failed: {_e}", flush=True)


def _atomic_write_text(path: Path, blob: str) -> None:
    """Atomic 寫入 — 寫到 .tmp 後 os.replace, 防併發 cron 互相覆蓋.

    GH Actions cron 啟動有 ~30s 抖動, monitor (每 30 min) + tw_close (07:00 UTC)
    可能在同分鐘觸發. 用 rename 的 atomic 性, 確保不會看到半寫狀態.
    """
    import tempfile
    parent = path.parent if str(path.parent) else Path(".")
    parent.mkdir(parents=True, exist_ok=True)
    # mkstemp 在同 dir 才能 atomic rename (跨 fs 會 fall back 到 copy)
    fd, tmp_str = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(blob)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_str, str(path))
    except Exception:
        # 寫失敗 — 清掉 tmp
        try:
            os.unlink(tmp_str)
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Watchlist (15 檔上限)
# ---------------------------------------------------------------------------
def load_watchlist() -> List[Dict]:
    """讀取自選股. 優先 Google Sheets, fallback local JSON."""
    sheet = _get_sheet("watchlist")
    if sheet is not None:
        try:
            data = sheet.get_all_records()
            if data:
                return [_normalize_item(d) for d in data][:MAX_WATCHLIST]
        except Exception:
            pass
    # Fallback: local
    if WATCHLIST_FILE.exists():
        try:
            data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            return [_normalize_item(d) for d in data][:MAX_WATCHLIST]
        except Exception:
            return []
    return []


def load_watchlist_ids(market: Optional[str] = None) -> List[str]:
    """只取自選股的「代號字串」清單 — 給所有 alert 模組掃描 universe 用。

    為什麼要有這支 (2026-08 修正):
      load_watchlist() 回的是 dict 陣列 ({"stock_id","name","market",...}), 但 8 個 alert 模組
      都把它當成「代號字串陣列」直接用, 兩種壞法都是靜默的:
        (a) str(dict) → "{'STOCK_ID': '6669', ...}", .isdigit() 為 False → 被當美股代號丟給
            yfinance, 必然抓不到;
        (b) list(dict.fromkeys(universe + wl)) → TypeError: unhashable type: 'dict',
            被外層 except 吞掉 → 整份自選股靜靜消失, log 完全沒有痕跡。
      結果是使用者加進自選股的標的, 量爆/籌碼/新聞/突破/強弱勢/大盤警報全都沒真的掃到。
      統一走這支, 之後新增模組也不會再踩到同一個坑。

    market: "TW" / "US" → 只回該市場; None → 全部。
      市場判定優先用 item 自己的 market 欄位, 沒有才退回「純數字 = 台股」的猜法。
    """
    out: List[str] = []
    for item in (load_watchlist() or []):
        try:
            if isinstance(item, dict):
                sid = str(item.get("stock_id", "")).strip().upper()
                mkt = str(item.get("market", "")).strip().upper()
            else:
                # 相容: 萬一哪天來源退化成字串陣列
                sid = str(item).strip().upper()
                mkt = ""
            if not sid:
                continue
            if not mkt:
                mkt = "TW" if sid.isdigit() else "US"
            if market and mkt != str(market).strip().upper():
                continue
            if sid not in out:
                out.append(sid)
        except Exception:
            continue
    return out


def _write_sheet_grid(sheet, cols: List[str], items: List[Dict], max_rows: int) -> None:
    """單次 update() 覆寫整張表 (含 header), 完全不呼叫 clear().

    Bug fix (race condition): 原本 save_watchlist/save_holdings 是 sheet.clear()
    後逐列 append_row() — clear 跟寫完之間有空窗, 若這時另一個 Streamlit session
    或 cron 剛好 load_watchlist()/load_holdings(), 會讀到清空後、尚未寫完的表,
    把使用者的自選股/持倉當成空清單; 若 append_row 迴圈中途因網路逾時 / API
    額度失敗而中斷, 表上會永久留下清空且不完整的資料, 直到下次成功儲存為止。
    改法: 把資料 pad 到固定的 max_rows (含 header), 一次 update() 整個範圍覆寫,
    跟 monitor_state._flush_state 的修法一致 — 沒有任何「先清空」的中間狀態,
    舊資料多出的列也會被這次的空白列一起蓋掉, 不會殘留。
    """
    header = list(cols)
    rows = [header] + [[str(d.get(c, "")) for c in cols] for d in items]
    n_cols = len(cols)
    total_rows = max_rows + 1  # +1 for header row
    while len(rows) < total_rows:
        rows.append([""] * n_cols)
    last_col = chr(ord("A") + n_cols - 1)
    range_name = f"A1:{last_col}{total_rows}"
    sheet.update(values=rows, range_name=range_name)


def save_watchlist(items: List[Dict]) -> bool:
    """儲存自選股. 同步到 Sheets + local."""
    items = items[:MAX_WATCHLIST]
    items = [_normalize_item(d) for d in items]

    # Local (atomic)
    try:
        _atomic_write_text(
            WATCHLIST_FILE,
            json.dumps(items, ensure_ascii=False, indent=2),
        )
    except Exception as _e:
        print(f"[watchlist_store] local write failed: {_e}", flush=True)

    # Sheets
    sheet = _get_sheet("watchlist")
    if sheet is not None:
        try:
            cols = ["stock_id", "name", "market", "entry_price", "added_date"]
            _write_sheet_grid(sheet, cols, items, MAX_WATCHLIST)
            return True
        except Exception as _e:
            print(f"[watchlist_store] gsheets save_watchlist failed: {_e}", flush=True)
    return True


def add_to_watchlist(stock_id: str, name: str = "", market: str = "TW",
                      entry_price: Optional[float] = None) -> bool:
    """加 1 檔. 已存在則更新."""
    items = load_watchlist()
    sid = str(stock_id).strip().upper()
    if not sid:
        return False
    # 已存在則更新
    found = False
    for i in items:
        if str(i.get("stock_id", "")).upper() == sid:
            i["name"] = name or i.get("name", "")
            if entry_price is not None:
                i["entry_price"] = entry_price
            found = True
            break
    if not found:
        if len(items) >= MAX_WATCHLIST:
            return False  # 滿了
        items.append({
            "stock_id": sid,
            "name": name,
            "market": market.upper(),
            "entry_price": entry_price,
            "added_date": dt.date.today().strftime("%Y-%m-%d"),
        })
    return save_watchlist(items)


def remove_from_watchlist(stock_id: str) -> bool:
    items = load_watchlist()
    sid = str(stock_id).strip().upper()
    items = [i for i in items if str(i.get("stock_id", "")).upper() != sid]
    save_watchlist(items)
    # 同時清掉這檔的 alert state
    state = load_monitor_state()
    if "watchlist_alerts" in state and sid in state["watchlist_alerts"]:
        del state["watchlist_alerts"][sid]
        save_monitor_state(state)
    return True


def _normalize_item(d: Dict) -> Dict:
    out = {
        "stock_id": str(d.get("stock_id", "")).strip().upper(),
        "name": str(d.get("name", "")).strip(),
        "market": str(d.get("market", "TW")).strip().upper(),
        "entry_price": d.get("entry_price"),
        "added_date": str(d.get("added_date", "")).strip(),
    }
    try:
        out["entry_price"] = float(out["entry_price"]) if out["entry_price"] not in (None, "") else None
    except Exception:
        out["entry_price"] = None
    return out


# ---------------------------------------------------------------------------
# Monitor state
# ---------------------------------------------------------------------------
def load_monitor_state() -> Dict:
    """讀全部 monitor state. 結構:
    {
      "watchlist_alerts": {sid: {last_pct: int, base_price: float}},
      "index_alerts": {sym: {last_level: float, last_direction: str, last_alert: str}},
      "crypto_alerts": {sym: {last_pct: float, base_price: float, base_date: str}},
    }

    I1 fix: 在 batched_state_writes() context 內共享 cache, 避免重複 GSheet read.
    """
    global _BATCH_CACHE, _BATCH_ORIG
    # 在 batch mode 內 — 直接拿 cache 副本
    if _BATCH_MODE and _BATCH_CACHE is not None:
        return copy.deepcopy(_BATCH_CACHE)

    # 不在 batch mode 或 cache 還沒裝 — 走實際 load
    sheet = _get_sheet("monitor_state")
    if sheet is not None:
        try:
            cell = sheet.acell("A1").value
            if cell:
                loaded = json.loads(cell)
                if _BATCH_MODE:
                    _BATCH_CACHE = copy.deepcopy(loaded)
                    _BATCH_ORIG = copy.deepcopy(loaded)
                return loaded
        except Exception:
            pass
    if MONITOR_STATE_FILE.exists():
        try:
            loaded = json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
            if _BATCH_MODE:
                _BATCH_CACHE = copy.deepcopy(loaded)
                _BATCH_ORIG = copy.deepcopy(loaded)
            return loaded
        except Exception:
            pass
    # 走到這裡 = GSheet 沒設定/失敗, 且本地檔不存在 (GitHub Actions 每次全新容器必然如此)。
    # 這代表「所有去重 / 冷卻 / 每日上限」的記憶都會歸零 → 同一則新聞每個 tick 重複推。
    # 以前這裡靜默回空, 完全看不出來 → 大聲警告。
    print(
        "[watchlist_store] ⚠️ monitor_state 載入失敗 (GSheet 未設定/失敗, 本地檔不存在) → "
        "回空 state。後果: 去重/冷卻/每日上限全部失效, 會重複推播。"
        "請確認 GCP_SERVICE_ACCOUNT_JSON / GOOGLE_SHEETS_ID 這兩個 secret 有設且有傳進 workflow env。",
        flush=True,
    )
    default = {"watchlist_alerts": {}, "index_alerts": {}, "crypto_alerts": {}}
    if _BATCH_MODE:
        _BATCH_CACHE = copy.deepcopy(default)
        _BATCH_ORIG = copy.deepcopy(default)
    return default


def save_monitor_state(state: Dict) -> None:
    """儲存 monitor state.

    I1 fix:
      - 預設模式: 立刻寫本地檔案 + GSheet (跟舊版相同)
      - batched_state_writes() context 內: 只更新 cache, context 結束時 flush.
    """
    global _BATCH_CACHE, _BATCH_DIRTY
    if _BATCH_MODE:
        # 批次模式 — 只更新 cache, 不打 GSheet
        _BATCH_CACHE = copy.deepcopy(state)
        _BATCH_DIRTY = True
        return
    # 預設 — 立刻 flush
    _flush_state(state)


# ---------------------------------------------------------------------------
# Google Sheets backend (optional)
# ---------------------------------------------------------------------------
def _get_sheet(name: str):
    """取得 worksheet by name; 不存在則建立."""
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore
    except Exception:
        return None
    sa_raw = ds._secret("GCP_SERVICE_ACCOUNT_JSON")
    sheet_id = ds._secret("GOOGLE_SHEETS_ID")
    if not (sa_raw and sheet_id):
        return None
    try:
        info = json.loads(sa_raw) if isinstance(sa_raw, str) else dict(sa_raw)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        wb = gc.open_by_key(sheet_id)
        try:
            return wb.worksheet(name)
        except Exception:
            return wb.add_worksheet(title=name, rows=200, cols=10)
    except Exception:
        return None
