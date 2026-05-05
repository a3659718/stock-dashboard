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


# 自選股盤中警報 — 預設 ±5% / ±10% 雙門檻
# 比對: 今日開盤 OR 昨收 (取較極端的當主)
# 每個 bucket (方向 × 門檻) 一天最多觸發 1 次
DEFAULT_EXTREME_THRESHOLDS = [5.0, 10.0]
EXTREME_THRESHOLDS = list(DEFAULT_EXTREME_THRESHOLDS)  # 向後相容

# 為了向後相容, 保留 THRESHOLDS 變數 (其他模組可能引用)
THRESHOLDS = {"TW": 5.0, "US": 5.0}


def get_thresholds_for(market: str = "TW") -> List[float]:
    """讀取使用者自訂門檻, 沒設定就用 default."""
    try:
        state = watchlist_store.load_monitor_state()
        cfg = (state.get("watchlist_config") or {})
        key = f"extreme_thresholds_{market.lower()}"
        thrs = cfg.get(key)
        if isinstance(thrs, list) and thrs:
            cleaned = sorted({float(x) for x in thrs if x and float(x) > 0})
            if cleaned:
                return cleaned
    except Exception:
        pass
    return list(DEFAULT_EXTREME_THRESHOLDS)


def save_thresholds_for(market: str, thresholds: List[float]) -> bool:
    """儲存使用者自訂門檻 (持久化到 Google Sheets / JSON)."""
    try:
        state = watchlist_store.load_monitor_state()
        cfg = state.setdefault("watchlist_config", {})
        cfg[f"extreme_thresholds_{market.lower()}"] = sorted(
            {float(x) for x in thresholds if x and float(x) > 0}
        )
        state["watchlist_config"] = cfg
        watchlist_store.save_monitor_state(state)
        return True
    except Exception:
        return False


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


def _fetch_today_status(stock_id: str, market: str = "TW") -> Optional[Dict]:
    """抓今日 open + 即時 close + 昨收. 用 5m 線取盤中, daily 取昨收.
    回傳 {today_open, current, prev_close} 或 None.
    """
    import pandas as pd

    def _try(symbol: str) -> Optional[Dict]:
        result: Dict = {"today_open": None, "current": None, "prev_close": None}
        # 5m 線優先 (盤中)
        df = ds.fetch_yf_history(symbol, period="2d", interval="5m")
        if df is not None and not df.empty:
            try:
                df = df.copy()
                date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
                df["_dt"] = pd.to_datetime(df[date_col])
                df["_d"] = df["_dt"].dt.date
                today = df["_d"].max()
                today_bars = df[df["_d"] == today].sort_values("_dt")
                if not today_bars.empty:
                    result["today_open"] = float(today_bars["Open"].iloc[0])
                    result["current"] = float(today_bars["Close"].iloc[-1])
            except Exception:
                pass
        # daily 線取昨收 (倒數第二根)
        df_d = ds.fetch_yf_history(symbol, period="5d", interval="1d")
        if df_d is not None and not df_d.empty and len(df_d) >= 2:
            try:
                close_d = df_d["Close"].astype(float)
                result["prev_close"] = float(close_d.iloc[-2])
                # 5m 抓不到時 fallback 用 daily 補 today_open / current
                if result["today_open"] is None:
                    result["today_open"] = float(df_d["Open"].astype(float).iloc[-1])
                if result["current"] is None:
                    result["current"] = float(close_d.iloc[-1])
            except Exception:
                pass
        if result["current"] is None:
            return None
        return result

    if market.upper() == "US":
        return _try(stock_id)
    for suffix in [".TW", ".TWO"]:
        r = _try(f"{stock_id}{suffix}")
        if r:
            return r
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
    """檢查自選股盤中警報.

    觸發邏輯 (2026-05 重構):
      - 雙門檻: ±5% 跟 ±10%
      - 兩個錨點: 今日開盤 / 昨收, 取較極端的當主
      - 每個 (方向 × 門檻) 一天最多觸發 1 次, 用 fired_buckets 去重
    例:
      股票今天開盤 100, 昨收 102. 現在 95.
      vs 開盤 -5.00%, vs 昨收 -6.86%. 取昨收 -6.86% 觸發 -5% bucket.
      接著跌到 92. vs 開盤 -8%, vs 昨收 -9.8%. 還沒到 -10%, 不觸發 (-5% 已 fired).
      跌到 90. vs 開盤 -10%, vs 昨收 -11.7%. 觸發 -10% bucket.
    """
    items = watchlist_store.load_watchlist()
    if not items:
        return []

    state = watchlist_store.load_monitor_state()
    wl_state = state.setdefault("watchlist_alerts", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")

    alerts: List[Dict] = []
    for item in items:
        sid = item.get("stock_id", "")
        name = item.get("name", "")
        market = item.get("market", "TW").upper()
        entry_price = item.get("entry_price")

        info = _fetch_today_status(sid, market)
        if info is None:
            continue
        today_open = info.get("today_open")
        current = info.get("current")
        prev_close = info.get("prev_close")
        if not current or current <= 0:
            continue

        sid_state = wl_state.setdefault(sid, {})

        # 跨日重置
        if sid_state.get("date") != today_str:
            sid_state.clear()
            sid_state["date"] = today_str
            sid_state["today_open"] = round(today_open, 2) if today_open else None
            sid_state["prev_close"] = round(prev_close, 2) if prev_close else None
            sid_state["fired_buckets"] = []  # list of [direction, threshold]
            try:
                ep = float(entry_price) if entry_price not in (None, "", 0, 0.0) else None
            except Exception:
                ep = None
            sid_state["base_price"] = ep if ep else (round(today_open, 2) if today_open else None)
            sid_state["base_source"] = "entry" if ep else "auto"

        # 算兩個錨點的 %
        today_pct = ((current / today_open - 1) * 100) if (today_open and today_open > 0) else None
        day_pct = ((current / prev_close - 1) * 100) if (prev_close and prev_close > 0) else None

        # 取較極端的當主 (絕對值大者)
        candidates = []
        if today_pct is not None:
            candidates.append(("open", "今日開盤", today_open, today_pct))
        if day_pct is not None:
            candidates.append(("close", "昨收", prev_close, day_pct))
        if not candidates:
            continue
        candidates.sort(key=lambda x: abs(x[3]), reverse=True)
        primary_anchor, anchor_label, anchor_price, primary_pct = candidates[0]

        # 找觸發的 bucket (從大到小檢查) — 使用該市場的自訂門檻
        market_thrs = get_thresholds_for(market)
        triggered_thr = None
        for thr in sorted(market_thrs, reverse=True):
            if abs(primary_pct) >= thr:
                triggered_thr = thr
                break
        if triggered_thr is None:
            continue

        direction = "漲" if primary_pct > 0 else "跌"
        bucket_key = [direction, triggered_thr]

        # dedup: 今日同 direction × thr 已觸發過 → skip
        fired = sid_state.get("fired_buckets", [])
        already = any(b[0] == direction and abs(b[1] - triggered_thr) < 0.01 for b in fired)
        if already:
            continue

        alerts.append({
            "type": "extreme",
            "stock_id": sid,
            "name": name,
            "market": market,
            "current": round(current, 2),
            "today_open": round(today_open, 2) if today_open else None,
            "prev_close": round(prev_close, 2) if prev_close else None,
            "today_pct": round(today_pct, 2) if today_pct is not None else None,
            "day_pct": round(day_pct, 2) if day_pct is not None else None,
            "primary_anchor": primary_anchor,       # "open" / "close"
            "primary_anchor_label": anchor_label,   # 顯示用
            "primary_anchor_price": round(anchor_price, 2),
            "primary_pct": round(primary_pct, 2),
            "threshold": triggered_thr,
            "direction": direction,
        })

        fired.append(bucket_key)
        sid_state["fired_buckets"] = fired

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
