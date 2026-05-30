"""
fundamentals_us.py
美股個股基本面 helper — 用 yfinance 抓 PE / EPS / PEG / market cap.

API:
  - fetch_us_fundamentals(ticker) -> dict
      回 trailingPE, forwardPE, pegRatio, trailingEps, forwardEps,
         marketCap, dividendYield, sector, industry, beta

回傳的 dict 一定有所有 key (沒抓到的設 None), caller 用 .get() 即可.

FinMind 沒有美股 PE/EPS, 用 yfinance ticker.info 補. 1hr cache.
"""
from __future__ import annotations

import streamlit as st

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None  # type: ignore


_EMPTY = {
    "ticker": "",
    "trailingPE": None,
    "forwardPE": None,
    "pegRatio": None,
    "trailingEps": None,
    "forwardEps": None,
    "marketCap": None,
    "dividendYield": None,
    "beta": None,
    "sector": None,
    "industry": None,
    "longName": None,
    # 新增: 財報日 + 成長率
    "earningsDate": None,
    "earningsGrowth": None,   # EPS YoY (季)
    "revenueGrowth": None,    # 營收 YoY (季)
}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_us_fundamentals(ticker: str) -> dict:
    """抓美股個股 fundamentals. 失敗時回 _EMPTY (各欄位 None), 不 raise."""
    if not ticker or yf is None:
        out = dict(_EMPTY)
        out["ticker"] = ticker or ""
        return out
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
    except Exception:
        out = dict(_EMPTY)
        out["ticker"] = ticker
        return out

    def _g(*keys):
        """從 info 取第一個有效值 (M5 fix: 0 / '' / None 都視為無效, fallback 下一個 key).
        yfinance 對沒 EPS 的票常回 0 或 '', 視為有效會誤導 verdict + 顯示空字串.
        """
        for k in keys:
            v = info.get(k)
            if v is None:
                continue
            # 字串空白也算無效
            if isinstance(v, str) and not v.strip():
                continue
            # 數值 0 視為無效 (PE/EPS 0 沒意義)
            if isinstance(v, (int, float)) and v == 0:
                continue
            return v
        return None

    # 財報日: yfinance 在 "earningsDate" 給 list[Timestamp]; ticker.calendar 也有
    ed = info.get("earningsDate")
    earnings_date_str = None
    try:
        if ed:
            if isinstance(ed, list) and ed:
                earnings_date_str = str(ed[0])[:10]
            else:
                earnings_date_str = str(ed)[:10]
        else:
            cal = getattr(t, "calendar", None)
            if cal is not None and "Earnings Date" in cal:
                ev = cal["Earnings Date"]
                if isinstance(ev, list) and ev:
                    earnings_date_str = str(ev[0])[:10]
                elif ev:
                    earnings_date_str = str(ev)[:10]
    except Exception:
        pass

    return {
        "ticker":        ticker,
        "trailingPE":    _g("trailingPE"),
        "forwardPE":     _g("forwardPE"),
        "pegRatio":      _g("pegRatio", "trailingPegRatio"),
        "trailingEps":   _g("trailingEps", "epsTrailingTwelveMonths"),
        "forwardEps":    _g("forwardEps", "epsForward"),
        "marketCap":     _g("marketCap"),
        "dividendYield": _g("dividendYield"),
        "beta":          _g("beta"),
        "sector":        _g("sector"),
        "industry":      _g("industry"),
        "longName":      _g("longName", "shortName"),
        # 新增 (1)
        "earningsDate":   earnings_date_str,
        "earningsGrowth": _g("earningsQuarterlyGrowth", "earningsGrowth"),
        "revenueGrowth":  _g("revenueQuarterlyGrowth", "revenueGrowth"),
    }


def fmt_pe_label(pe) -> str:
    """把 PE 數字轉成「估值偏高/合理/便宜」標籤."""
    if pe is None or pe <= 0:
        return "—"
    if pe < 10:
        return f"{pe:.1f} (極低/可能價值陷阱)"
    if pe < 15:
        return f"{pe:.1f} (便宜)"
    if pe < 25:
        return f"{pe:.1f} (合理)"
    if pe < 40:
        return f"{pe:.1f} (略貴)"
    return f"{pe:.1f} (偏高)"


def fmt_marketcap(mc) -> str:
    """marketCap 數字轉成 $XB / $XM."""
    if mc is None or mc <= 0:
        return "—"
    if mc >= 1e12:
        return f"${mc/1e12:.2f}T"
    if mc >= 1e9:
        return f"${mc/1e9:.2f}B"
    if mc >= 1e6:
        return f"${mc/1e6:.0f}M"
    return f"${mc:.0f}"
