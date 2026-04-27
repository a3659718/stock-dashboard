"""
data_sources.py
整合所有資料來源：
  - FinMind  : 台股日線、法人、融資融券
  - yfinance : 美股、台股即時 quote
  - CNN F&G  : Fear & Greed Index
  - Yahoo News / Finnhub : 美股新聞題材

所有 fetcher 都用 streamlit.cache_data 包起來，避免在頁面互動時重複呼叫 API。
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Dict, List, Optional

import pandas as pd
import requests
import streamlit as st

try:
    from FinMind.data import DataLoader
except Exception:  # pragma: no cover
    DataLoader = None  # type: ignore

try:
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None  # type: ignore


# ---------------------------------------------------------------------------
# Secrets 讀取
# ---------------------------------------------------------------------------
def _secret(key: str, default: str = "") -> str:
    """安全的讀 Streamlit secrets / 環境變數。"""
    try:
        return st.secrets.get(key, default)  # type: ignore[attr-defined]
    except Exception:
        import os
        return os.environ.get(key, default)


def get_finmind_token() -> str:
    return _secret("FINMIND_TOKEN")


# ---------------------------------------------------------------------------
# FinMind helpers
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_finmind_api():
    """建立並快取 FinMind DataLoader 實例。"""
    if DataLoader is None:
        raise RuntimeError("FinMind 套件未安裝")
    api = DataLoader()
    token = get_finmind_token()
    if token:
        try:
            api.login_by_token(api_token=token)
        except Exception as e:
            st.warning(f"FinMind token 登入失敗：{e}")
    return api


@st.cache_data(ttl=3600, show_spinner=False)
def get_taiwan_stock_info() -> pd.DataFrame:
    """全台股清單 (含 twse / tpex / 排除 ETF/權證/全額)."""
    api = get_finmind_api()
    df = api.taiwan_stock_info()
    # 只保留 4 碼純數字、非 00 開頭(排除 ETF/期貨)
    df = df[df["stock_id"].astype(str).str.fullmatch(r"\d{4}")]
    df = df[~df["stock_id"].str.startswith("00")]
    return df.reset_index(drop=True)


def list_universe(market: str = "all") -> List[str]:
    """回傳要掃描的股票清單。market: 'twse' | 'tpex' | 'all'."""
    info = get_taiwan_stock_info()
    if market == "twse":
        sub = info[info["type"] == "twse"]
    elif market == "tpex":
        sub = info[info["type"] == "tpex"]
    else:
        sub = info[info["type"].isin(["twse", "tpex"])]
    return sub["stock_id"].tolist()


# ---------------------------------------------------------------------------
# 台股日線 / 量價 (一次抓全市場較有效率)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_tw_market_daily(start_date: str, end_date: str) -> pd.DataFrame:
    """
    用 FinMind 的 dataset 一次撈全市場日線。
    回傳欄位: date, stock_id, open, high, low, close, Trading_Volume, ...
    """
    api = get_finmind_api()
    df = api.taiwan_stock_daily(stock_id="", start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["stock_id", "date"]).reset_index(drop=True)
    return df


@st.cache_data(ttl=900, show_spinner=False)
def fetch_tw_stock_daily_one(stock_id: str, days: int = 120) -> pd.DataFrame:
    """單檔日線。"""
    api = get_finmind_api()
    end_date = dt.date.today().strftime("%Y-%m-%d")
    start_date = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    df = api.taiwan_stock_daily(stock_id=stock_id, start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 法人 (投信買賣超)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_institutional_market(start_date: str, end_date: str) -> pd.DataFrame:
    """全市場法人資料 (含投信)."""
    api = get_finmind_api()
    df = api.taiwan_stock_institutional_investors(stock_id="", start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df


# ---------------------------------------------------------------------------
# 融資融券
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_margin_short_market(start_date: str, end_date: str) -> pd.DataFrame:
    """全市場融資融券資料."""
    api = get_finmind_api()
    df = api.taiwan_stock_margin_purchase_short_sale(
        stock_id="", start_date=start_date, end_date=end_date
    )
    if df is None or df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df


# ---------------------------------------------------------------------------
# 即時 Quote (yfinance 對 TW stocks)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_yf_quote(symbol: str) -> Dict:
    """以 yfinance 取得即時 quote.
    台股要加 .TW (上市) 或 .TWO (上櫃)."""
    if yf is None:
        return {}
    try:
        t = yf.Ticker(symbol)
        info = t.fast_info if hasattr(t, "fast_info") else {}
        hist = t.history(period="2d", interval="1d")
        last_close = float(hist["Close"].iloc[-1]) if not hist.empty else None
        prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
        change_pct = (
            (last_close - prev_close) / prev_close * 100
            if last_close and prev_close
            else None
        )
        return {
            "symbol": symbol,
            "last": last_close,
            "prev_close": prev_close,
            "change_pct": change_pct,
            "volume": int(hist["Volume"].iloc[-1]) if not hist.empty else None,
        }
    except Exception:
        return {}


@st.cache_data(ttl=120, show_spinner=False)
def fetch_yf_history(symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
    if yf is None:
        return pd.DataFrame()
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.reset_index()
        df.columns = [c if isinstance(c, str) else c[0] for c in df.columns]
        return df
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# CNN Fear & Greed Index
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def fetch_fear_greed() -> Dict:
    """抓 CNN Fear & Greed Index 公開 endpoint."""
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) Safari/605.1.15"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return {}
        j = r.json()
        fg = j.get("fear_and_greed", {})
        return {
            "score": fg.get("score"),
            "rating": fg.get("rating"),
            "previous_close": fg.get("previous_close"),
            "previous_1_week": fg.get("previous_1_week"),
            "previous_1_month": fg.get("previous_1_month"),
            "previous_1_year": fg.get("previous_1_year"),
            "timestamp": fg.get("timestamp"),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# US Sector ETFs (S&P SPDR)
# ---------------------------------------------------------------------------
SECTOR_ETFS = {
    "XLK": "科技",
    "XLE": "能源",
    "XLF": "金融",
    "XLV": "醫療",
    "XLY": "非必需消費",
    "XLP": "必需消費",
    "XLI": "工業",
    "XLB": "原材料",
    "XLU": "公用事業",
    "XLRE": "房地產",
    "XLC": "通訊服務",
}


@st.cache_data(ttl=600, show_spinner=False)
def fetch_sector_rotation() -> pd.DataFrame:
    """各 sector ETF 的近期表現 (1d / 5d / 20d %)."""
    if yf is None:
        return pd.DataFrame()
    rows = []
    for sym, name in SECTOR_ETFS.items():
        df = fetch_yf_history(sym, period="2mo", interval="1d")
        if df.empty:
            continue
        try:
            close = df["Close"]
            last = close.iloc[-1]
            r1 = (last / close.iloc[-2] - 1) * 100 if len(close) >= 2 else None
            r5 = (last / close.iloc[-6] - 1) * 100 if len(close) >= 6 else None
            r20 = (last / close.iloc[-21] - 1) * 100 if len(close) >= 21 else None
            rows.append(
                {"symbol": sym, "sector": name, "1d_%": r1, "5d_%": r5, "20d_%": r20, "last": float(last)}
            )
        except Exception:
            continue
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("5d_%", ascending=False, na_position="last").reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# News 題材 (Yahoo Finance + 可選 Finnhub)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_yahoo_news(symbol: str, max_n: int = 6) -> List[Dict]:
    """從 yfinance Ticker.news 抓近期新聞."""
    if yf is None:
        return []
    try:
        items = yf.Ticker(symbol).news or []
    except Exception:
        items = []
    out = []
    for it in items[:max_n]:
        out.append(
            {
                "title": it.get("title"),
                "publisher": it.get("publisher"),
                "link": it.get("link"),
                "providerPublishTime": it.get("providerPublishTime"),
                "relatedTickers": it.get("relatedTickers", []),
            }
        )
    return out


@st.cache_data(ttl=900, show_spinner=False)
def fetch_market_news_themes() -> List[Dict]:
    """抓 SPY/QQQ 近 24 小時新聞做為市場題材參考."""
    items: List[Dict] = []
    seen = set()
    for sym in ["SPY", "QQQ", "DIA", "IWM"]:
        for it in fetch_yahoo_news(sym, max_n=8):
            key = it.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(it)
    # 過濾近 24 小時
    cutoff = time.time() - 86400
    items = [x for x in items if (x.get("providerPublishTime") or 0) >= cutoff]
    return items


def get_finnhub_token() -> str:
    return _secret("FINNHUB_TOKEN")


@st.cache_data(ttl=900, show_spinner=False)
def fetch_finnhub_news(category: str = "general", max_n: int = 20) -> List[Dict]:
    token = get_finnhub_token()
    if not token:
        return []
    url = f"https://finnhub.io/api/v1/news?category={category}&token={token}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        return r.json()[:max_n]
    except Exception:
        return []
