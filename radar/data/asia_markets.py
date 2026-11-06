"""
asia_markets.py
日股 (Nikkei 225) + 韓股 (KOSPI) + 港股 (Hang Seng) 大盤監控。

偵測事件：
  1. 急漲：當日漲幅 >= +1.5%
  2. 急跌：當日跌幅 <= -1.5%
  3. 由跌轉漲：5 日累計為負，今日 +0.5% 以上
  4. 由漲轉跌：5 日累計為正，今日 -0.5% 以上
  5. 突破前高：今日收創 20 日新高
  6. 跌破前低：今日收創 20 日新低

訊號使用 yfinance 取資料 (有 15 分延遲，但對日 K 來說足夠)。
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

from radar.data import data_sources as ds


ASIA_INDICES = {
    "日經 225":  {"symbol": "^N225",  "country": "🇯🇵"},
    "韓國 KOSPI": {"symbol": "^KS11",  "country": "🇰🇷"},
    "香港 HSI":   {"symbol": "^HSI",   "country": "🇭🇰"},
    "上證指數":   {"symbol": "000001.SS", "country": "🇨🇳"},
}


def _detect_events(name: str, country: str, close: pd.Series) -> List[Dict]:
    """從 close series 偵測事件 (回傳事件 list)."""
    if close is None or len(close) < 21:
        return []
    events = []
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    daily_pct = (last / prev - 1) * 100 if prev else 0
    five_pct = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0
    # 昨天為止的 5d trend
    prev_5d_pct = (prev / float(close.iloc[-7]) - 1) * 100 if len(close) >= 7 else 0
    high_20 = float(close.iloc[-21:].max())
    low_20 = float(close.iloc[-21:].min())

    base = {
        "market": name, "country": country,
        "last": round(last, 2),
        "daily_pct": round(daily_pct, 2),
        "5d_pct": round(five_pct, 2),
        "high_20d": round(high_20, 2),
        "low_20d": round(low_20, 2),
    }

    # 1) 急漲 / 急跌
    if abs(daily_pct) >= 1.5:
        if daily_pct > 0:
            events.append({**base, "event": "急漲", "severity": "high",
                            "msg": f"當日 +{daily_pct:.2f}% (5d 累計 {five_pct:+.2f}%)"})
        else:
            events.append({**base, "event": "急跌", "severity": "high",
                            "msg": f"當日 {daily_pct:.2f}% (5d 累計 {five_pct:+.2f}%)"})
    # 2) 由跌轉漲 / 由漲轉跌 (只在沒有觸發急漲急跌時才送，避免重複)
    else:
        if prev_5d_pct < -1.5 and daily_pct > 0.5:
            events.append({**base, "event": "由跌轉漲", "severity": "medium",
                            "msg": f"近 5 日累跌 {prev_5d_pct:.2f}%，今日反彈 +{daily_pct:.2f}%"})
        elif prev_5d_pct > 1.5 and daily_pct < -0.5:
            events.append({**base, "event": "由漲轉跌", "severity": "medium",
                            "msg": f"近 5 日累漲 +{prev_5d_pct:.2f}%，今日回檔 {daily_pct:.2f}%"})

    # 3) 突破 / 跌破 (跟急漲跌獨立)
    if last >= high_20 * 0.999 and daily_pct > 0:
        events.append({**base, "event": "突破 20 日新高", "severity": "low",
                        "msg": f"創 20 日新高 {last:,.0f}"})
    if last <= low_20 * 1.001 and daily_pct < 0:
        events.append({**base, "event": "跌破 20 日新低", "severity": "low",
                        "msg": f"跌破 20 日新低 {last:,.0f}"})

    return events


@st.cache_data(ttl=600, show_spinner=False)
def check_asia_markets() -> Dict:
    """檢查所有亞洲指數，回傳 {snapshot, events}。
    snapshot: 各市場當前價格 / 漲跌幅 (即使沒事件也會有資料)
    events: list of 事件 dict (急漲/急跌/反轉)
    """
    snapshot: List[Dict] = []
    all_events: List[Dict] = []

    for name, info in ASIA_INDICES.items():
        sym = info["symbol"]
        country = info["country"]
        df = ds.fetch_yf_history(sym, period="2mo", interval="1d")
        if df.empty or len(df) < 6:
            continue
        try:
            close = df["Close"].astype(float)
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2]) if len(close) >= 2 else last
            daily_pct = (last / prev - 1) * 100 if prev else 0
            five_pct = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0
            snapshot.append({
                "market": name, "country": country, "symbol": sym,
                "last": round(last, 2),
                "daily_pct": round(daily_pct, 2),
                "5d_pct": round(five_pct, 2),
            })
            # Bug fix: 沒檢查最後一根是不是今天 → 日 / 韓 / 港 / 陸休市日 (例 黃金週) 會把上一個
            # 交易日的「當日 -2% 急跌」天天重報。台北 10:00 後 (亞洲各市場都已開盤) 最新日線
            # 仍不是今天 → 視為休市, 不產生事件 (快照照列, 標註休市)。
            try:
                import datetime as _dtm
                _now_tpe = _dtm.datetime.utcnow() + _dtm.timedelta(hours=8)
                _dcol = "Date" if "Date" in df.columns else df.columns[0]
                _last_d = pd.to_datetime(df[_dcol].iloc[-1]).date()
                if _now_tpe.hour >= 10 and _last_d != _now_tpe.date():
                    snapshot[-1]["stale"] = True
                    snapshot[-1]["note"] = f"休市 (最新 {_last_d})"
                    continue
            except Exception as _se:
                print(f"[asia_markets] {sym} 休市判斷失敗 (照常偵測): {_se}", flush=True)
            events = _detect_events(name, country, close)
            all_events.extend(events)
        except Exception:
            continue

    return {"snapshot": snapshot, "events": all_events}
