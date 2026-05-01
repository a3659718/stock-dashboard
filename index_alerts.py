"""
index_alerts.py
大盤點數增量警報 + 加密貨幣劇烈波動警報。

【大盤】當日漲跌每超過閾值就跳通知:
  日經 225 (^N225)   每 ±150 點
  韓國 KOSPI (^KS11) 每 ±50 點
  台股加權 (^TWII)   每 ±100 點

【加密貨幣】當日 ±2.5% 就跳通知:
  BTC-USD
  ETH-USD

連續同方向跳 N 次 → 視為強勢趨勢，加上「持續 X 連跌/連漲」警示文字。

State: monitor_state["index_alerts"], monitor_state["crypto_alerts"]
       每天的開盤價當 base, 收盤後重置.
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import data_sources as ds
import watchlist_store


# 配置
INDEX_CONFIG = {
    "^N225":  {"name": "日經 225",   "threshold": 150.0, "country": "JP"},
    "^KS11":  {"name": "韓國 KOSPI", "threshold": 50.0,  "country": "KR"},
    "^TWII":  {"name": "台灣加權",   "threshold": 100.0, "country": "TW"},
}

CRYPTO_CONFIG = {
    "BTC-USD": {"name": "BTC",  "threshold_pct": 2.5},
    "ETH-USD": {"name": "ETH",  "threshold_pct": 2.5},
}


# ---------------------------------------------------------------------------
# 大盤點數警報
# ---------------------------------------------------------------------------
def _fetch_index_today_open_and_current(symbol: str) -> Optional[Dict]:
    """抓今天開盤價跟即時價 (用 5m 線推今日開盤)."""
    df = ds.fetch_yf_history(symbol, period="2d", interval="5m")
    if df.empty:
        # fallback to daily
        df_d = ds.fetch_yf_history(symbol, period="2d", interval="1d")
        if df_d.empty or len(df_d) < 1:
            return None
        try:
            today_open = float(df_d["Open"].iloc[-1])
            today_close = float(df_d["Close"].iloc[-1])
            return {"open": today_open, "current": today_close}
        except Exception:
            return None

    import pandas as pd
    df = df.copy()
    date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
    df["_dt"] = pd.to_datetime(df[date_col])
    df["_d"] = df["_dt"].dt.date
    today = df["_d"].max()
    today_bars = df[df["_d"] == today].sort_values("_dt")
    if today_bars.empty:
        return None
    try:
        today_open = float(today_bars.iloc[0]["Open"])
        current = float(today_bars.iloc[-1]["Close"])
        return {"open": today_open, "current": current}
    except Exception:
        return None


def check_index_alerts() -> List[Dict]:
    """檢查大盤點數是否觸發新門檻."""
    state = watchlist_store.load_monitor_state()
    idx_state = state.setdefault("index_alerts", {})

    today_str = dt.date.today().strftime("%Y-%m-%d")
    alerts: List[Dict] = []

    for sym, cfg in INDEX_CONFIG.items():
        info = _fetch_index_today_open_and_current(sym)
        if not info:
            continue
        today_open = info["open"]
        current = info["current"]
        diff = current - today_open  # 點數變化
        threshold = cfg["threshold"]

        # 取 bucket: -300, -150, 0, 150, 300, ...
        if diff >= 0:
            bucket = int(diff // threshold) * threshold
        else:
            bucket = -(int(-diff // threshold) * threshold)

        # 跨日重置
        sym_state = idx_state.setdefault(sym, {})
        if sym_state.get("date") != today_str:
            sym_state.clear()
            sym_state["date"] = today_str
            sym_state["last_bucket"] = 0
            sym_state["consecutive_count"] = 0
            sym_state["last_direction"] = "none"

        last_bucket = sym_state.get("last_bucket", 0)
        if bucket != last_bucket and abs(bucket) >= threshold:
            # 觸發新門檻
            direction = "漲" if diff > 0 else "跌"
            # 統計連續同方向
            last_direction = sym_state.get("last_direction", "none")
            if direction == last_direction:
                sym_state["consecutive_count"] = sym_state.get("consecutive_count", 0) + 1
            else:
                sym_state["consecutive_count"] = 1
                sym_state["last_direction"] = direction

            consecutive = sym_state["consecutive_count"]
            alerts.append({
                "symbol": sym,
                "name": cfg["name"],
                "country": cfg["country"],
                "today_open": round(today_open, 2),
                "current": round(current, 2),
                "diff": round(diff, 2),
                "direction": direction,
                "threshold_bucket": bucket,
                "consecutive": consecutive,
                "warning": consecutive >= 2,  # 連續 2 次以上加警示
            })
            sym_state["last_bucket"] = bucket

    state["index_alerts"] = idx_state
    watchlist_store.save_monitor_state(state)
    return alerts


# ---------------------------------------------------------------------------
# 加密貨幣警報
# ---------------------------------------------------------------------------
def check_crypto_alerts() -> List[Dict]:
    """檢查 BTC / ETH 是否觸發 ±2.5% 門檻."""
    state = watchlist_store.load_monitor_state()
    crypto_state = state.setdefault("crypto_alerts", {})

    today_str = dt.date.today().strftime("%Y-%m-%d")
    alerts: List[Dict] = []

    for sym, cfg in CRYPTO_CONFIG.items():
        df = ds.fetch_yf_history(sym, period="2d", interval="1h")
        if df.empty or len(df) < 2:
            continue
        try:
            close = df["Close"].astype(float)
            last = float(close.iloc[-1])

            # base = 24 小時前的價格
            if len(close) >= 24:
                base = float(close.iloc[-25])
            else:
                base = float(close.iloc[0])

            change_pct = (last / base - 1) * 100 if base > 0 else 0
            threshold = cfg["threshold_pct"]

            # bucket: ±2.5, ±5, ±7.5, ±10
            if change_pct >= 0:
                bucket = int(change_pct / threshold) * threshold
            else:
                bucket = -(int(-change_pct / threshold) * threshold)

            sym_state = crypto_state.setdefault(sym, {})
            if sym_state.get("date") != today_str:
                sym_state.clear()
                sym_state["date"] = today_str
                sym_state["last_bucket"] = 0
                sym_state["base_price"] = base

            last_bucket = sym_state.get("last_bucket", 0)
            if bucket != last_bucket and abs(bucket) >= threshold:
                direction = "上漲" if change_pct > 0 else "下跌"
                alerts.append({
                    "symbol": sym,
                    "name": cfg["name"],
                    "current": round(last, 2),
                    "base_price": round(base, 2),
                    "change_pct": round(change_pct, 2),
                    "threshold_bucket": bucket,
                    "direction": direction,
                })
                sym_state["last_bucket"] = bucket
        except Exception:
            continue

    state["crypto_alerts"] = crypto_state
    watchlist_store.save_monitor_state(state)
    return alerts
