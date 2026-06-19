"""
holdings_intraday_alert.py
持倉 intraday 風險警報 — 盤中即時偵測持倉股大跌或急轉, 立即推 TG.

跟 holdings_tracker (停損價檢查) 互補:
  - holdings_tracker: 跌破用戶設的「停損價」才推 (絕對價位)
  - 本模組: 看「相對日內動能」(今日跌幅 / 從早高回吐), 即使沒設停損也能警示

觸發條件 (任一即推):
  1. 今日跌幅 ≤ -3% (大跌, 必警示)
  2. 從今日高點回吐 ≥ 5% (急轉直下)
  3. 跌破用戶設的 stop_price (跟 holdings_tracker 共用)

Throttle:
  - per-stock per-day cap: 1 則 (避免同檔反覆推)
  - 全域 cooldown: 30 min between batches

State: monitor_state["holdings_intraday_alert"] = {
   date, stocks_alerted: [stock_id, ...], last_batch_at
}

API:
  - check_holdings_intraday_risk() -> List[Dict]
  - mark_alerts_sent(alerts) -> None  (caller 在 send 成功後呼叫)
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import data_sources as ds
import watchlist_store
import holdings_store


HI_TODAY_DROP_PCT = -3.0       # 今日跌幅警戒
HI_DRAWDOWN_FROM_HIGH = -5.0   # 從早高點回吐警戒
HI_COOLDOWN_MIN = 30           # 兩批推播間最少間隔


def _is_tw_session() -> bool:
    """台股交易時段 09:00-13:30 TPE = 01:00-05:30 UTC, 平日."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    if now_utc.weekday() >= 5:
        return False
    cur = now_utc.hour + now_utc.minute / 60.0
    return 1.0 <= cur < 5.5


def _is_us_session() -> bool:
    """美股 RTH 交易時段 — 用 US/Eastern 直接判, 自動處理 DST."""
    # Bug fix: 原本用 13.5-21.0 UTC 聯集, EST 季的 13:30-14:30 UTC 其實是盤前,
    #          會用盤前/舊資料評估持倉. 改用 US/Eastern 9:30-16:00 RTH, DST 自動正確.
    try:
        import pytz
        et = dt.datetime.now(pytz.timezone("US/Eastern"))
        if et.weekday() >= 5:
            return False
        mins = et.hour * 60 + et.minute
        return 9 * 60 + 30 <= mins < 16 * 60
    except Exception:
        # pytz 不可用 → 退回舊聯集 (寬鬆但不崩)
        now_utc = dt.datetime.now(dt.timezone.utc)
        if now_utc.weekday() >= 5:
            return False
        cur = now_utc.hour + now_utc.minute / 60.0
        return 13.5 <= cur < 21.0


def _tw_suffix(stock_id: str) -> Optional[str]:
    """簡化版台股後綴判斷 — 試 .TW 再 .TWO."""
    for sfx in [".TW", ".TWO"]:
        df = ds.fetch_yf_history(f"{stock_id}{sfx}", period="2d", interval="5m")
        if df is not None and not df.empty and len(df) >= 3:
            return sfx
    return None


def _eval_holding_intraday(h: Dict) -> Optional[Dict]:
    """評估單檔持倉 intraday 風險. 沒觸發回 None."""
    sid = str(h.get("stock_id", "")).strip()
    if not sid:
        return None
    market = h.get("market", "TW")  # holdings_store 預設 TW
    stop_price = h.get("stop_price")  # 用戶設的停損價 (可能無)

    # 取 ticker
    if market == "TW":
        sfx = _tw_suffix(sid)
        if not sfx:
            return None
        ticker = f"{sid}{sfx}"
    else:
        ticker = sid

    # 抓今日 5m bars
    try:
        df = ds.fetch_yf_history(ticker, period="2d", interval="5m")
        if df is None or df.empty:
            return None
        import pandas as pd
        date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
        df = df.copy()
        df["_dt"] = pd.to_datetime(df[date_col], utc=True)
        df["_d"] = df["_dt"].dt.date
        today = df["_d"].max()
        today_bars = df[df["_d"] == today].sort_values("_dt")
        if today_bars.empty:
            return None
        # 昨收 (前一日最後一根 bar 或前一日 daily close)
        prev_bars = df[df["_d"] < today]
        if prev_bars.empty:
            return None
        prev_close = float(prev_bars["Close"].iloc[-1])
        today_open = float(today_bars["Open"].iloc[0])
        today_high = float(today_bars["High"].astype(float).max())
        current = float(today_bars["Close"].iloc[-1])
        if prev_close <= 0 or today_open <= 0 or today_high <= 0:
            return None

        today_pct = (current / prev_close - 1) * 100
        drawdown_from_high = (current / today_high - 1) * 100 if today_high > 0 else 0

        # 觸發條件
        triggers = []
        if today_pct <= HI_TODAY_DROP_PCT:
            triggers.append(f"今日大跌 {today_pct:+.2f}%")
        if drawdown_from_high <= HI_DRAWDOWN_FROM_HIGH:
            triggers.append(f"從高點回吐 {drawdown_from_high:.2f}%")
        if stop_price and current <= float(stop_price):
            triggers.append(f"跌破停損 {stop_price}")

        if not triggers:
            return None

        return {
            "stock_id": sid,
            "name": h.get("name") or h.get("stock_name", ""),
            "market": market,
            "current": round(current, 2),
            "today_pct": round(today_pct, 2),
            "today_high": round(today_high, 2),
            "drawdown_from_high_pct": round(drawdown_from_high, 2),
            "stop_price": stop_price,
            "triggers": triggers,
            "severity": "severe" if (today_pct <= -5 or drawdown_from_high <= -7) else "medium",
        }
    except Exception as e:
        print(f"[holdings_intraday] {sid} 評估失敗: {e}", flush=True)
        return None


def check_holdings_intraday_risk() -> List[Dict]:
    """偵測持倉股 intraday 風險. 回 list (空 = 沒觸發).

    HIGH-1 fix pattern: 不在這裡 save state. 由 caller 在 send 成功後
    呼叫 mark_alerts_sent() 才寫, 避免 send 失敗時整天靜默.
    """
    # 必須在某個交易時段內
    if not _is_tw_session() and not _is_us_session():
        return []

    # 假日 skip (TW)
    try:
        import holiday_check
        # 只看 TW 假日 — US 假日先不管 (持倉以 TW 為主)
        if _is_tw_session() and holiday_check.is_market_closed_today("TW"):
            return []
    except Exception:
        pass

    try:
        holdings = holdings_store.load_holdings() or []
    except Exception as e:
        print(f"[holdings_intraday] load_holdings 失敗 (check GSheet creds): {e}", flush=True)
        holdings = []
    # HIGH-C2 fix: 顯式 log 持倉數量, 方便 GH Actions log 排錯
    # 若 GSheet creds 沒掛, GH Actions 讀 git 倉庫的 holdings.json (常常是空)
    print(f"[holdings_intraday] 載入 {len(holdings)} 檔持倉", flush=True)
    if not holdings:
        print("[holdings_intraday] 無持倉 — 跳過. 若 streamlit cloud 有設持倉但 cron 看不到,"
              " 檢查 GitHub Secrets 是否設 GOOGLE_SHEETS_CREDS", flush=True)
        return []

    state = watchlist_store.load_monitor_state()
    hi_state = state.setdefault("holdings_intraday_alert", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    now_utc = dt.datetime.now(dt.timezone.utc)

    # 跨日 reset
    if hi_state.get("date") != today_str:
        hi_state.clear()
        hi_state.update({
            "date": today_str,
            "stocks_alerted": [],
            "last_batch_at": None,
        })

    # 全域 cooldown
    last_batch = hi_state.get("last_batch_at")
    if last_batch:
        try:
            ts = dt.datetime.fromisoformat(last_batch)
            if (now_utc - ts).total_seconds() < HI_COOLDOWN_MIN * 60:
                return []
        except Exception:
            pass

    alerts = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_eval_holding_intraday, h): h for h in holdings}
        for f in as_completed(futs):
            r = f.result()
            if r:
                alerts.append(r)

    if not alerts:
        return []

    # 過濾已推過的 (per-stock daily cap 1)
    stocks_alerted = set(hi_state.get("stocks_alerted") or [])
    fresh = [a for a in alerts if a["stock_id"] not in stocks_alerted]

    # 給每檔 alert 加 entry_label (推播裡顯示「該檔現在減碼/出場/加碼」)
    if fresh:
        try:
            import entry_label_helper as _el
            pairs = [(a["stock_id"], a.get("market", "TW")) for a in fresh]
            eval_map = _el.batch_evaluate(pairs, max_workers=8)
            for a in fresh:
                ev = eval_map.get(a["stock_id"]) or {}
                a["entry_label"] = ev.get("entry_label", "—")
                a["entry_emoji"] = ev.get("entry_emoji", "")
                a["entry_score"] = ev.get("entry_score")
                a["entry_action"] = ev.get("entry_action", "—")
        except Exception as _e:
            print(f"[holdings_intraday] entry_label 失敗 (non-fatal): {_e}", flush=True)

    return fresh


def mark_alerts_sent(alerts: List[Dict]) -> None:
    """caller 在 send 成功後呼叫, 把已成功推送的持倉登記進 state."""
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        hi_state = state.setdefault("holdings_intraday_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        if hi_state.get("date") != today_str:
            hi_state.clear()
            hi_state.update({"date": today_str, "stocks_alerted": [], "last_batch_at": None})
        stocks_alerted = set(hi_state.get("stocks_alerted") or [])
        for a in alerts:
            stocks_alerted.add(a["stock_id"])
        hi_state["stocks_alerted"] = sorted(stocks_alerted)
        hi_state["last_batch_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        state["holdings_intraday_alert"] = hi_state
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[holdings_intraday] mark_alerts_sent failed: {e}", flush=True)
