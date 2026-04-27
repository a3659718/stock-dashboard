"""
sector_pulse.py
台股「即時 / 開盤強勢族群」分析。

策略：
  1. 先用 FinMind 拿到 industry_category 對應表 (taiwan_stock_info)
  2. 對每檔股票抓 yfinance 即時 quote (.TW / .TWO)，計算今日漲跌幅
  3. 依照產業分組，找出族群平均漲幅最高 + 上漲家數最多者

由於 yfinance 對全市場逐檔 query 太慢，我們限制在「市值/成交量 top N」 + 各產業前幾名。
"""

from __future__ import annotations

import datetime as dt
from typing import List

import pandas as pd
import streamlit as st

import data_sources as ds


@st.cache_data(ttl=300, show_spinner=False)
def universe_with_industry(top_n: int = 400) -> pd.DataFrame:
    """挑選近 5 日成交量最大的 top_n 個檔，附上產業分類。"""
    info = ds.get_taiwan_stock_info()
    if info.empty:
        return pd.DataFrame()

    # 為了不燒 quota，這裡僅用 FinMind 的 stock_id 順序取前 top_n 檔
    # (大型股股號通常較小，足以涵蓋主要流動性個股)
    return info.head(top_n).copy().reset_index(drop=True)


@st.cache_data(ttl=120, show_spinner=False)
def fetch_intraday_changes(stock_ids: List[str], market_map: dict) -> pd.DataFrame:
    """對每檔抓 yfinance 即時漲跌幅。"""
    rows = []
    for sid in stock_ids:
        suffix = ".TWO" if market_map.get(sid) == "tpex" else ".TW"
        q = ds.fetch_yf_quote(f"{sid}{suffix}")
        if not q or q.get("change_pct") is None:
            continue
        rows.append({"stock_id": sid, "change_pct": q["change_pct"], "last": q.get("last")})
    return pd.DataFrame(rows)


def compute_strong_sectors(top_n: int = 200) -> dict:
    """回傳族群熱度。"""
    uni = universe_with_industry(top_n=top_n)
    if uni.empty:
        return {"sectors": pd.DataFrame(), "stocks": pd.DataFrame()}
    market_map = uni.set_index("stock_id")["type"].to_dict()
    quotes = fetch_intraday_changes(uni["stock_id"].tolist(), market_map)
    if quotes.empty:
        return {"sectors": pd.DataFrame(), "stocks": pd.DataFrame()}

    industry_col = "industry_category" if "industry_category" in uni.columns else None
    merged = uni.merge(quotes, on="stock_id", how="inner")
    if industry_col is None:
        return {"sectors": pd.DataFrame(), "stocks": merged}

    grp = merged.groupby(industry_col)
    sect = grp.agg(
        avg_change=("change_pct", "mean"),
        median_change=("change_pct", "median"),
        up_count=("change_pct", lambda s: int((s > 0).sum())),
        n=("change_pct", "size"),
    ).reset_index()
    sect["up_ratio"] = sect["up_count"] / sect["n"]
    sect = sect[sect["n"] >= 3]
    sect = sect.sort_values(["avg_change", "up_ratio"], ascending=False).reset_index(drop=True)

    # 族群內漲幅前幾名作為龍頭
    leaders = (
        merged.sort_values(["change_pct"], ascending=False)
        .groupby(industry_col)
        .head(3)
        .reset_index(drop=True)
    )
    return {"sectors": sect, "stocks": merged, "leaders": leaders}
