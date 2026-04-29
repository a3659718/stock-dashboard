"""
market_open_picks.py
開盤後 30 分鐘分析：

【台股版】
  1) 計算當下熱門題材 (sector_pulse.compute_hot_themes)
  2) 取「平均%」最高的前 3 個族群（=資金主流）
  3) 每個族群挑 3 檔「動能潛在」的個股 (不一定漲最多)
  4) 動能標準: 站月線 + 量比>1.2 + 5d漲幅<12% + 今日漲幅 0.3~7% (避免追頂)

【美股版】
  1) 板塊 ETF 1d 表現排序，取前 3 板塊
  2) 每個板塊池內挑 3 檔
  3) 額外輸出「成長動能極強 + 近期 IPO」單獨 5 檔
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import data_sources as ds
import market_predictor
import sector_pulse
import stock_catalyst


# ---------------------------------------------------------------------------
# 美股板塊 → 代表性個股池
# ---------------------------------------------------------------------------
US_SECTOR_STOCKS: Dict[str, List[str]] = {
    "XLK": ["NVDA", "MSFT", "AAPL", "AVGO", "AMD", "ADBE", "CRM", "ORCL", "PANW", "CRWD", "PLTR", "SNOW", "MDB"],
    "XLE": ["XOM", "CVX", "COP", "EOG", "OXY", "PSX", "MPC", "SLB"],
    "XLF": ["JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "AXP", "V", "MA"],
    "XLV": ["UNH", "LLY", "JNJ", "MRK", "ABBV", "TMO", "PFE", "ABT"],
    "XLY": ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG", "ABNB"],
    "XLP": ["WMT", "PG", "COST", "KO", "PEP", "PM", "MO"],
    "XLI": ["GE", "BA", "CAT", "DE", "UNP", "RTX", "HON"],
    "XLB": ["LIN", "FCX", "APD", "SHW", "ECL"],
    "XLU": ["NEE", "SO", "DUK", "VST", "CEG"],
    "XLRE": ["PLD", "EQIX", "AMT", "CCI"],
    "XLC": ["META", "GOOGL", "NFLX", "DIS", "TMUS", "CMCSA"],
}


# 「成長動能 / 近期 IPO」池：高成長+高熱度名單 (2023-2025 IPO 或 高 RS)
US_GROWTH_IPO_POOL = [
    "RDDT", "CRWV", "ARM", "ASTS", "RBLX", "CART", "KLC",
    "IONQ", "RGTI", "QBTS", "SOUN", "BBAI",  # AI/Quantum
    "PLTR", "SMCI", "MSTR", "COIN",            # high-momentum
    "OKLO", "VST", "CEG",                       # nuclear/power
    "CRWD", "PANW", "DDOG", "MDB",              # cyber/cloud
    "HOOD", "SOFI", "AFRM",                     # fintech
    "ANET", "SMR", "TEM",                        # other growth
]


# ---------------------------------------------------------------------------
# 共用 helpers
# ---------------------------------------------------------------------------
def _score_stock_momentum(metrics: Dict) -> float:
    """0-10 的動能潛力分數，偏好「起漲位 + 量能配合」。"""
    s = 0.0
    today = metrics.get("今日%")
    five = metrics.get("5日%")
    twenty = metrics.get("20日%") if "20日%" in metrics else None
    ratio = metrics.get("量比")

    if today is not None:
        if 0.3 <= today <= 4:
            s += 2.5
        elif 4 < today <= 7:
            s += 1.5
        elif today > 7:
            s += 0.3  # 太強了反而扣分
        elif today < 0:
            s -= 1
    if ratio is not None:
        if 1.2 <= ratio <= 3:
            s += 2.5
        elif 3 < ratio <= 5:
            s += 1.5
        elif ratio < 0.8:
            s -= 1.0
    if five is not None:
        if 0 <= five <= 8:
            s += 2.0  # 起漲位
        elif 8 < five <= 15:
            s += 1.0
        elif five > 20:
            s -= 1.5
        elif five < -3:
            s -= 0.5
    return round(max(0.0, s), 2)


# ---------------------------------------------------------------------------
# 台股開盤分析
# ---------------------------------------------------------------------------
def get_tw_open_picks(top_themes_n: int = 3, picks_per_theme: int = 3) -> Dict:
    """回傳前 N 族群與每族群挑出的個股。"""
    hot = sector_pulse.compute_hot_themes()
    themes_df = hot.get("themes")
    leaders_map = hot.get("leaders") or {}
    if themes_df is None or themes_df.empty:
        return {"error": "尚未取得題材資料 (盤前/休市?)"}

    top_themes = themes_df.head(top_themes_n)["題材"].tolist()
    picks: List[Dict] = []
    seen_sids: set = set()  # 跨題材去重

    info = ds.get_taiwan_stock_info()
    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}

    for theme in top_themes:
        df = leaders_map.get(theme, pd.DataFrame())
        if df is None or df.empty:
            stock_ids = sector_pulse.TW_THEMES.get(theme, [])
            market_map = info.set_index("stock_id")["type"].to_dict()
            df = sector_pulse.fetch_intraday_metrics(stock_ids, market_map)
        if df is None or df.empty:
            picks.append({"theme": theme, "stocks": pd.DataFrame()})
            continue

        df = df.copy()
        # 跨題材去重
        df = df[~df["stock_id"].isin(seen_sids)]
        df["score"] = df.apply(lambda r: _score_stock_momentum(r.to_dict()), axis=1)
        df = df[df["score"] > 0].sort_values("score", ascending=False).head(picks_per_theme)
        if "stock_name" not in df.columns:
            df["stock_name"] = df["stock_id"].map(name_map).fillna("")
        seen_sids.update(df["stock_id"].tolist())
        picks.append({"theme": theme, "stocks": df})

    # 大盤盤型預測 + 評估歷史準確率
    prediction = market_predictor.predict_tw_pattern()
    if not prediction.get("error"):
        market_predictor.save_prediction(prediction)
    market_predictor.evaluate_pending_predictions()
    accuracy = market_predictor.accuracy_stats(market="TW", lookback_days=30)

    # 替每檔個股補上催化劑摘要 (Gemini 一次批次處理)
    all_picks_rows = []
    for p in picks:
        st_df = p.get("stocks")
        if st_df is None or (hasattr(st_df, "empty") and st_df.empty):
            continue
        for _, row in st_df.iterrows():
            d = row.to_dict()
            d["_theme"] = p["theme"]
            all_picks_rows.append(d)
    catalysts = stock_catalyst.annotate_picks_with_catalysts(all_picks_rows, market="TW")

    return {
        "themes": themes_df.head(top_themes_n),
        "picks": picks,
        "prediction": prediction,
        "accuracy": accuracy,
        "catalysts": catalysts,  # {stock_id: "催化劑文字"}
    }


# ---------------------------------------------------------------------------
# 美股開盤分析
# ---------------------------------------------------------------------------
def _us_stock_metrics(symbol: str) -> Optional[Dict]:
    df = ds.fetch_yf_history(symbol, period="3mo", interval="1d")
    if df.empty or len(df) < 6:
        return None
    try:
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        chg = (last / prev - 1) * 100 if prev else 0
        five = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else None
        twenty = (last / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else None
        avg5_vol = vol.iloc[-6:-1].mean()
        ratio = float(vol.iloc[-1] / avg5_vol) if avg5_vol > 0 else None
        return {
            "symbol": symbol, "現價": round(last, 2),
            "今日%": round(chg, 2), "5日%": round(five, 2) if five is not None else None,
            "20日%": round(twenty, 2) if twenty is not None else None,
            "量比": round(ratio, 2) if ratio else None,
        }
    except Exception:
        return None


def get_us_open_picks(top_sectors_n: int = 3, picks_per_sector: int = 3,
                      growth_top: int = 5) -> Dict:
    """美股版本：板塊輪動排序 + 每板塊 3 檔 + 成長動能 5 檔."""
    sectors = ds.fetch_sector_rotation()
    if sectors is None or sectors.empty:
        return {"error": "板塊資料尚未取得"}

    # 用 1d_% 排序 (剛開盤後最敏感)
    if "1d_%" in sectors.columns:
        sectors_sorted = sectors.sort_values("1d_%", ascending=False).head(top_sectors_n)
    else:
        sectors_sorted = sectors.head(top_sectors_n)

    sector_picks: List[Dict] = []
    seen_us_sids: set = set()
    for _, sec in sectors_sorted.iterrows():
        sym = sec["symbol"]
        candidates = US_SECTOR_STOCKS.get(sym, [])
        # 跨板塊去重
        candidates = [c for c in candidates if c not in seen_us_sids]
        rows = []
        for s in candidates:
            m = _us_stock_metrics(s)
            if not m:
                continue
            m["score"] = _score_stock_momentum(m)
            rows.append(m)
        if rows:
            df = pd.DataFrame(rows)
            df = df[df["score"] > 0].sort_values("score", ascending=False).head(picks_per_sector)
            seen_us_sids.update(df["symbol"].tolist())
            sector_picks.append({"sector": f"{sym} {sec.get('sector','')}", "stocks": df})
        else:
            sector_picks.append({"sector": f"{sym} {sec.get('sector','')}", "stocks": pd.DataFrame()})

    # 成長動能極強 / 近期 IPO 池
    growth_rows = []
    for s in US_GROWTH_IPO_POOL:
        m = _us_stock_metrics(s)
        if not m:
            continue
        # 偏好高 RS / 高 momentum
        # 自製 growth score
        gscore = 0.0
        if m.get("20日%") and m["20日%"] > 0:
            gscore += min(3.0, m["20日%"] / 5.0)
        if m.get("5日%") and 0 <= m["5日%"] <= 15:
            gscore += 2.0
        elif m.get("5日%") and m["5日%"] > 15:
            gscore += 0.5
        if m.get("今日%") and m["今日%"] > 0:
            gscore += min(2.0, m["今日%"] / 2)
        if m.get("量比") and 1.2 <= m["量比"] <= 5:
            gscore += 2.0
        m["growth_score"] = round(gscore, 2)
        growth_rows.append(m)

    growth_df = pd.DataFrame(growth_rows)
    if not growth_df.empty:
        growth_df = growth_df.sort_values("growth_score", ascending=False).head(growth_top)

    # 大盤預測 + 準確率
    prediction = market_predictor.predict_us_pattern()
    if not prediction.get("error"):
        market_predictor.save_prediction(prediction)
    market_predictor.evaluate_pending_predictions()
    accuracy = market_predictor.accuracy_stats(market="US", lookback_days=30)

    # 美股催化劑
    all_us_rows = []
    for sp in sector_picks:
        st_df = sp.get("stocks")
        if st_df is None or st_df.empty:
            continue
        for _, row in st_df.iterrows():
            d = row.to_dict()
            d["stock_id"] = d.get("symbol", "")  # 統一欄位
            d["_sector"] = sp["sector"]
            all_us_rows.append(d)
    if growth_df is not None and not growth_df.empty:
        for _, row in growth_df.iterrows():
            d = row.to_dict()
            d["stock_id"] = d.get("symbol", "")
            d["_sector"] = "成長動能 / IPO"
            all_us_rows.append(d)
    catalysts = stock_catalyst.annotate_picks_with_catalysts(all_us_rows, market="US")

    return {
        "sectors": sectors_sorted,
        "sector_picks": sector_picks,
        "growth": growth_df,
        "prediction": prediction,
        "accuracy": accuracy,
        "catalysts": catalysts,
    }
