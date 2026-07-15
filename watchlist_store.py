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
    global _BATCH_MODE, _BATCH_CACHE, _BATCH_DIRTY
    if _BATCH_MODE:
        # 巢狀 open — 已在 batch 內, no-op
        return
    _BATCH_MODE = True
    _BATCH_CACHE = None
    _BATCH_DIRTY = False


def close_batched_state() -> None:
    """關閉 batched mode + flush 已修改的 state. Idempotent (重複呼叫 OK)."""
    global _BATCH_MODE, _BATCH_CACHE, _BATCH_DIRTY
    if not _BATCH_MODE:
        return  # 不在 batch 內 (重複呼叫 / 沒開過)
    _BATCH_MODE = False
    if _BATCH_DIRTY and _BATCH_CACHE is not None:
        try:
            _flush_state(_BATCH_CACHE)
        except Exception as _e:
            print(f"[watchlist_store] batched flush failed: {_e}", flush=True)
    _BATCH_CACHE = None
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
            state = merged
    except Exception as _me:
        print(f"[watchlist_store] monitor_state merge skip (non-fatal): {_me}", flush=True)
    blob = json.dumps(state, ensure_ascii=False, indent=2)
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
            sheet.clear()
            cols = ["stock_id", "name", "market", "entry_price", "added_date"]
            sheet.append_row(cols)
            for d in items:
                sheet.append_row([str(d.get(c, "")) for c in cols])
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
    global _BATCH_CACHE
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
                return loaded
        except Exception:
            pass
    if MONITOR_STATE_FILE.exists():
        try:
            loaded = json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
            if _BATCH_MODE:
                _BATCH_CACHE = copy.deepcopy(loaded)
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
