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


def check_and_consume(category: str, cap: int = DEFAULT_DAILY_CAP) -> bool:
    """Check daily cap and atomically increment counter if OK.

    Returns:
      True  - 還在 cap 內, counter 已 +1, caller 可推.
      False - 已到 cap, 不該推.

    Note: 這個 function 是 "best-effort" 原子 — 如果同一 cron 內多次 call,
          每次都會 +1 (即使 caller 後面 send 失敗). 為避免 send 失敗仍佔額,
          建議 caller 先 send 再 mark (見 mark_consumed below).
    """
    state = _load_state()
    entry = _get_today_entry(state)
    if int(entry.get("count", 0)) >= cap:
        print(f"[push_cap] daily cap {cap} reached, skip {category}", flush=True)
        return False
    entry["count"] = int(entry.get("count", 0)) + 1
    cats = entry.get("categories") or []
    cats.append(category)
    entry["categories"] = cats[-20:]  # keep last 20 for debug
    state[_STATE_KEY] = entry
    _save_state(state)
    return True


def peek_remaining(cap: int = DEFAULT_DAILY_CAP) -> int:
    """How many secondary pushes still allowed today."""
    return max(0, cap - get_today_count())
