"""
push_dedup.py — 排程推播「同一 slot 短時間內只送一次」去重守衛.

問題
----
GitHub Actions cron 常 drift 5~20 分鐘, market_open_alert.yml 的分鐘分流在邊界
可能把「漂移的 monitor cron」誤判成 tw_open / us_close 等主推, 或讓兩個相鄰 cron
都落進同一個 slot → 同一則內容一天被推兩次 (duplicate)。

解法
----
每個「一天只該推一次」的 slot (tw_open / us_close / morning_recap …) 在送出前先
claim 一次; 若同一 slot 在 window_min 分鐘內已 claim 過 → 這次跳過。
- 用「時間窗」而非「整天鎖」: drift 重複都發生在數十分鐘內, 窗夠大就擋掉;
  又不會把整天鎖死, 隔天 / 真正需要的補推不受影響。
- monitor 這種「本來就一天多跑」的 slot 不該 dedup → 由 caller 自行排除。

設計原則: **fail-open**。dedup 自身任何例外都當作「可以送」, 寧可偶爾重複,
          也絕不因為去重邏輯壞掉而漏推。

狀態存在 watchlist_store 的 monitor_state["slot_dedup"] = {slot: last_claim_iso_utc}.

API
---
  claim_slot(slot, window_min=50) -> bool
      True  = 這次可送 (已記錄 claim 時間)
      False = window 內已送過, 應跳過
  was_claimed_recently(slot, window_min=50) -> bool   # 只看不寫
  reset(slot=None)                                     # 清掉 (測試 / 手動補推用)
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

_STATE_KEY = "slot_dedup"
DEFAULT_WINDOW_MIN = 50
_PRUNE_AFTER_MIN = 24 * 60  # 超過 1 天的舊紀錄清掉, 不讓 state 無限長


def _now() -> _dt.datetime:
    return _dt.datetime.utcnow()


def _parse(ts: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", ""))
    except Exception:
        return None


def _load() -> dict:
    try:
        import watchlist_store
        return watchlist_store.load_monitor_state() or {}
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        import watchlist_store
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[push_dedup] save fail (non-fatal): {e}", flush=True)


def _prune(table: dict) -> dict:
    """丟掉超過 _PRUNE_AFTER_MIN 的舊 claim, 控制 state 大小."""
    now = _now()
    out = {}
    for slot, ts in (table or {}).items():
        d = _parse(str(ts))
        if d is None:
            continue
        if (now - d).total_seconds() <= _PRUNE_AFTER_MIN * 60:
            out[slot] = ts
    return out


def was_claimed_recently(slot: str, window_min: int = DEFAULT_WINDOW_MIN) -> bool:
    """只讀: 此 slot 是否在 window_min 內已 claim 過."""
    try:
        table = (_load().get(_STATE_KEY) or {})
        ts = table.get(slot)
        if not ts:
            return False
        d = _parse(str(ts))
        if d is None:
            return False
        return (_now() - d).total_seconds() < window_min * 60
    except Exception:
        return False  # fail-open: 不確定就當沒送過


def claim_slot(slot: str, window_min: int = DEFAULT_WINDOW_MIN) -> bool:
    """嘗試 claim 一個 slot.

    回 True  = 可送 (並把 claim 時間記成現在)
       False = window_min 內已有 claim, 應跳過

    fail-open: 任何例外都回 True (寧可重複也不漏推)。
    """
    if not slot:
        return True
    try:
        state = _load()
        table = _prune(state.get(_STATE_KEY) or {})
        ts = table.get(slot)
        if ts:
            d = _parse(str(ts))
            if d is not None and (_now() - d).total_seconds() < window_min * 60:
                age = int((_now() - d).total_seconds() // 60)
                print(f"[push_dedup] slot '{slot}' {age} 分鐘前已送過 (<{window_min}min), 跳過重複推播", flush=True)
                return False
        # 可送 — 記錄 claim 時間
        table[slot] = _now().isoformat(timespec="seconds") + "Z"
        state[_STATE_KEY] = table
        _save(state)
        return True
    except Exception as e:
        print(f"[push_dedup] claim_slot 例外, fail-open 照送: {e}", flush=True)
        return True


def reset(slot: Optional[str] = None) -> None:
    """清掉某 slot (或全部) 的 claim — 測試 / 強制補推用."""
    try:
        state = _load()
        table = state.get(_STATE_KEY) or {}
        if slot is None:
            table = {}
        else:
            table.pop(slot, None)
        state[_STATE_KEY] = table
        _save(state)
    except Exception as e:
        print(f"[push_dedup] reset fail: {e}", flush=True)


# 一天會跑很多次、本來就不該 dedup 的 slot (caller 可參考)
MULTI_RUN_SLOTS = frozenset({"monitor"})


def should_guard(slot: str) -> bool:
    """這個 slot 該不該套 dedup 守衛 (monitor 等多跑型別不套)."""
    return bool(slot) and slot not in MULTI_RUN_SLOTS


if __name__ == "__main__":
    # 簡單自測 (不碰 watchlist_store 真實 state 時, fail-open 行為仍可驗證)
    import sys
    print("should_guard('tw_open') =", should_guard("tw_open"))
    print("should_guard('monitor') =", should_guard("monitor"))
    a = claim_slot("__selftest__", window_min=50)
    b = claim_slot("__selftest__", window_min=50)
    reset("__selftest__")
    print(f"claim#1={a} claim#2={b} (期望 True 後 False, 除非 state 不可寫則皆 True/fail-open)")
    sys.exit(0)
