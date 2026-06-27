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
# 本地 fallback 快取: TaiwanStockInfo 幾乎不變 (上市櫃清單),
# FinMind 402 額度爆掉時用最後一次成功結果, 避免整個 dashboard 崩潰.
import os as _os

_STOCK_INFO_CACHE_FILE = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), ".cache", "taiwan_stock_info.csv"
)


def _save_stock_info_cache(df: pd.DataFrame) -> None:
    """存成功抓到的台股清單供日後 fallback (CSV, 不依賴 pyarrow)."""
    try:
        _os.makedirs(_os.path.dirname(_STOCK_INFO_CACHE_FILE), exist_ok=True)
        df.to_csv(_STOCK_INFO_CACHE_FILE, index=False, encoding="utf-8")
    except Exception as e:
        print(f"[data_sources] 存台股清單快取失敗 (non-fatal): {e}", flush=True)


def _load_stock_info_cache() -> pd.DataFrame:
    """讀本地台股清單快取. 沒有就回空 df."""
    try:
        if _os.path.exists(_STOCK_INFO_CACHE_FILE):
            return pd.read_csv(_STOCK_INFO_CACHE_FILE, dtype={"stock_id": str})
    except Exception as e:
        print(f"[data_sources] 讀台股清單快取失敗 (non-fatal): {e}", flush=True)
    return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def _get_taiwan_stock_info_cached() -> pd.DataFrame:
    """內部: 抓 FinMind 台股清單並清洗. 失敗會 raise.

    重點: st.cache_data 預設「不快取拋出例外的呼叫」— 所以 FinMind 402 raise
    時不會被快取, 下次呼叫會自動重試 (額度恢復後立即生效). 只有成功結果
    才被快取 1 小時 + 存本地 fallback.
    """
    df = _finmind_get("TaiwanStockInfo")  # 失敗 raise → 不被快取
    if df.empty:
        return df
    df = df[df["stock_id"].astype(str).str.fullmatch(r"\d{4}")]
    df = df.reset_index(drop=True)
    _save_stock_info_cache(df)  # 存成功結果供日後 fallback
    return df


def get_taiwan_stock_info() -> pd.DataFrame:
    """全台股清單 (twse / tpex)。只做基本清洗 (限 4 碼數字),
    是否排除 ETF/權證/TDR/全額交割等交給 filter_tradeable_stocks 決定,
    不在這裡寫死, 避免 caller 想保留 ETF 時被靜默過濾.

    韌性: FinMind 失敗 (常見 HTTP 402 額度用盡) 時不 raise — 改用本地
    最後一次成功的快取, 找不到才回空 df. 確保單一 API 額度爆掉不會
    讓整個 dashboard 崩潰. caller 普遍已處理 empty df.

    MED fix: 成功路徑走 @cache_data (快取 1hr); 失敗路徑「不」被快取,
    所以額度恢復後下次呼叫立即重試, 不會卡 1 小時空結果.
    """
    try:
        return _get_taiwan_stock_info_cached()
    except Exception as e:
        print(
            f"[data_sources] get_taiwan_stock_info FinMind 失敗 ({e}), 改用本地快取",
            flush=True,
        )
        fallback = _load_stock_info_cache()
        if not fallback.empty:
            fallback = fallback[
                fallback["stock_id"].astype(str).str.fullmatch(r"\d{4}")
            ]
            return fallback.reset_index(drop=True)
        return pd.DataFrame()


def list_universe(market: str = "all") -> List[str]:
    info = get_taiwan_stock_info()
    if market == "twse":
        sub = info[info["type"] == "twse"]
    elif market == "tpex":
        sub = info[info["type"] == "tpex"]
    else:
        sub = info[info["type"].isin(["twse", "tpex"])]
    return sub["stock_id"].tolist()


def filter_tradeable_stocks(info: pd.DataFrame, exclude_etf: bool = True) -> pd.DataFrame:
    """過濾出可正常買賣的個股 — 排除 ETF/權證/低價/全額交割等。"""
    if info.empty:
        return info
    sub = info.copy()
    sub = sub[sub["stock_id"].astype(str).str.fullmatch(r"\d{4}")]
    if exclude_etf:
        sub = sub[~sub["stock_id"].str.startswith("00")]  # 00xx ETF
    # 名稱含特殊符號 (有些全額交割股會在名稱前後標 *)
    if "stock_name" in sub.columns:
        sub = sub[~sub["stock_name"].astype(str).str.contains(r"\*", na=False)]
        sub = sub[~sub["stock_name"].astype(str).str.contains("DR$", na=False)]  # 排除 TDR
    return sub.reset_index(drop=True)


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
# 股本 (取最新季報的普通股股本) — 算投本比用
# ---------------------------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def fetch_shares_outstanding(stock_ids_tuple: tuple) -> Dict[str, float]:
    """回傳 {stock_id: 流通股本(張)}; 1張=1000股, 面額10元."""
    end = dt.date.today().strftime("%Y-%m-%d")
    start = (dt.date.today() - dt.timedelta(days=400)).strftime("%Y-%m-%d")
    df = _fetch_universe("TaiwanStockBalanceSheet", list(stock_ids_tuple), start, end)
    if df.empty:
        return {}

    # 試找股本欄位 (FinMind 在不同期間欄名可能不同)
    type_col = None
    for c in ["type", "type_name", "Type"]:
        if c in df.columns:
            type_col = c
            break
    val_col = None
    for c in ["value", "Value"]:
        if c in df.columns:
            val_col = c
            break
    if type_col is None or val_col is None:
        return {}

    # 找「股本」(可能為 CommonStock / 普通股股本 / 4111 / 股本)
    keywords = ["CommonStock", "普通股股本", "股本"]
    sub = df[df[type_col].astype(str).str.contains("|".join(keywords), case=False, na=False)]
    if sub.empty:
        return {}
    # 每檔取最近一期
    sub = sub.sort_values(["stock_id", "date"]).groupby("stock_id").tail(1)
    out: Dict[str, float] = {}
    for _, row in sub.iterrows():
        try:
            value = float(row[val_col])  # 元
            shares = value / 10.0          # 股 (面額10元)
            lots = shares / 1000.0         # 張
            if lots > 0:
                out[str(row["stock_id"])] = lots
        except Exception:
            continue
    return out


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


# ===========================================================
# G9 fix: yfinance fetch in-memory cache
# ===========================================================
# 為什麼: 同一個 monitor cron tick (~1 min) 內, 多個 check function 對同 symbol
#         重複呼叫 yfinance:
#           ^TWII: reversal + crash + index_alerts = 3-4 calls
#           ^SOX:  同上 = 3-4 calls
#         GH Actions 共用 IP 易被 Yahoo rate-limit. 加 5min in-memory cache 後,
#         同 (symbol, period, interval) 在 cache 內只實打 1 次 yfinance.
# 注意:
#   - 跨 cron tick 沒效 (process restart, cache 清空) — 跨 tick 用 yfinance own cache
#   - streamlit `@st.cache_data(ttl=120)` 在 streamlit runtime 內優先 (cache 在那邊命中)
#   - 失敗 (empty) 也 cache 但 TTL 短 (30s), 避免一直 retry 觸發 rate limit
_YF_FETCH_CACHE: Dict = {}
_YF_CACHE_TTL_OK = 300       # 成功 fetch cache 5 分鐘
_YF_CACHE_TTL_EMPTY = 30     # 空結果 cache 30 秒 (避免持續打 yfinance)


@st.cache_data(ttl=120, show_spinner=False)
def fetch_yf_history(symbol: str, period: str = "6mo", interval: str = "1d",
                      max_retries: int = 3) -> pd.DataFrame:
    """yfinance 抓歷史資料, 失敗自動 retry + sleep (避開 Yahoo rate limit).

    G9 cache: 跨 streamlit / 跨呼叫者 共享 in-memory cache (5 min for OK, 30s for empty).
    """
    import time

    # === G9 cache hit check ===
    cache_key = (symbol, period, interval)
    now = time.time()
    cached = _YF_FETCH_CACHE.get(cache_key)
    if cached is not None:
        cached_ts, cached_df = cached
        is_empty = cached_df is None or (hasattr(cached_df, "empty") and cached_df.empty)
        ttl = _YF_CACHE_TTL_EMPTY if is_empty else _YF_CACHE_TTL_OK
        if now - cached_ts < ttl:
            # 回 copy 避免 caller 修改影響後續使用者
            return cached_df.copy() if (cached_df is not None and not is_empty) else pd.DataFrame()

    # === 沒命中 → 實際 fetch ===
    if yf is None:
        _YF_FETCH_CACHE[cache_key] = (now, pd.DataFrame())
        return pd.DataFrame()

    last_err = None
    result_df = pd.DataFrame()
    for attempt in range(max_retries):
        try:
            df = yf.download(symbol, period=period, interval=interval,
                              progress=False, auto_adjust=False)
            if df is None or df.empty:
                # 空資料但沒 throw → 直接回 (不算錯誤). cache 短 TTL 避免反覆打.
                result_df = pd.DataFrame()
                break
            df = df.reset_index()
            df.columns = [c if isinstance(c, str) else c[0] for c in df.columns]
            result_df = df
            break
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            # 只對 rate limit / 連線錯誤 retry
            if any(kw in err_str for kw in ["rate", "429", "timeout", "connection",
                                              "max retries", "ssl"]):
                if attempt < max_retries - 1:
                    # 指數 backoff: 2s, 5s, 10s
                    sleep_s = (attempt + 1) * 2 + (attempt * 3)
                    time.sleep(sleep_s)
                    continue
            # 非 rate limit 錯誤 → 立刻放棄
            break

    # === 寫 cache ===
    _YF_FETCH_CACHE[cache_key] = (now, result_df.copy() if not result_df.empty else result_df)
    return result_df


def _yf_cache_stats() -> Dict:
    """給 heartbeat / debug 用. 回 cache 大小 + 命中率 (粗略)."""
    return {
        "cache_entries": len(_YF_FETCH_CACHE),
        "ok_ttl_seconds": _YF_CACHE_TTL_OK,
        "empty_ttl_seconds": _YF_CACHE_TTL_EMPTY,
    }


def _yf_cache_clear() -> None:
    """清空 cache (給 testing / 強制 refresh 用)."""
    _YF_FETCH_CACHE.clear()


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
    """抓單檔 Yahoo Finance news.

    修正 (2026-05): yfinance 0.2.40+ 改回 {"content": {title, ...}} 巢狀格式.
    舊版是 flat {title, publisher, link, providerPublishTime, relatedTickers}.
    這裡同時相容兩種格式, 否則「候選個股近期新聞 / 題材」全空.
    """
    if yf is None:
        return []
    try:
        items = yf.Ticker(symbol).news or []
    except Exception:
        items = []
    out = []
    for it in items[:max_n]:
        if not isinstance(it, dict):
            continue
        content = it.get("content")
        if isinstance(content, dict):
            # 新版巢狀格式 (yfinance >= 0.2.40)
            title = content.get("title") or it.get("title")
            provider_obj = content.get("provider") or {}
            publisher = (provider_obj.get("displayName") if isinstance(provider_obj, dict)
                          else str(provider_obj)) or it.get("publisher", "")
            # link 在新版可能在 content.canonicalUrl 或 content.clickThroughUrl
            link = None
            for key in ("canonicalUrl", "clickThroughUrl"):
                v = content.get(key)
                if isinstance(v, dict):
                    link = v.get("url")
                    if link:
                        break
                elif isinstance(v, str):
                    link = v
                    break
            link = link or it.get("link")
            # 時間: pubDate / displayTime (ISO string) 或舊版 providerPublishTime (epoch)
            pub_time = (content.get("pubDate") or content.get("displayTime")
                          or it.get("providerPublishTime"))
            if isinstance(pub_time, str):
                try:
                    pub_time = int(dt.datetime.fromisoformat(
                        pub_time.replace("Z", "+00:00")).timestamp())
                except Exception:
                    pub_time = None
            related = (content.get("relatedTickers") or it.get("relatedTickers") or [])
        else:
            # 舊版平格式
            title = it.get("title")
            publisher = it.get("publisher", "")
            link = it.get("link")
            pub_time = it.get("providerPublishTime")
            related = it.get("relatedTickers", [])
        if not title:
            continue
        out.append({
            "title": title,
            "publisher": publisher,
            "link": link,
            "providerPublishTime": pub_time,
            "relatedTickers": related,
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
    # 雙名後備: 不同 workflow / secret 命名習慣不一 (FINNHUB_TOKEN vs FINNHUB_API_KEY)。
    # 之前 market_open_alert.yml 只傳 FINNHUB_API_KEY 但這裡只讀 FINNHUB_TOKEN → Actions 上
    # token 永遠空, 專家訊號 / 8-K 急報 / IPO / 川普新聞全部靜默失效。改成兩個名都認。
    return _secret("FINNHUB_TOKEN") or _secret("FINNHUB_API_KEY")


# ---------------------------------------------------------------------------
# 台股市場情緒指數 (TW-specific Fear & Greed)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def fetch_tw_market_pulse() -> Dict:
    """以加權指數 (^TWII) 合成 0-100 分台股情緒指數。

    四個子分數平均：
      1) 5 日動能 (近 5 日漲跌幅)
      2) 20 日動能 (近 20 日漲跌幅)
      3) 波動率反向 (高波動 = 恐慌)
      4) 距 60 日均線位置
    """
    twii = fetch_yf_history("^TWII", period="6mo", interval="1d")
    if twii.empty or len(twii) < 60:
        return {}
    try:
        close = twii["Close"].astype(float)
        last = float(close.iloc[-1])
        ret_5d = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0
        ret_20d = (last / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0


        daily_ret = close.pct_change().dropna()
        vol_20d = float(daily_ret.iloc[-20:].std() * 100) if len(daily_ret) >= 20 else 1.0

        ma60 = float(close.rolling(60).mean().iloc[-1])
        ma60_dist = (last - ma60) / ma60 * 100 if ma60 else 0

        # 0-100 子分數
        s_5d = max(0, min(100, 50 + ret_5d * 10))      # ±5% → 0/100
        s_20d = max(0, min(100, 50 + ret_20d * 5))     # ±10% → 0/100
        # 台股日波動正常約 0.6-1.0%，>2% 恐慌
        s_vol = max(0, min(100, 100 - (vol_20d - 0.6) * 50))
        s_ma = max(0, min(100, 50 + ma60_dist * 5))    # ±10% → 0/100

        score = (s_5d + s_20d + s_vol + s_ma) / 4

        if score <= 25:
            rating, rating_zh = "Extreme Fear", "極度恐慌"
        elif score <= 45:
            rating, rating_zh = "Fear", "恐慌"
        elif score <= 55:
            rating, rating_zh = "Neutral", "中性"
        elif score <= 75:
            rating, rating_zh = "Greed", "貪婪"
        else:
            rating, rating_zh = "Extreme Greed", "極度貪婪"

        return {
            "score": round(score, 1),
            "rating": rating,
            "rating_zh": rating_zh,
            "components": {
                "5日動能": round(s_5d, 1),
                "20日動能": round(s_20d, 1),
                "波動率": round(s_vol, 1),
                "MA60距離": round(s_ma, 1),
            },
            "raw": {
                "TWII": round(last, 2),
                "5日%": round(ret_5d, 2),
                "20日%": round(ret_20d, 2),
                "20日日波動率%": round(vol_20d, 2),
                "距 MA60 %": round(ma60_dist, 2),
            },
        }
    except Exception:
        return {}


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
