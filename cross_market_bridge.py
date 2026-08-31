"""
cross_market_bridge.py
跨市場 narrative 連動評分 — 美股板塊強勢 → 對應台股族群 / 個股加分.

設計理念:
  美股科技 (XLK) 大漲 → 隔日台積電 / 聯發科 / 廣達等 AI 族群會跟漲
  美股核電 (CEG/VST/OKLO) 強 → 台股核電概念 (中興電 1513 / 華城 1519) 受惠
  美股加密 (COIN/MSTR) 強 → 台股加密相關 (1903 士紙 / 沒太多直接對應)

mapping 邏輯:
  - 抓 US 11 個 SPDR sector ETF + 重點主題股 (OKLO/SMR/COIN/MSTR) 的近 1d / 5d 表現
  - 對應到 TW 族群 (sector_pulse.TW_THEMES) 與個股
  - 強勢板塊 → 該族群 / 個股的 boost score (0-20)

對外接口:
    get_us_sector_strength() -> Dict
        {"XLK": {"1d_pct": 1.5, "5d_pct": 3.2}, ...}

    tw_theme_boost_from_us(theme: str) -> Dict
        {"boost_score": 15, "drivers": ["XLK +1.5%", "AI narrative hot"], "strength": "strong"}

    tw_stock_boost_from_us(stock_id: str) -> Dict
        # 從個股對應到 TW theme, 再轉成 boost
"""
from __future__ import annotations

import re as _re
from functools import lru_cache as _lru_cache
from typing import Dict, List, Optional

import data_sources as ds

try:
    import streamlit as st  # type: ignore
except Exception:
    class _NoOp:
        def cache_data(self, *args, **kwargs):
            def deco(f):
                return f
            if len(args) == 1 and callable(args[0]):
                return args[0]
            return deco
    st = _NoOp()  # type: ignore


# --- 美股板塊 / 主題股 → 台股族群對應 ---
# 美股強勢時, 隔日台股對應族群 / 個股有跟漲傾向
US_SECTOR_TO_TW_THEME = {
    # 科技 / AI / 半導體
    "XLK": ["AI 伺服器", "AI 邊緣", "AI PC", "ABF 載板", "PCB", "高頻高速",
            "矽光子", "散熱", "被動元件", "面板"],
    # 通訊
    "XLC": ["AI 伺服器", "低軌衛星"],
    # 非必需消費
    "XLY": ["航運", "汽車零件", "電動車"],
    # 工業 / 國防 / 太空 (RKLB / ASTS 拉動的)
    "XLI": ["重電族群", "汽車零件", "機器人", "低軌衛星"],
    # 金融
    "XLF": ["金融"],
    # 醫療
    "XLV": ["生技"],
    # 能源
    "XLE": ["塑化石化"],
    # 公用事業 (核電題材都在這)
    "XLU": ["重電族群", "儲能"],
    # 必需消費 (台股對應較弱)
    "XLP": [],
    # 房地產
    "XLRE": [],
    # 原物料
    "XLB": ["塑化石化", "鋼鐵"],
}

# 美股主題股 → 台股對應 (個股級別, 比 sector ETF 更精準)
US_THEME_STOCK_TO_TW = {
    # 核電
    "OKLO": ["中興電 (1513)", "華城 (1519)", "亞力 (1514)"],
    "SMR":  ["中興電 (1513)", "華城 (1519)"],
    "CEG":  ["中興電 (1513)", "華城 (1519)", "士電 (1503)"],
    "VST":  ["中興電 (1513)", "華城 (1519)"],
    # AI 半導體
    "NVDA": ["台積電 (2330)", "聯發科 (2454)", "廣達 (2382)", "緯創 (3231)", "技嘉 (2376)"],
    "TSM":  ["台積電 (2330)"],
    "AVGO": ["台積電 (2330)", "聯發科 (2454)"],
    "SMCI": ["緯穎 (6669)", "緯創 (3231)", "技嘉 (2376)", "鴻海 (2317)"],
    # 機器人 / 自動化
    "TSLA": ["和大 (1536)", "東陽 (1319)", "達明 (4585)"],
    # 太空 / 國防
    "RKLB": ["昇達科 (3491)", "啟碁 (6285)", "公準 (3178)"],
    "ASTS": ["昇達科 (3491)", "啟碁 (6285)"],
    # 加密 / 高 beta
    "COIN": [],  # 台股無直接對應
    "MSTR": [],
}


@st.cache_data(ttl=600, show_spinner=False)
def get_us_sector_strength() -> Dict[str, Dict]:
    """抓 US 11 個 sector ETF + 重要主題股的 1d / 5d 表現.

    回傳 {symbol: {"1d_pct": float, "5d_pct": float, "rank": int (in sector ETFs)}}
    用於 caller 判斷哪些 US 強勢可以拉動台股.
    """
    out: Dict[str, Dict] = {}
    # 1. SPDR sector ETF
    sectors_df = ds.fetch_sector_rotation()
    if sectors_df is not None and not sectors_df.empty:
        sorted_df = sectors_df.sort_values("1d_%", ascending=False).reset_index(drop=True)
        for i, row in sorted_df.iterrows():
            out[row["symbol"]] = {
                "1d_pct": float(row.get("1d_%") or 0),
                "5d_pct": float(row.get("5d_%") or 0),
                "rank": i + 1,
                "name": row.get("sector", ""),
            }
    # 2. 重要主題股 (yfinance fetch 1d/5d)
    theme_stocks = ["OKLO", "SMR", "CEG", "VST", "NVDA", "TSM", "AVGO", "SMCI",
                    "TSLA", "RKLB", "ASTS", "COIN", "MSTR"]
    for sym in theme_stocks:
        try:
            df = ds.fetch_yf_history(sym, period="10d", interval="1d")
            if df is None or df.empty or len(df) < 6:
                continue
            close = df["Close"].astype(float)
            pct_1d = (float(close.iloc[-1]) / float(close.iloc[-2]) - 1) * 100 if len(close) >= 2 else 0
            pct_5d = (float(close.iloc[-1]) / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0
            out[sym] = {"1d_pct": round(pct_1d, 2), "5d_pct": round(pct_5d, 2)}
        except Exception:
            continue
    return out


@_lru_cache(maxsize=1)
def _us_stock_to_tw_themes() -> Dict[str, set]:
    """{美股代號: {它連動的台股族群名, ...}}

    US_THEME_STOCK_TO_TW 的值是 "中興電 (1513)" 這種顯示字串, 這裡把括號裡的 4 碼代號
    解出來, 再透過 STOCK_TO_TW_THEME (台股代號 → 族群) 反查族群名。
    """
    out: Dict[str, set] = {}
    for us, tw_list in US_THEME_STOCK_TO_TW.items():
        themes = set()
        for tw in tw_list:
            m = _re.search(r"(\d{4})", str(tw))
            if not m:
                continue
            th = STOCK_TO_TW_THEME.get(m.group(1))
            if th:
                themes.add(th)
        if themes:
            out[us] = themes
    return out


def tw_theme_boost_from_us(theme: str, us_strength: Optional[Dict] = None) -> Dict:
    """給定 TW 族群名稱, 計算來自美股的 boost score (0-20).

    邏輯:
      1. 找出所有對應到此 theme 的 US sector ETF + 主題股
      2. 看哪些 US 標的 1d 漲幅 ≥ 1%, 加 base 5 分; ≥ 2% +5; ≥ 3% +5
      3. 5d 漲幅 ≥ 5% 額外 +3, ≥ 10% +5
      4. 最高 cap 20 分

    回傳 {boost_score, drivers, strength}
    """
    us_strength = us_strength if us_strength is not None else get_us_sector_strength()
    drivers = []
    score = 0

    # 找對應的 US ETF
    matched_etfs = [etf for etf, themes in US_SECTOR_TO_TW_THEME.items()
                    if theme in themes]
    for etf in matched_etfs:
        info = us_strength.get(etf, {})
        p1 = info.get("1d_pct", 0)
        p5 = info.get("5d_pct", 0)
        if p1 >= 3:
            score += 10; drivers.append(f"{etf} +{p1:.1f}% (強)")
        elif p1 >= 2:
            score += 7; drivers.append(f"{etf} +{p1:.1f}%")
        elif p1 >= 1:
            score += 4; drivers.append(f"{etf} +{p1:.1f}%")
        if p5 >= 10:
            score += 4
        elif p5 >= 5:
            score += 2

    # 找對應的主題股
    # Bug fix (2026-08): 原本拿 theme (族群名, 例如 "重電族群") 去跟 tw_list 的元素
    # (個股字串, 例如 "中興電 (1513)") 互相做子字串比對 —— 兩者永遠不是對方的子字串,
    # 實測 21 個 theme 的 matched_stocks 全部是空 list, 整個「個股級別連動」從未生效。
    # 正確作法: 從個股字串裡把 4 碼代號解出來, 再用 STOCK_TO_TW_THEME 反查它屬於哪個族群。
    matched_stocks = [us for us, themes in _us_stock_to_tw_themes().items() if theme in themes]
    for sym in matched_stocks:
        info = us_strength.get(sym, {})
        p1 = info.get("1d_pct", 0)
        if p1 >= 5:
            score += 6; drivers.append(f"{sym} +{p1:.1f}% (主題股強拉)")
        elif p1 >= 2:
            score += 3; drivers.append(f"{sym} +{p1:.1f}%")

    score = min(score, 20)
    if score >= 15:
        strength = "strong"
    elif score >= 8:
        strength = "moderate"
    elif score >= 3:
        strength = "weak"
    else:
        strength = "none"
    return {
        "boost_score": score,
        "drivers": drivers[:4],
        "strength": strength,
        "matched_us_etfs": matched_etfs,
        "matched_us_stocks": matched_stocks,
    }


# TW 股票 → 族群 mapping (從 sector_pulse 借用, 簡化版)
# 若 sector_pulse 有更完整版可動態查; 這裡放常見對應給 fallback
STOCK_TO_TW_THEME = {
    # AI 伺服器
    "2330": "AI 伺服器", "2454": "AI 伺服器", "2382": "AI 伺服器",
    "3231": "AI 伺服器", "2376": "AI 伺服器", "6669": "AI 伺服器",
    "2317": "AI 伺服器", "3017": "散熱", "2308": "AI 伺服器",
    # 核電 / 重電
    "1513": "重電族群", "1519": "重電族群", "1514": "重電族群",
    "1503": "重電族群", "1504": "重電族群",
    # 太空 / 衛星
    "3491": "低軌衛星", "6285": "低軌衛星", "3178": "低軌衛星",
    # 機器人 / 自動化
    "1536": "機器人", "1319": "機器人", "4585": "機器人",
    # ABF 載板
    "3037": "ABF 載板", "8046": "ABF 載板",
    # 6669 (緯穎) 原本在這裡重複定義, 會靜默覆蓋上面的 "AI 伺服器" 分類 — 移除。
    # 金融
    "2891": "金融", "2882": "金融", "2881": "金融",
}


def tw_stock_boost_from_us(stock_id: str, us_strength: Optional[Dict] = None) -> Dict:
    """給定 TW stock_id, 透過 STOCK_TO_TW_THEME 找族群再算 boost."""
    theme = STOCK_TO_TW_THEME.get(str(stock_id))
    if not theme:
        return {"boost_score": 0, "drivers": [], "strength": "none", "theme": None}
    result = tw_theme_boost_from_us(theme, us_strength)
    result["theme"] = theme
    return result
