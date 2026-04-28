"""
us_screener.py
美股推薦：技術突破 + 動能 + 新聞題材熱度 + Fear & Greed / 板塊輪動。

最後輸出 Top 5。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List

import pandas as pd
import streamlit as st

import data_sources as ds

# ---------------------------------------------------------------------------
# 候選池 (S&P 100 + 高熱度科技 / AI 個股，可在 secrets 自定 watchlist)
# ---------------------------------------------------------------------------
DEFAULT_UNIVERSE = [
    # Mega cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "AMD", "ADBE",
    "CRM", "ORCL", "INTC", "QCOM", "MU", "TXN", "ASML", "TSM",
    # AI / cloud / cyber
    "PLTR", "SNOW", "CRWD", "PANW", "NET", "DDOG", "MDB", "SMCI", "ARM", "MRVL",
    # Megacap non-tech
    "BRK-B", "JPM", "BAC", "V", "MA", "WMT", "COST", "PG", "JNJ", "UNH", "HD",
    "XOM", "CVX", "GE", "BA", "CAT", "DE", "LMT",
    # Consumer / momentum
    "NFLX", "DIS", "MCD", "SBUX", "NKE", "ABNB", "UBER", "SHOP", "COIN", "MSTR",
    # EV / energy
    "RIVN", "LCID", "ENPH", "FSLR", "OKLO", "CEG", "VST",
    # ETF benchmark
    "SPY", "QQQ", "IWM", "DIA",
]


def _watchlist() -> List[str]:
    custom = ds._secret("US_WATCHLIST", "").strip()
    if custom:
        wl = [s.strip().upper() for s in re.split(r"[,\s]+", custom) if s.strip()]
        return list(dict.fromkeys(wl))
    return DEFAULT_UNIVERSE


# ---------------------------------------------------------------------------
# 個股技術分數
# ---------------------------------------------------------------------------
def _ma_breakout_score(df: pd.DataFrame) -> Dict:
    """回傳: ma20_break / ma50_break / ma200_break / volume_ratio / score."""
    if df.empty or len(df) < 60:
        return {}
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean() if len(df) >= 200 else None

    res = {
        "last": float(close.iloc[-1]),
        "ma20": float(ma20.iloc[-1]) if not math.isnan(ma20.iloc[-1]) else None,
        "ma50": float(ma50.iloc[-1]) if not math.isnan(ma50.iloc[-1]) else None,
        "ma200": float(ma200.iloc[-1]) if ma200 is not None and not math.isnan(ma200.iloc[-1]) else None,
        "ma20_break": bool(close.iloc[-1] > ma20.iloc[-1] and close.iloc[-2] <= ma20.iloc[-2]),
        "ma50_break": bool(close.iloc[-1] > ma50.iloc[-1] and close.iloc[-2] <= ma50.iloc[-2]),
    }
    avg5_vol = vol.iloc[-6:-1].mean()
    res["vol_ratio"] = float(vol.iloc[-1] / avg5_vol) if avg5_vol > 0 else None
    return res


def _momentum_metrics(df: pd.DataFrame, spy_df: pd.DataFrame) -> Dict:
    """漲幅、相對 SPY 強度。"""
    if df.empty or len(df) < 22:
        return {}
    close = df["Close"].astype(float)
    daily_pct = (close.iloc[-1] / close.iloc[-2] - 1) * 100
    five_pct = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else None
    twenty_pct = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) >= 21 else None

    rs = None
    if not spy_df.empty and len(spy_df) >= 22:
        spy_close = spy_df["Close"].astype(float)
        spy_20 = (spy_close.iloc[-1] / spy_close.iloc[-21] - 1) * 100
        if twenty_pct is not None and spy_20 != 0:
            rs = round(twenty_pct - spy_20, 2)

    return {
        "daily_pct": round(float(daily_pct), 2),
        "five_pct": round(float(five_pct), 2) if five_pct is not None else None,
        "twenty_pct": round(float(twenty_pct), 2) if twenty_pct is not None else None,
        "rs_vs_spy_20d": rs,
    }


# ---------------------------------------------------------------------------
# 新聞題材熱度
# ---------------------------------------------------------------------------
THEME_KEYWORDS = {
    "AI": ["AI", "artificial intelligence", "GPT", "LLM", "chatbot", "generative"],
    "Chips/Semi": ["chip", "semiconductor", "GPU", "wafer", "fab"],
    "Cloud": ["cloud", "AWS", "Azure", "data center"],
    "Cybersecurity": ["cyber", "security", "ransomware"],
    "EV/Battery": ["EV", "electric vehicle", "battery", "Tesla"],
    "Energy": ["oil", "OPEC", "natural gas", "LNG"],
    "Crypto": ["bitcoin", "crypto", "ETF approval", "ethereum"],
    "Fed/Rates": ["Fed", "rate cut", "FOMC", "inflation", "CPI"],
    "Earnings": ["earnings", "guidance", "beats", "misses"],
}


def _theme_score_for(symbol: str, news_pool: List[Dict]) -> Dict:
    """根據新聞抓題材熱度。"""
    sym_news = [n for n in news_pool if symbol.upper() in (n.get("relatedTickers") or [])]
    sym_news.extend(ds.fetch_yahoo_news(symbol, max_n=4))
    titles = " ".join((n.get("title") or "") for n in sym_news).lower()
    themes = []
    for theme, kws in THEME_KEYWORDS.items():
        if any(k.lower() in titles for k in kws):
            themes.append(theme)
    return {"news_count": len(sym_news), "themes": themes, "news": sym_news[:3]}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_us_recommendation(top_n: int = 5) -> dict:
    syms = _watchlist()
    spy_df = ds.fetch_yf_history("SPY", period="3mo")
    fg = ds.fetch_fear_greed()
    sector = ds.fetch_sector_rotation()
    news_pool = ds.fetch_market_news_themes()

    rows = []
    for sym in syms:
        if sym in {"SPY", "QQQ", "IWM", "DIA"}:
            continue
        df = ds.fetch_yf_history(sym, period="6mo")
        if df.empty or len(df) < 30:
            continue
        tech = _ma_breakout_score(df)
        mom = _momentum_metrics(df, spy_df)
        theme = _theme_score_for(sym, news_pool)

        # 評分權重 (消息面加重)
        score = 0
        reasons: List[str] = []
        if tech.get("ma20_break"):
            score += 1.5; reasons.append("突破 MA20")
        if tech.get("ma50_break"):
            score += 1.5; reasons.append("突破 MA50")
        if tech.get("vol_ratio") and tech["vol_ratio"] >= 1.5:
            score += 1.0; reasons.append(f"量比 {tech['vol_ratio']:.1f}x")
        if mom.get("daily_pct") and mom["daily_pct"] > 1:
            score += 0.5; reasons.append(f"當日 +{mom['daily_pct']:.1f}%")
        if mom.get("rs_vs_spy_20d") and mom["rs_vs_spy_20d"] > 0:
            score += min(2.0, mom["rs_vs_spy_20d"] / 5.0)
            reasons.append(f"RS+{mom['rs_vs_spy_20d']:.1f}")
        # 消息面權重提高
        if theme["themes"]:
            score += 1.0 * len(theme["themes"])
            reasons.append(f"題材: {', '.join(theme['themes'])}")
        if theme["news_count"] >= 3:
            score += 1.5; reasons.append(f"新聞熱度高 ({theme['news_count']} 則)")
        elif theme["news_count"] >= 1:
            score += 0.5

        rows.append({
            "symbol": sym,
            "last": tech.get("last"),
            "daily_%": mom.get("daily_pct"),
            "5d_%": mom.get("five_pct"),
            "20d_%": mom.get("twenty_pct"),
            "RS_20d": mom.get("rs_vs_spy_20d"),
            "MA20突破": "Y" if tech.get("ma20_break") else "",
            "MA50突破": "Y" if tech.get("ma50_break") else "",
            "量比": tech.get("vol_ratio"),
            "題材": ", ".join(theme["themes"]) if theme["themes"] else "",
            "近期新聞": theme["news"],
            "進場理由": " · ".join(reasons),
            "score": round(float(score), 2),
        })

    if not rows:
        return {"top_picks": pd.DataFrame(), "fear_greed": fg, "sectors": sector, "news": news_pool}

    df_all = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    return {
        "top_picks": df_all.head(top_n),
        "all_scored": df_all,
        "fear_greed": fg,
        "sectors": sector,
        "news": news_pool,
    }
