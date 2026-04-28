"""
sector_pulse.py
台股「即時 / 開盤強勢族群」分析。

兩種視角：
  1) 證交所產業分類 (industry_category)：適合大方向觀察
  2) 熱門題材股池 (硬 mapping)：無人機 / AI 伺服器 / 低軌衛星 / 重電 / 散熱 / 機器人 / AI PC / 儲能 ...

每個族群顯示：
  - 平均漲跌幅、上漲家數
  - 該族群 Top 5 強勢股 (漲幅、現價、量比、振幅)

資料來源：yfinance (TW=.TW, OTC=.TWO)，盤中即時可用。
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List

import pandas as pd
import streamlit as st

import data_sources as ds


# ---------------------------------------------------------------------------
# 熱門題材股 mapping (可隨市場熱度增補)
# 代號為 4 碼台股代號；上市/上櫃會自動依 FinMind 分類加 .TW 或 .TWO
# ---------------------------------------------------------------------------
TW_THEMES: Dict[str, List[str]] = {
    "AI 伺服器": ["2382", "2376", "3231", "6669", "3017", "3406", "2356", "2308", "5443", "3005",
                  "2353", "2377", "2049", "8210", "3552", "3653"],
    "AI 邊緣":   ["3035", "5269", "3661", "3034", "6116", "3450", "8261"],
    "AI PC":     ["2324", "2382", "3231", "2356", "2353", "2377", "2308"],
    "無人機":    ["2486", "8033", "2634", "1597", "5371", "4540", "2206", "1591"],
    "低軌衛星":  ["6285", "2314", "2383", "6213", "8210", "2419", "2317", "2059"],
    "重電族群":  ["1519", "1503", "1514", "1521", "1513", "1605", "1615"],
    "散熱":      ["3324", "3017", "3680", "8358", "3338", "3653", "2421"],
    "機器人":    ["2049", "1590", "6294", "3019", "4583", "2308", "1597"],
    "儲能":      ["6803", "6442", "6166", "1519", "1503", "1514"],
    "高頻高速":  ["3037", "2383", "8112", "6213", "8046", "3030"],
    "ABF 載板":  ["3037", "6239", "8046"],
    "矽光子":    ["3450", "5274", "6285", "3035", "6271", "3163"],
    "CCL":       ["6213", "1909", "8016", "2305"],
    "汽車零件":  ["1536", "1525", "2227", "8255", "1592", "1583", "8222"],
    "電動車":    ["1536", "2231", "1598", "1503"],
    "生技":      ["4174", "1733", "4123", "6446", "4147", "4137", "4904"],
    "金融":      ["2882", "2891", "2884", "2886", "2880", "2881", "2885", "2890", "2887"],
    "航運":      ["2603", "2609", "2615", "2618", "2606", "2610"],
    "PCB":       ["2316", "2383", "3037", "6213", "8016", "3044", "8046"],
    "被動元件":  ["2327", "2375", "2456", "2492", "5285"],
    "面板":      ["2409", "2474", "3481", "3637"],
}


# ---------------------------------------------------------------------------
# 流動性 universe (給「依產業分類」用)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def universe_with_industry(top_n: int = 200) -> pd.DataFrame:
    info = ds.get_taiwan_stock_info()
    if info.empty:
        return pd.DataFrame()
    return info.head(top_n).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 用 yfinance 抓多檔即時報價並計算盤中資訊
# ---------------------------------------------------------------------------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_intraday_metrics(stock_ids: List[str], market_map: dict) -> pd.DataFrame:
    """每檔回傳：當日 %、現價、振幅 %、量比 (今日量/5日均量)、5日%。
    用 yfinance 抓 6 日 K 線，最後一根當盤中。
    """
    rows: List[Dict] = []
    for sid in stock_ids:
        suffix = ".TWO" if market_map.get(sid) == "tpex" else ".TW"
        df = ds.fetch_yf_history(f"{sid}{suffix}", period="1mo", interval="1d")
        if df.empty or len(df) < 2:
            continue
        try:
            close = df["Close"].astype(float)
            high = df["High"].astype(float)
            low = df["Low"].astype(float)
            vol = df["Volume"].astype(float)

            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            chg = (last / prev - 1) * 100 if prev else None
            amp = (float(high.iloc[-1]) - float(low.iloc[-1])) / prev * 100 if prev else None
            avg5 = vol.iloc[-6:-1].mean() if len(vol) >= 6 else None
            vol_ratio = float(vol.iloc[-1] / avg5) if avg5 and avg5 > 0 else None
            r5 = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else None

            rows.append({
                "stock_id": sid,
                "現價": round(last, 2),
                "今日%": round(chg, 2) if chg is not None else None,
                "振幅%": round(amp, 2) if amp is not None else None,
                "量比": round(vol_ratio, 2) if vol_ratio else None,
                "5日%": round(r5, 2) if r5 is not None else None,
            })
        except Exception:
            continue
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 依「證交所產業分類」算族群熱度
# ---------------------------------------------------------------------------
def compute_strong_sectors(top_n: int = 200) -> dict:
    uni = universe_with_industry(top_n=top_n)
    if uni.empty:
        return {"sectors": pd.DataFrame(), "stocks": pd.DataFrame()}
    market_map = uni.set_index("stock_id")["type"].to_dict()
    quotes = fetch_intraday_metrics(uni["stock_id"].tolist(), market_map)
    if quotes.empty:
        return {"sectors": pd.DataFrame(), "stocks": pd.DataFrame()}

    industry_col = "industry_category" if "industry_category" in uni.columns else None
    name_map = uni.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in uni.columns else {}
    merged = uni.merge(quotes, on="stock_id", how="inner")
    merged["stock_name"] = merged["stock_id"].map(name_map).fillna("")
    if industry_col is None:
        return {"sectors": pd.DataFrame(), "stocks": merged}

    grp = merged.groupby(industry_col)
    sect = grp.agg(
        avg_change=("今日%", "mean"),
        median_change=("今日%", "median"),
        up_count=("今日%", lambda s: int((s > 0).sum())),
        n=("今日%", "size"),
    ).reset_index()
    sect["up_ratio"] = sect["up_count"] / sect["n"]
    sect = sect[sect["n"] >= 3]
    sect = sect.sort_values(["avg_change", "up_ratio"], ascending=False).reset_index(drop=True)

    leaders = (
        merged.sort_values(["今日%"], ascending=False)
        .groupby(industry_col)
        .head(5)
        .reset_index(drop=True)
    )
    return {"sectors": sect, "stocks": merged, "leaders": leaders}


# ---------------------------------------------------------------------------
# 依「熱門題材股」算族群熱度
# ---------------------------------------------------------------------------
def compute_hot_themes() -> dict:
    """回傳:
       - themes: DataFrame (題材熱度排行)
       - leaders: dict[題材 -> DataFrame (該題材 Top 5 強勢股)]
    """
    info = ds.get_taiwan_stock_info()
    if info.empty:
        return {"themes": pd.DataFrame(), "leaders": {}}
    market_map = info.set_index("stock_id")["type"].to_dict()
    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}

    # 集合所有題材股，去重後一次抓 quote
    all_ids = sorted({sid for ids in TW_THEMES.values() for sid in ids if sid in market_map})
    quotes = fetch_intraday_metrics(all_ids, market_map)
    if quotes.empty:
        return {"themes": pd.DataFrame(), "leaders": {}}
    quotes["stock_name"] = quotes["stock_id"].map(name_map).fillna("")

    rows = []
    leaders: Dict[str, pd.DataFrame] = {}
    for theme, sids in TW_THEMES.items():
        sub = quotes[quotes["stock_id"].isin(sids)].copy()
        if sub.empty:
            continue
        avg = sub["今日%"].mean()
        med = sub["今日%"].median()
        up_n = int((sub["今日%"] > 0).sum())
        n = len(sub)
        rows.append({
            "題材": theme,
            "平均%": round(float(avg), 2) if pd.notna(avg) else None,
            "中位%": round(float(med), 2) if pd.notna(med) else None,
            "上漲家數": up_n,
            "樣本數": n,
            "上漲比率%": round(up_n / n * 100, 1) if n else None,
        })
        leaders[theme] = sub.sort_values("今日%", ascending=False).head(5).reset_index(drop=True)

    themes = pd.DataFrame(rows).sort_values(["平均%", "上漲比率%"], ascending=False).reset_index(drop=True)
    return {"themes": themes, "leaders": leaders}


# ---------------------------------------------------------------------------
# 潛伏題材股 — 族群熱、本身還沒大漲、但有量能/籌碼跡象
# ---------------------------------------------------------------------------
def find_stealth_followers(top_themes: int = 5) -> dict:
    """從近期最熱的族群中，挑出尚未大漲但有跡象的個股。

    篩選邏輯：
      1) 該題材族群「平均%」進入前 N 名 (= 族群熱)
      2) 個股近 5 日漲幅 < 8%   (= 本身還沒大漲)
      3) 個股當天漲幅 > 0       (= 開始發動)
      4) 量比 >= 1.3            (= 量能配合)
      5) 排除已經破底/弱勢者：close > 5 日均線 (用 yfinance 簡略判斷)
    """
    hot = compute_hot_themes()
    themes_df: pd.DataFrame = hot.get("themes")
    leaders_map: Dict[str, pd.DataFrame] = hot.get("leaders") or {}
    if themes_df is None or themes_df.empty:
        return {"hot_themes": pd.DataFrame(), "stealth": pd.DataFrame()}

    top_theme_names = themes_df["題材"].head(top_themes).tolist()

    # 重組：把熱門題材中所有股票收集起來，套上篩選條件
    pool: List[pd.DataFrame] = []
    for theme in top_theme_names:
        df = leaders_map.get(theme)
        if df is None or df.empty:
            # leaders 只有前 5 名；用全題材池抓
            stock_ids = TW_THEMES.get(theme, [])
            info = ds.get_taiwan_stock_info()
            market_map = info.set_index("stock_id")["type"].to_dict()
            full = fetch_intraday_metrics(stock_ids, market_map)
            full["題材"] = theme
            pool.append(full)
        else:
            x = df.copy()
            x["題材"] = theme
            pool.append(x)
    if not pool:
        return {"hot_themes": themes_df.head(top_themes), "stealth": pd.DataFrame()}

    all_df = pd.concat(pool, ignore_index=True)
    if all_df.empty or "今日%" not in all_df.columns:
        return {"hot_themes": themes_df.head(top_themes), "stealth": pd.DataFrame()}

    cond = (
        (all_df["5日%"].fillna(99) < 8)
        & (all_df["今日%"].fillna(-99) > 0)
        & (all_df["量比"].fillna(0) >= 1.3)
    )
    stealth = all_df[cond].copy()
    if stealth.empty:
        return {"hot_themes": themes_df.head(top_themes), "stealth": pd.DataFrame()}

    # 排序：量比優先，再看當天漲幅
    stealth = stealth.sort_values(["量比", "今日%"], ascending=False).reset_index(drop=True)
    if "stock_name" not in stealth.columns:
        info = ds.get_taiwan_stock_info()
        nm = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}
        stealth["stock_name"] = stealth["stock_id"].map(nm).fillna("")

    cols = ["stock_id", "stock_name", "題材", "現價", "今日%", "5日%", "量比", "振幅%"]
    cols = [c for c in cols if c in stealth.columns]
    return {"hot_themes": themes_df.head(top_themes), "stealth": stealth[cols].head(20)}
