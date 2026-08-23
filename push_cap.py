"""
push_cap.py — 每日次要 alert 推播 cap.

主推播 (us_open / us_close / pre_market / weekend_recap / 反轉 / 系統性大跌) 不算 cap.
次要 alert (volume_breakout / chip_anomaly / strong_stock / news_event /
analyst_insider / trump / sector_strong) 共用 daily cap 6.

用 watchlist_store 的 monitor_state 存 counter, key=日期 (TPE 日).
跨日自動 reset.

API:
  check_and_consume(category: str) -> bool
    回 True = 可推 (已 increment counter); False = 已到 cap, 不該推.

  get_today_count() -> int
    回今日已推次要 alert 數.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

DEFAULT_DAILY_CAP = 6
_STATE_KEY = "secondary_push_cap"


def _today_tpe() -> str:
    """Return today's date in TPE (UTC+8) — YYYY-MM-DD."""
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def _load_state() -> dict:
    try:
        import watchlist_store
        return watchlist_store.load_monitor_state() or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        import watchlist_store
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[push_cap] save_monitor_state fail (non-fatal): {e}", flush=True)


def _get_today_entry(state: dict) -> dict:
    """Get/init today's counter entry, auto-reset if date changed."""
    today = _today_tpe()
    cap_state = state.get(_STATE_KEY) or {}
    if cap_state.get("date") != today:
        cap_state = {"date": today, "count": 0, "categories": []}
    return cap_state


def get_today_count() -> int:
    """Get today's secondary push count (TPE)."""
    state = _load_state()
    entry = _get_today_entry(state)
    return int(entry.get("count", 0))


def would_allow(category: str, cap: int = DEFAULT_DAILY_CAP) -> bool:
    """唯讀檢查: 今日次要 alert 數是否還沒到 cap. 不 increment counter.

    Caller 應該: 先 would_allow() 檢查 → 真的送出成功後才呼叫 mark_consumed()
    去 +1, 避免送失敗 (網路抖動 / TG API 503) 也白白佔掉當日額度。
    """
    state = _load_state()
    entry = _get_today_entry(state)
    if int(entry.get("count", 0)) >= cap:
        print(f"[push_cap] daily cap {cap} reached, skip {category}", flush=True)
        return False
    return True


def mark_consumed(category: str) -> None:
    """實際送出成功後才呼叫, 真正把今日 counter +1.

    Bug fix: 舊版 check_and_consume() 在送出前就先 +1, 若 caller 3 次重試全部
    失敗, 當日 cap 額度已經被「送失敗的訊息」燒光, 導致後面真正成功的次要
    alert 被 cap 擋下但使用者其實一封都沒收到。現在改成 would_allow() 只讀
    不寫, 送出確定成功後才呼叫這個函式扣額。
    """
    state = _load_state()
    entry = _get_today_entry(state)
    entry["count"] = int(entry.get("count", 0)) + 1
    cats = entry.get("categories") or []
    cats.append(category)
    entry["categories"] = cats[-20:]  # keep last 20 for debug
    state[_STATE_KEY] = entry
    _save_state(state)


def check_and_consume(category: str, cap: int = DEFAULT_DAILY_CAP) -> bool:
    """[Deprecated] 舊 API: 檢查時就立刻 +1 counter, 保留給尚未遷移的舊 caller。

    新 caller 請改用 would_allow() + mark_consumed() (送出成功後才扣額),
    避免送失敗仍佔用當日額度。目前專案內的呼叫點已全部遷移, 這個函式只
    是相容性保留, 行為維持原樣 (先 +1 再回傳)。
    """
    if not would_allow(category, cap):
        return False
    mark_consumed(category)
    return True


def peek_remaining(cap: int = DEFAULT_DAILY_CAP) -> int:
    """How many secondary pushes still allowed today."""
    return max(0, cap - get_today_count())
