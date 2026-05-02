"""
watchlist_alerts.py
自選股「累計漲跌每 X% 增量」門檻警報。

各市場 threshold:
  TW: 2.5% (台股波動較小，更敏感)
  US: 5%   (美股波動較大)

邏輯:
  每檔自選股有 base_price (入場價或第一次監控價)
  - 累計達 +2.5%, +5%, +7.5%, +10%, ... 觸發 (TW)
  - 累計達 +5%, +10%, +15%, +20%, ... 觸發 (US)
  - last_pct 紀錄上次觸發的 bucket，避免同一個 bucket 重發
  - 訊息會顯示「上次門檻 / 本次門檻 / 差異」

State: monitor_state["watchlist_alerts"]
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import data_sources as ds
import watchlist_store


# 每市場 threshold (%)
THRESHOLDS = {
    "TW": 2.5,
    "US": 5.0,
}


def _fetch_current_price(stock_id: str, market: str = "TW") -> Optional[float]:
    """用 yfinance 抓即時價."""
    if market == "US":
        df = ds.fetch_yf_history(stock_id, period="2d", interval="1d")
    else:
        for suffix in [".TW", ".TWO"]:
            df = ds.fetch_yf_history(f"{stock_id}{suffix}", period="2d", interval="1d")
            if not df.empty:
                break
        else:
            return None
    if df.empty:
        return None
    try:
        return float(df["Close"].astype(float).iloc[-1])
    except Exception:
        return None


def _get_threshold_bucket(pct: float, threshold: float) -> float:
    """把漲跌幅換算成 threshold 的整數倍.
    threshold=2.5 時:
      +3.0% → 2.5
      +6.0% → 5.0
      -3.5% → -2.5
      -7.0% → -5.0
    """
    if pct >= 0:
        steps = int(pct / threshold)
        return round(steps * threshold, 1)
    steps = int(-pct / threshold)
    return -round(steps * threshold, 1)


def check_watchlist_alerts() -> List[Dict]:
    """檢查所有自選股是否觸發新門檻. 回傳新觸發的警報 list."""
    items = watchlist_store.load_watchlist()
    if not items:
        return []

    state = watchlist_store.load_monitor_state()
    wl_state = state.setdefault("watchlist_alerts", {})

    alerts: List[Dict] = []
    for item in items:
        sid = item.get("stock_id", "")
        name = item.get("name", "")
        market = item.get("market", "TW").upper()
        entry_price = item.get("entry_price")

        threshold = THRESHOLDS.get(market, 5.0)

        current = _fetch_current_price(sid, market)
        if current is None:
            continue

        sid_state = wl_state.setdefault(sid, {})

        # 偵測 entry_price 是否變動 (使用者在 UI 改了入場價)
        # 同時把 None / 0 / float 都正規化, 避免比較失敗
        try:
            ep_now = float(entry_price) if entry_price not in (None, "", 0, 0.0) else None
        except Exception:
            ep_now = None
        ep_snapshot = sid_state.get("entry_price_snapshot")
        try:
            ep_snapshot = float(ep_snapshot) if ep_snapshot not in (None, "", 0, 0.0) else None
        except Exception:
            ep_snapshot = None

        # 第一次監控 OR entry_price 被改了 → 重設 base
        if "base_price" not in sid_state or ep_now != ep_snapshot:
            sid_state["base_price"] = float(ep_now) if ep_now else current
            sid_state["entry_price_snapshot"] = ep_now
            sid_state["base_source"] = "entry" if ep_now else "auto"
            sid_state["base_set_date"] = dt.date.today().strftime("%Y-%m-%d")
            sid_state["last_pct"] = 0.0
            sid_state["threshold"] = threshold
            continue

        base = float(sid_state["base_price"]) or 1.0
        current_pct = (current / base - 1) * 100
        bucket = _get_threshold_bucket(current_pct, threshold)
        last_bucket = float(sid_state.get("last_pct", 0))

        if bucket != last_bucket and abs(bucket) >= threshold:
            direction = "上漲" if bucket > 0 else "下跌"
            diff_bucket = round(bucket - last_bucket, 1)

            # 上次價格 (從 base + last_bucket 反推)
            prev_price = round(base * (1 + last_bucket / 100), 2)

            alerts.append({
                "stock_id": sid,
                "name": name,
                "market": market,
                "current": round(current, 2),
                "base_price": round(base, 2),
                "previous_price": prev_price,
                "current_pct": round(current_pct, 2),
                "threshold_bucket": bucket,
                "previous_bucket": last_bucket,
                "diff_bucket": diff_bucket,
                "direction": direction,
                "threshold_step": threshold,
            })
            sid_state["last_pct"] = bucket

    state["watchlist_alerts"] = wl_state
    watchlist_store.save_monitor_state(state)
    return alerts


def reset_watchlist_baseline(stock_id: str) -> bool:
    """重設某檔的 baseline (例如使用者剛賣出 / 重新進場)."""
    state = watchlist_store.load_monitor_state()
    wl_state = state.setdefault("watchlist_alerts", {})
    if stock_id in wl_state:
        del wl_state[stock_id]
        state["watchlist_alerts"] = wl_state
        watchlist_store.save_monitor_state(state)
        return True
    return False
