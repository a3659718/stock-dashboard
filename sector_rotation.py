"""sector_rotation.py — 板塊輪動 4 象限."""
from __future__ import annotations
from typing import Dict
from functools import lru_cache
import data_sources as ds

try:
    import streamlit as _st
    _CACHE = _st.cache_data(ttl=600, show_spinner=False)
except Exception:
    def _CACHE(fn):
        return lru_cache(maxsize=32)(fn)


US_SECTOR_ETF = {
    "XLK": "Technology 科技", "XLF": "Financials 金融", "XLV": "Healthcare 醫療",
    "XLY": "Consumer Cyclical 非必需", "XLP": "Consumer Defensive 必需",
    "XLE": "Energy 能源", "XLI": "Industrials 工業", "XLB": "Materials 原物料",
    "XLRE": "Real Estate 房地產", "XLU": "Utilities 公用", "XLC": "Communication 通訊",
}

TW_SECTOR_ETF = {
    "0050": "台灣 50", "0056": "高股息", "00878": "ESG 高股息",
    "00919": "金融優選", "00929": "復華台灣科技優息",
    "00940": "元大臺灣價值高息", "0055": "金融",
    "00692": "富邦公司治理", "00733": "富邦中証傳產",
}


@_CACHE
def _fetch_etf_history(symbol: str):
    try:
        df = ds.fetch_yf_history(symbol, period="30d", interval="1d")
        if df is None or df.empty:
            return None
        return df
    except Exception:
        return None


def _classify_quadrant(this_w, last_w, median_this, median_last):
    above_this = this_w >= median_this
    above_last = last_w >= median_last
    if above_this and above_last: return "leading"
    if above_this and not above_last: return "improving"
    if not above_this and above_last: return "weakening"
    return "lagging"


def compute_sector_rotation(market: str = "US") -> Dict:
    import datetime as dt
    sectors = US_SECTOR_ETF if market == "US" else TW_SECTOR_ETF
    matrix = []
    for etf, name in sectors.items():
        df = _fetch_etf_history(etf)
        this_w = last_w = None
        if df is not None and len(df) >= 11:
            try:
                c = df["Close"].astype(float).reset_index(drop=True)
                this_w = round((float(c.iloc[-1]) / float(c.iloc[-6]) - 1) * 100, 2)
                last_w = round((float(c.iloc[-6]) / float(c.iloc[-11]) - 1) * 100, 2)
            except Exception:
                pass
        matrix.append({
            "etf": etf, "name": name,
            "this_week_pct": this_w, "last_week_pct": last_w,
            "delta": (this_w - last_w) if (this_w is not None and last_w is not None) else None,
        })

    valid = [m for m in matrix if m["this_week_pct"] is not None and m["last_week_pct"] is not None]
    if not valid:
        return {"ts": dt.datetime.now().isoformat(), "market": market, "matrix": matrix,
                "quadrants": {"leading": [], "improving": [], "weakening": [], "lagging": []},
                "err": "無有效 ETF 資料"}

    this_vals = sorted([v["this_week_pct"] for v in valid])
    last_vals = sorted([v["last_week_pct"] for v in valid])
    median_this = this_vals[len(this_vals) // 2]
    median_last = last_vals[len(last_vals) // 2]

    quadrants = {"leading": [], "improving": [], "weakening": [], "lagging": []}
    for v in valid:
        q = _classify_quadrant(v["this_week_pct"], v["last_week_pct"], median_this, median_last)
        v["quadrant"] = q
        quadrants[q].append(v)
    for k in quadrants:
        quadrants[k].sort(key=lambda x: x["this_week_pct"], reverse=True)

    return {
        "ts": dt.datetime.now().isoformat(), "market": market,
        "matrix": sorted(matrix, key=lambda x: x.get("this_week_pct") or -999, reverse=True),
        "quadrants": quadrants,
        "median_this": median_this, "median_last": median_last,
    }


QUADRANT_LABELS = {
    "leading":   ("\U0001F7E2", "持續強勢 (Leading)",   "持續強, 仍有動能"),
    "improving": ("\U0001F7E1", "新興強勢 (Improving)", "剛輪入, 籌碼進場中"),
    "weakening": ("\U0001F535", "漲多回測 (Weakening)", "漲多回測, 籌碼鬆動"),
    "lagging":   ("\U0001F534", "持續弱勢 (Lagging)",   "避開, 籌碼持續流出"),
}
