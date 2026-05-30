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
# B13 修正: 完整的 ETF / ADR / 高相關股過濾
# ---------------------------------------------------------------------------
# 排除清單 — 純 ETF, 不該出現在「個股推薦」
_US_ETF_BLACKLIST = {
    # 大盤 ETF
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VTV", "VUG", "VEA", "VWO",
    # 板塊 ETF
    "XLK", "XLE", "XLF", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC",
    # 三倍槓桿 ETF
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA", "UPRO", "SPXU", "FAS", "FAZ",
    # 主題 ETF
    "ARKK", "ARKW", "ARKG", "ARKF", "ARKQ", "SMH", "XSD", "IBB", "XBI",
    # 商品 / 債券 ETF
    "GLD", "SLV", "USO", "UNG", "TLT", "HYG", "LQD",
}

# 高度相關股清單 — 同一族群只取 1 檔避免 Top 5 重複曝險
# 用「代表性最強」的標的當 anchor, 其他列為其同類
_US_CORRELATED_GROUPS = [
    {"NVDA", "TSM", "ASML", "AVGO"},          # AI 半導體 (高度同向)
    {"MSFT", "GOOGL", "META", "AAPL"},        # mega-cap tech (相關係數 > 0.8)
    {"COIN", "MSTR", "MARA", "RIOT"},          # 加密貨幣概念
    {"OKLO", "SMR", "CEG", "VST", "NEE"},     # 核電 / 公用事業
    {"PLTR", "AI", "BBAI", "SOUN"},           # AI 軟體
    {"IONQ", "RGTI", "QBTS"},                  # 量子運算
    {"AMD", "INTC", "MU"},                     # CPU/Memory
]


def _dedup_correlated(scored_rows: List[Dict], score_key: str = "score",
                       min_kept: int = 5) -> List[Dict]:
    """同一相關性 group 只保留分數最高的那檔, 避免推薦過度集中.

    B13 + M3 修正: 若 dedup 後不足 min_kept 檔, 把被砍掉的「同 group 次高分」
    依序補回, 直到湊滿 min_kept 或用完候選為止.
    防止「Top 10 都在 mega-cap tech group → dedup 後只剩 1 檔」的問題.
    """
    sorted_rows = sorted(scored_rows, key=lambda r: r.get(score_key, 0), reverse=True)
    kept = []
    deferred = []  # 被 dedup 砍掉的, 留作備援
    used_groups = []
    for row in sorted_rows:
        sym = row.get("symbol", "")
        my_group = None
        for g in _US_CORRELATED_GROUPS:
            if sym in g:
                my_group = g
                break
        if my_group is not None and my_group in used_groups:
            deferred.append(row)  # 先記下, 不足時補回
            continue
        kept.append(row)
        if my_group is not None:
            used_groups.append(my_group)

    # M3 fallback: 不足 min_kept 時補回 deferred (仍按分數)
    if len(kept) < min_kept and deferred:
        need = min_kept - len(kept)
        kept.extend(deferred[:need])
        # 重新排序確保高分在前
        kept.sort(key=lambda r: r.get(score_key, 0), reverse=True)
    return kept


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_us_recommendation(top_n: int = 5, dedup_correlated: bool = True) -> dict:
    """B13: dedup_correlated=True 時, 同一相關性族群只取分數最高的那檔."""
    syms = _watchlist()
    spy_df = ds.fetch_yf_history("SPY", period="3mo")
    fg = ds.fetch_fear_greed()
    sector = ds.fetch_sector_rotation()
    news_pool = ds.fetch_market_news_themes()

    rows = []
    for sym in syms:
        # B13: 用 ETF blacklist 取代寫死的 4 檔
        if sym in _US_ETF_BLACKLIST:
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

    # B13 + M3: 同族群去重 (避免 Top 5 都是 AI 半導體), 但不足時自動補回
    if dedup_correlated:
        rows = _dedup_correlated(rows, score_key="score", min_kept=top_n)

    df_all = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    top_df = df_all.head(top_n).copy()

    # 補催化劑（美股 Top 5）
    try:
        import stock_catalyst
        records = []
        for _, r in top_df.iterrows():
            records.append({
                "stock_id": r.get("symbol", ""),
                "stock_name": r.get("symbol", ""),
                "今日%": r.get("daily_%"),
            })
        cat_map = stock_catalyst.annotate_picks_with_catalysts(records, market="US")
        top_df["催化劑"] = top_df["symbol"].astype(str).map(cat_map).fillna("")
    except Exception:
        pass

    # C: 對 top_picks 加 quick entry 評估 (入場標籤)
    try:
        import entry_label_helper as _el
        syms = top_df["symbol"].astype(str).tolist()
        pairs = [(s, "US") for s in syms]
        eval_map = _el.batch_evaluate(pairs, max_workers=8)
        top_df["入場標籤"] = top_df["symbol"].astype(str).map(
            lambda s: ((eval_map.get(s) or {}).get("entry_emoji", "") + " " +
                       (eval_map.get(s) or {}).get("entry_label", "—")).strip()
        )
        top_df["入場分數"] = top_df["symbol"].astype(str).map(
            lambda s: (eval_map.get(s) or {}).get("entry_score")
        )
    except Exception as _e:
        print(f"[us_screener] entry_label 計算失敗 (non-fatal): {_e}", flush=True)

    return {
        "top_picks": top_df,
        "all_scored": df_all,
        "fear_greed": fg,
        "sectors": sector,
        "news": news_pool,
    }
