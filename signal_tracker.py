"""
signal_tracker.py
通用訊號 → 結果追蹤. 30 天滾動準確率.

設計原則:
  - 對所有訊號類型 (catalyst / strong_sector_leader / next_day_breakout 等) 用同一個 schema
  - 「預測 → 實際」用 stock_id 對應, N 天後驗證 (預測時設好 evaluate_after_days)
  - 紀錄存在 watchlist_store.monitor_state["signals"], 跨 cron persist

Schema (一筆記錄):
{
    "id": "uuid",
    "signal_type": "catalyst" | "strong_sector_leader" | "next_day_breakout" | ...,
    "stock_id": "2330",
    "name": "台積電",
    "predicted_at": "2026-05-09",      # 預測日期
    "predicted_price": 1050,           # 預測時的收盤價
    "expected_direction": "up" | "down",
    "evaluate_after_days": 1 | 3 | 5,  # 幾天後驗證
    "evaluate_at": "2026-05-12",
    "actual_price": 1080,              # 驗證日收盤
    "actual_pct": 2.86,
    "hit": true | false | null,        # null = 還沒驗證
    "extras": {...}                    # 額外 metadata (e.g. 機率 / 信心 / R:R)
}

用法:
  signal_tracker.record_signal(signal_type, stock_id, ...)
  signal_tracker.evaluate_pending()  # 在每天盤後呼叫一次, 把到期的標記 hit
  signal_tracker.accuracy_summary(signal_type, lookback_days=30)
  signal_tracker.fmt_accuracy_block()  # 給推播末段用
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Dict, List, Optional

import data_sources as ds


_STATE_KEY = "signals"
_MAX_RECORDS = 2000  # 防爆: 留最近 2000 筆


def _load_state() -> Dict:
    try:
        import watchlist_store
        return watchlist_store.load_monitor_state()
    except Exception:
        return {}


def _save_state(state: Dict) -> None:
    try:
        import watchlist_store
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[signal_tracker] save failed: {e}", flush=True)


def _today_str() -> str:
    return dt.date.today().strftime("%Y-%m-%d")


def _add_days(d: str, n: int) -> str:
    base = dt.datetime.strptime(d, "%Y-%m-%d").date()
    return (base + dt.timedelta(days=n)).strftime("%Y-%m-%d")


def record_signal(signal_type: str, stock_id: str, name: str = "",
                   predicted_price: Optional[float] = None,
                   expected_direction: str = "up",
                   evaluate_after_days: int = 3,
                   extras: Optional[Dict] = None) -> Optional[str]:
    """記一筆預測, 回 record id (失敗回 None)."""
    if not stock_id or not signal_type:
        return None
    state = _load_state()
    records: List[Dict] = state.setdefault(_STATE_KEY, [])

    # Dedup: 同一天 同一 stock_id + signal_type 只記一筆
    today = _today_str()
    for r in records:
        if (r.get("predicted_at") == today
                and r.get("signal_type") == signal_type
                and str(r.get("stock_id", "")) == str(stock_id)):
            return r.get("id")  # 已存在 → 回原 id, 不重複

    rec_id = str(uuid.uuid4())[:8]
    rec = {
        "id": rec_id,
        "signal_type": signal_type,
        "stock_id": str(stock_id),
        "name": str(name or ""),
        "predicted_at": today,
        "predicted_price": float(predicted_price) if predicted_price is not None else None,
        "expected_direction": expected_direction,
        "evaluate_after_days": int(evaluate_after_days),
        "evaluate_at": _add_days(today, int(evaluate_after_days)),
        "actual_price": None,
        "actual_pct": None,
        "hit": None,
        "extras": dict(extras or {}),
    }
    records.append(rec)
    # Cap — 但要保留所有「還沒驗證 (hit=None)」的 pending, 不能被砍掉
    # 否則 evaluate_after_days=5 的訊號可能被開頭的舊紀錄擠掉
    if len(records) > _MAX_RECORDS:
        pending = [r for r in records if r.get("hit") is None]
        validated = [r for r in records if r.get("hit") is not None]
        # 留所有 pending + 最近的 validated, 直到總數 <= _MAX_RECORDS
        keep_validated = max(0, _MAX_RECORDS - len(pending))
        records[:] = pending + validated[-keep_validated:]
    state[_STATE_KEY] = records
    _save_state(state)
    return rec_id


def _is_market_closed_now(market: str = "TW") -> bool:
    """判斷該市場是否已收盤 (用來決定要不要評估 today 的訊號).

    TW: UTC ≥ 06:00 (= 14:00 TPE 之後, 13:30 收盤後加 30 min buffer)
    US: UTC ≥ 21:00 EDT 期 / ≥ 22:00 EST (16:00 ET 收盤後加 1 hr buffer)
    """
    now_utc = dt.datetime.utcnow()
    if market == "US":
        # 用 index_alerts 的 DST 偵測
        try:
            import index_alerts
            dst = index_alerts._is_us_in_dst(now_utc.date())
        except Exception:
            dst = True  # 假設 DST (容錯)
        return now_utc.hour >= (21 if dst else 22)
    return now_utc.hour >= 6  # TW


def _fetch_eod_close_price(stock_id: str, market: str = "TW",
                             trade_date: Optional[str] = None) -> Optional[float]:
    """抓「指定日期或最近一個完整交易日」的收盤價.

    重要: 不能直接 iloc[-1] — 盤中跑會拿到 partial-bar (今日 high/low/close 還沒定案).
    解法:
      1. 如果該市場 not closed yet → 不評估 (回 None)
      2. 已收盤但 yfinance 最後一根還是「今日 partial」(罕見, 邊界 case) → 用 iloc[-2]
    """
    if not _is_market_closed_now(market):
        return None
    try:
        if market == "US":
            df = ds.fetch_yf_history(stock_id, period="5d", interval="1d")
            if df is None or df.empty:
                return None
            return float(df["Close"].astype(float).iloc[-1])
        for suffix in [".TW", ".TWO"]:
            df = ds.fetch_yf_history(f"{stock_id}{suffix}", period="5d", interval="1d")
            if df is None or df.empty:
                continue
            return float(df["Close"].astype(float).iloc[-1])
        return None
    except Exception:
        return None


# 向後相容 (萬一其他地方有 import)
_fetch_close_price = _fetch_eod_close_price


def evaluate_pending() -> int:
    """掃所有 evaluate_at <= 今日 的待驗證紀錄, 抓現價算 hit. 回驗證了幾筆."""
    state = _load_state()
    records: List[Dict] = state.get(_STATE_KEY, [])
    if not records:
        return 0
    today = _today_str()
    n = 0
    for r in records:
        if r.get("hit") is not None:
            continue  # 已驗過
        if r.get("evaluate_at", "9999-99-99") > today:
            continue  # 還沒到期
        sid = r.get("stock_id", "")
        market = "US" if r.get("extras", {}).get("market") == "US" else "TW"
        # 收盤後才評估; 否則 skip 等下次 cron
        price = _fetch_eod_close_price(sid, market)
        if price is None:
            continue
        pred = r.get("predicted_price")
        if not pred:
            continue
        actual_pct = (price / pred - 1) * 100
        direction = r.get("expected_direction", "up")
        if direction == "up":
            hit = actual_pct > 0.5  # 漲 > 0.5% 算命中
        else:  # down
            hit = actual_pct < -0.5
        r["actual_price"] = round(float(price), 2)
        r["actual_pct"] = round(float(actual_pct), 2)
        r["hit"] = bool(hit)
        n += 1
    if n:
        state[_STATE_KEY] = records
        _save_state(state)
    return n


def accuracy_summary(signal_type: Optional[str] = None,
                      lookback_days: int = 30) -> Dict:
    """指定 signal_type 的滾動準確率 (None = 全部).

    回 {
        "signal_type": "...",
        "n": 25, "hit": 17, "pct": 68.0, "lookback_days": 30,
    }
    """
    state = _load_state()
    records: List[Dict] = state.get(_STATE_KEY, [])
    cutoff = dt.date.today() - dt.timedelta(days=lookback_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    total = 0
    hit = 0
    for r in records:
        if r.get("hit") is None:
            continue
        if r.get("predicted_at", "0") < cutoff_str:
            continue
        if signal_type and r.get("signal_type") != signal_type:
            continue
        total += 1
        if r.get("hit"):
            hit += 1
    pct = round(hit / total * 100, 1) if total else None
    return {
        "signal_type": signal_type or "ALL",
        "n": total, "hit": hit, "pct": pct,
        "lookback_days": lookback_days,
    }


def fmt_accuracy_block(signal_types: Optional[List[str]] = None,
                        lookback_days: int = 30) -> str:
    """格式化「本月各訊號準確率」一段給推播末用."""
    types_to_show = signal_types or [
        "catalyst", "strong_sector_leader", "next_day_breakout",
        "avoid_pick", "potential_pick",
    ]
    lines = []
    for st in types_to_show:
        s = accuracy_summary(st, lookback_days=lookback_days)
        if s["n"] >= 5:
            label_map = {
                "catalyst": "催化劑利多→3日漲",
                "strong_sector_leader": "強勢族群龍頭→隔日漲",
                "next_day_breakout": "隔日上漲 Top 3→隔日漲",
                "avoid_pick": "避開訊號→3日跌",
                "potential_pick": "潛力股→5日漲",
            }
            label = label_map.get(st, st)
            mark = "🟢" if s["pct"] and s["pct"] >= 60 else ("🟡" if s["pct"] and s["pct"] >= 40 else "🔴")
            lines.append(f"  {mark} {label}: {s['hit']}/{s['n']} ({s['pct']}%)")
    if not lines:
        return ""
    return "\n".join([f"<b>近 {lookback_days} 天訊號表現</b>"] + lines)


def reset_all() -> int:
    state = _load_state()
    n = len(state.get(_STATE_KEY, []) or [])
    state[_STATE_KEY] = []
    _save_state(state)
    return n
