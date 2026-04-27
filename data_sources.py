"""
data_sources.py
整合所有資料來源：
  - FinMind  : 台股日線、法人、融資融券 (直接呼叫 REST v4 API，不依賴 SDK)
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
    import yfinance as yf
except Exception:  # pragma: no cover
    yf = None  # type: ignore


# ---------------------------------------------------------------------------
# Secrets 讀取
# ---------------------------------------------------------------------------
def _secret(key: str, default: str = "") -> str:
    """安全的讀 Streamlit secrets / 環境變數。"""
    # 1) Streamlit secrets (dict-like 存取)
    try:
        if key in st.secrets:
            v = st.secrets[key]
            if v is not None:
                return str(v)
    except Exception:
        pass
    # 2) Streamlit secrets .get
    try:
        v = st.secrets.get(key, None)  # type: ignore[attr-defined]
        if v:
            return str(v)
    except Exception:
        pass
    # 3) 環境變數
    import os
    return os.environ.get(key, default) or default


def list_secret_keys() -> list:
    """偵錯用：列出 st.secrets 看得到哪些 key。"""
    try:
        return list(st.secrets.keys())  # type: ignore[attr-defined]
    except Exception:
        return []


def get_finmind_token() -> str:
    return _secret("FINMIND_TOKEN")


def finmind_available() -> bool:
    """Token 是否有設好 (有 token 就算可用，不依賴 SDK)."""
    return bool(get_finmind_token())


# ---------------------------------------------------------------------------
# FinMind v4 REST 客戶端 (取代 SDK)
# ---------------------------------------------------------------------------
FINMIND_API = "https://api.finmindtrade.com/api/v4/data"


def _finmind_get(dataset: str, **params) -> pd.DataFrame:
    """通用 FinMind v4 抓取，自動帶 token，回傳 DataFrame。"""
    token = get_finmind_token()
    if not token:
        raise RuntimeError("尚未設定 FINMIND_TOKEN，請到 Streamlit Secrets 加入。")
    q = {"dataset": dataset, "token": token}
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    try:
        r = requests.get(FINMIND_API, params=q, timeout=30)
    except Exception as e:
        raise RuntimeError(f"FinMind 連線錯誤: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"FinMind HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    if j.get("status") not in (200, "200"):
        raise RuntimeError(f"FinMind API 回應錯誤: {j.get('msg', j)}")
    data = j.get("data") or []
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# 台股清單
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def get_taiwan_stock_info() -> pd.DataFrame:
    """全台股清單 (含 twse / tpex / 排除 ETF/權證/全額)."""
    df = _finmind_get("TaiwanStockInfo")
    if df.empty:
        return df
    df = df[df["stock_id"].astype(str).str.fullmatch(r"\d{4}")]
    df = df[~df["stock_id"].str.startswith("00")]
    return df.reset_index(drop=True)


def list_universe(market: str = "all") -> List[str]:
    info = get_taiwan_stock_info()
    if market == "twse":
        sub = info[info["type"] == "twse"]
    elif market == "tpex":
        sub = info[info["type"] == "tpex"]
    else:
        sub = info[info["type"].isin(["twse", "tpex"])]
    return sub["stock_id"].tolist()


# ---------------------------------------------------------------------------
# 通用：per-stock 平行抓取
# ---------------------------------------------------------------------------
def _finmind_get_one(dataset: str, stock_id: str, start_date: str, end_date: str) -> pd.DataFrame:
    """單檔抓取，失敗時靜默回空 DataFrame (避免少數股票拖累整批)."""
    try:
        return _finmind_get(
            dataset, data_id=stock_id, start_date=start_date, end_date=end_date
        )
    except Exception:
        return pd.DataFrame()


def _fetch_universe(
    dataset: str, stock_ids: List[str], start_date: str, end_date: str,
    max_workers: int = 5, progress_cb=None,
) -> pd.DataFrame:
    """對一組 stock_ids 平行抓 dataset，回傳合併後的 DataFrame。
    progress_cb(done, total) 可選的進度回呼 (給 streamlit 用)。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    rows: List[pd.DataFrame] = []
    total = len(stock_ids)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_finmind_get_one, dataset, sid, start_date, end_date): sid
                for sid in stock_ids}
        for fut in as_completed(futs):
            df = fut.result()
            if df is not None and not df.empty:
                rows.append(df)
            done += 1
            if progress_cb:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"])
    return out


# ---------------------------------------------------------------------------
# 台股日線 (universe 版)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_tw_universe_daily(stock_ids_tuple: tuple, start_date: str, end_date: str) -> pd.DataFrame:
    """注意 cache 不接受 list，所以參數用 tuple。回傳全部已合併的日線。"""
    df = _fetch_universe("TaiwanStockPrice", list(stock_ids_tuple), start_date, end_date)
    if df.empty:
        return df
    if "max" in df.columns and "high" not in df.columns:
        df = df.rename(columns={"max": "high", "min": "low"})
    df = df.sort_values(["stock_id", "date"]).reset_index(drop=True)
    return df


@st.cache_data(ttl=900, show_spinner=False)
def fetch_tw_stock_daily_one(stock_id: str, days: int = 120) -> pd.DataFrame:
    end_date = dt.date.today().strftime("%Y-%m-%d")
    start_date = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    df = _finmind_get_one("TaiwanStockPrice", stock_id, start_date, end_date)
    if df is None or df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 投信法人 (universe 版)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_institutional_universe(stock_ids_tuple: tuple, start_date: str, end_date: str) -> pd.DataFrame:
    df = _fetch_universe(
        "TaiwanStockInstitutionalInvestorsBuySell",
        list(stock_ids_tuple), start_date, end_date,
    )
    if df.empty:
        return df
    return df.sort_values(["stock_id", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 融資融券 (universe 版)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_margin_universe(stock_ids_tuple: tuple, start_date: str, end_date: str) -> pd.DataFrame:
    df = _fetch_universe(
        "TaiwanStockMarginPurchaseShortSale",
        list(stock_ids_tuple), start_date, end_date,
    )
    if df.empty:
        return df
    return df.sort_values(["stock_id", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 即時 Quote (yfinance 對 TW stocks)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_yf_quote(symbol: str) -> Dict:
    if yf is None:
        return {}
    try:
        t = yf.Ticker(symbol)
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
# US Sector ETFs
# ---------------------------------------------------------------------------
SECTOR_ETFS = {
    "XLK": "科技", "XLE": "能源", "XLF": "金融", "XLV": "醫療",
    "XLY": "非必需消費", "XLP": "必需消費", "XLI": "工業",
    "XLB": "原材料", "XLU": "公用事業", "XLRE": "房地產", "XLC": "通訊服務",
}


@st.cache_data(ttl=600, show_spinner=False)
def fetch_sector_rotation() -> pd.DataFrame:
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
            rows.append({"symbol": sym, "sector": name, "1d_%": r1,
                         "5d_%": r5, "20d_%": r20, "last": float(last)})
        except Exception:
            continue
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("5d_%", ascending=False, na_position="last").reset_index(drop=True)
    return out


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_yahoo_news(symbol: str, max_n: int = 6) -> List[Dict]:
    if yf is None:
        return []
    try:
        items = yf.Ticker(symbol).news or []
    except Exception:
        items = []
    out = []
    for it in items[:max_n]:
        out.append({
            "title": it.get("title"),
            "publisher": it.get("publisher"),
            "link": it.get("link"),
            "providerPublishTime": it.get("providerPublishTime"),
            "relatedTickers": it.get("relatedTickers", []),
        })
    return out


@st.cache_data(ttl=900, show_spinner=False)
def fetch_market_news_themes() -> List[Dict]:
    items: List[Dict] = []
    seen = set()
    for sym in ["SPY", "QQQ", "DIA", "IWM"]:
        for it in fetch_yahoo_news(sym, max_n=8):
            key = it.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(it)
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
