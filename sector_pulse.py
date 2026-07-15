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
def _fetch_intraday_metrics_cached(stock_ids_tuple: tuple, market_map_tuple: tuple) -> pd.DataFrame:
    """內部快取版本 — 接 tuple 確保 cache hash 穩定.

    Streamlit cache_data 對 list / dict 的 hash 用 pickle, 順序不穩定 → cache 永遠 miss.
    對外 wrapper (fetch_intraday_metrics) 把 list / dict normalize 成 tuple 後才呼叫.
    """
    market_map = dict(market_map_tuple)
    rows: List[Dict] = []
    for sid in stock_ids_tuple:
        _t = market_map.get(sid)
        # 分類未知 (FinMind 額度爆 402 / 失效 → market_map 空) 時「兩個後綴都試」,
        # 不要只抓 .TW → 上櫃股會靜默抓不到, 整個題材分析縮水。
        _suffixes = [".TWO"] if _t == "tpex" else ([".TW"] if _t else [".TW", ".TWO"])
        df = None
        for _sfx in _suffixes:
            df = ds.fetch_yf_history(f"{sid}{_sfx}", period="1mo", interval="1d")
            if df is not None and not df.empty and len(df) >= 2:
                break
        if df is None or df.empty or len(df) < 2:
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


def fetch_intraday_metrics(stock_ids: List[str], market_map: dict) -> pd.DataFrame:
    """每檔回傳：當日 %、現價、振幅 %、量比 (今日量/5日均量)、5日%。

    對外的穩定接口 — 把 list/dict normalize 成 tuple, cache 才會命中.
    Caller 可以隨意亂序傳 list/dict, 結果一致.
    """
    stock_ids_tuple = tuple(sorted(set(s for s in stock_ids if s)))
    # 只保留會用到的 sid 對應的 market type, 縮小 hash key
    market_map_tuple = tuple(sorted(
        (sid, market_map.get(sid)) for sid in stock_ids_tuple
    ))
    df = _fetch_intraday_metrics_cached(stock_ids_tuple, market_map_tuple)
    # Bug fix: 空結果(多半是 yfinance 一時限流)原本會被 cache 住 120s, 害用戶連按
    # 幾次「熱門題材」都拿到同一份空資料 → 看起來「按了沒反應」。空結果就清掉 cache,
    # 讓下一次點擊真的重抓, 而不是吃到被毒化的快取。
    if df is None or df.empty:
        try:
            _fetch_intraday_metrics_cached.clear()
        except Exception:
            pass
    return df


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

    # 去重：股票只出現在排名最高的產業
    leaders_rows = []
    seen = set()
    for ind in sect[industry_col].tolist():
        sub = merged[merged[industry_col] == ind].sort_values("今日%", ascending=False)
        sub = sub[~sub["stock_id"].isin(seen)].head(5)
        leaders_rows.append(sub)
        seen.update(sub["stock_id"].tolist())
    leaders = pd.concat(leaders_rows, ignore_index=True) if leaders_rows else pd.DataFrame()

    # 補催化劑（與 compute_hot_themes 對稱; 用一次 Gemini 批次處理所有 leaders）
    if leaders is not None and not leaders.empty:
        try:
            import stock_catalyst
            cat_map = stock_catalyst.annotate_picks_with_catalysts(
                leaders.to_dict("records"), market="TW",
            )
            leaders["催化劑"] = leaders["stock_id"].astype(str).map(cat_map).fillna("")
        except Exception as _e:
            print(f"[sector_pulse] strong_sectors annotate catalyst failed: {_e}", flush=True)
        # C: 對 leaders 批次跑 quick entry 評估, 加 入場標籤 + 入場分數
        try:
            import entry_label_helper as _el
            sym_list = leaders["stock_id"].astype(str).tolist()
            pairs = [(s, "TW") for s in sym_list]
            eval_map = _el.batch_evaluate(pairs, max_workers=8)
            leaders["入場標籤"] = leaders["stock_id"].astype(str).map(
                lambda s: ((eval_map.get(s) or {}).get("entry_emoji", "") + " " +
                           (eval_map.get(s) or {}).get("entry_label", "—")).strip()
            )
            leaders["入場分數"] = leaders["stock_id"].astype(str).map(
                lambda s: (eval_map.get(s) or {}).get("entry_score")
            )
        except Exception as _e:
            print(f"[sector_pulse] strong_sectors entry_label failed: {_e}", flush=True)

    return {"sectors": sect, "stocks": merged, "leaders": leaders}


# ---------------------------------------------------------------------------
# 依「熱門題材股」算族群熱度
# ---------------------------------------------------------------------------
def compute_hot_themes() -> dict:
    """回傳:
       - themes: DataFrame (題材熱度排行)
       - leaders: dict[題材 -> DataFrame (該題材 Top 5 強勢股)]
    FinMind 失敗時 graceful return 空 dict, 不 raise.
    """
    # FinMind 只用來取「上市/上櫃分類 + 股名」; 價格本來就是 yfinance 抓的。
    # 修正: 以前 FinMind 失敗 (常見 402 額度爆) 就整個 return 空 → 台股題材/開盤分析全白,
    #       明明 yfinance 拿得到價。改成 graceful degrade: 沒清單就照樣用 yfinance 直抓
    #       (兩個後綴都試), 只是少了股名/分類。
    info = None
    try:
        info = ds.get_taiwan_stock_info()
    except Exception as e:
        print(f"[sector_pulse] get_taiwan_stock_info 失敗 ({e}) → 改用 yfinance 直抓題材股", flush=True)
    if info is not None and not info.empty:
        market_map = info.set_index("stock_id")["type"].to_dict()
        name_map = (info.set_index("stock_id")["stock_name"].to_dict()
                    if "stock_name" in info.columns else {})
    else:
        print("[sector_pulse] 台股清單空 (FinMind 額度爆/失效?) → 題材分析改用 yfinance 直抓 (無股名)",
              flush=True)
        market_map = {}
        name_map = {}

    # 集合所有題材股，去重後一次抓 quote
    # 注意: market_map 空時「不可」用 `sid in market_map` 過濾, 否則會濾掉全部 → 空結果。
    if market_map:
        all_ids = sorted({sid for ids in TW_THEMES.values() for sid in ids if sid in market_map})
    else:
        all_ids = sorted({sid for ids in TW_THEMES.values() for sid in ids})
    quotes = fetch_intraday_metrics(all_ids, market_map)
    if quotes.empty:
        return {"themes": pd.DataFrame(), "leaders": {}}
    quotes["stock_name"] = quotes["stock_id"].map(name_map).fillna("")

    rows = []
    leaders_unfiltered: Dict[str, pd.DataFrame] = {}
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
        leaders_unfiltered[theme] = sub.sort_values("今日%", ascending=False).reset_index(drop=True)

    themes = pd.DataFrame(rows).sort_values(["平均%", "上漲比率%"], ascending=False).reset_index(drop=True)

    # 去重：每檔股票只放在排名最高的題材底下
    leaders: Dict[str, pd.DataFrame] = {}
    seen_stocks = set()
    for theme in themes["題材"].tolist():
        df_unf = leaders_unfiltered.get(theme)
        if df_unf is None or df_unf.empty:
            leaders[theme] = pd.DataFrame()
            continue
        df_dedup = df_unf[~df_unf["stock_id"].isin(seen_stocks)].head(5).reset_index(drop=True)
        leaders[theme] = df_dedup
        seen_stocks.update(df_dedup["stock_id"].tolist())

    # 補催化劑（用一次 Gemini 批次處理所有 leaders）
    try:
        import stock_catalyst
        all_records = []
        for theme, df in leaders.items():
            if df is not None and not df.empty:
                for _, r in df.iterrows():
                    rec = r.to_dict()
                    rec["_theme"] = theme
                    all_records.append(rec)
        if all_records:
            cat_map = stock_catalyst.annotate_picks_with_catalysts(all_records, market="TW")
            for theme, df in leaders.items():
                if df is None or df.empty:
                    continue
                df["催化劑"] = df["stock_id"].astype(str).map(cat_map).fillna("")
                leaders[theme] = df
    except Exception:
        pass

    # C: 對 themes leaders 加 入場標籤
    try:
        import entry_label_helper as _el
        all_syms = set()
        for theme, df in leaders.items():
            if df is not None and not df.empty:
                all_syms.update(df["stock_id"].astype(str).tolist())
        if all_syms:
            pairs = [(s, "TW") for s in all_syms]
            eval_map = _el.batch_evaluate(pairs, max_workers=8)
            for theme, df in leaders.items():
                if df is None or df.empty:
                    continue
                df["入場標籤"] = df["stock_id"].astype(str).map(
                    lambda s: ((eval_map.get(s) or {}).get("entry_emoji", "") + " " +
                               (eval_map.get(s) or {}).get("entry_label", "—")).strip()
                )
                df["入場分數"] = df["stock_id"].astype(str).map(
                    lambda s: (eval_map.get(s) or {}).get("entry_score")
                )
                leaders[theme] = df
    except Exception as _e:
        print(f"[sector_pulse] hot_themes entry_label failed: {_e}", flush=True)

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
            stock_ids = TW_THEMES.get(theme, [])
            if not stock_ids:
                continue
            try:
                info = ds.get_taiwan_stock_info()
            except Exception:
                info = None
            if info is None or info.empty:
                continue
            market_map = info.set_index("stock_id")["type"].to_dict()
            name_map = info.set_index("stock_id")["stock_name"].to_dict() \
                if "stock_name" in info.columns else {}
            quotes = fetch_intraday_metrics(stock_ids, market_map)
            if quotes.empty:
                continue
            quotes["stock_name"] = quotes["stock_id"].map(name_map).fillna("")
            quotes["題材"] = theme
            df = quotes
        df = df.copy()
        df["題材"] = theme
        pool.append(df)

    if not pool:
        return {"stealth": pd.DataFrame(), "hot_themes": themes_df.head(top_themes)}

    all_df = pd.concat(pool, ignore_index=True)
    # 條件: 今日 ≤ +2% + 量比 ≥ 1.5 + 5 日 ≤ +5% (潛伏: 量先動、股還沒動)
    cond = (
        (all_df["今日%"].fillna(0) <= 2)
        & (all_df["量比"].fillna(0) >= 1.5)
        & (all_df["5日%"].fillna(0) <= 5)
    )
    stealth = all_df[cond].sort_values("量比", ascending=False).head(15).reset_index(drop=True)
    return {"stealth": stealth, "hot_themes": themes_df.head(top_themes)}
