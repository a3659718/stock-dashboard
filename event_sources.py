"""
event_sources.py
擴充 news_event_alert 的事件來源:
  - fetch_finnhub_8k(symbol): 美股 8-K filings (Finnhub /stock/filings)
  - fetch_tw_major_announcements(): 台股重大訊息 (FinMind TaiwanStockNews)
  - fetch_us_press_releases(symbol): Finnhub press releases
  - Twitter: 評估後決定 skip (沒免費 API, scraper 易被擋)

API:
  fetch_finnhub_8k(symbol, days_back=3) -> List[Dict]
  fetch_tw_major_announcements(stock_id=None, days_back=2) -> List[Dict]
  fetch_us_press_releases(symbol, days_back=3) -> List[Dict]

回 dict schema (對齊 news_event_alert 期望):
  {"title": str, "link": str, "publisher": str, "date": str (YYYY-MM-DD), "type": "8K"/"PR"/"TW_MAJOR"}
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import requests

import data_sources as ds


# ---------------------------------------------------------------------------
# 美股 8-K filings (Finnhub)
# ---------------------------------------------------------------------------
def fetch_finnhub_8k(symbol: str, days_back: int = 3) -> List[Dict]:
    """抓最近 N 天的 8-K filings.
    8-K = 重大事件 (CEO 變動 / M&A / 法說 / 信用評等變動).
    比 Yahoo News 早 10-60 分鐘 (因為 8-K 是 SEC 直發).
    """
    token = ds.get_finnhub_token()
    if not token:
        return []
    today = dt.date.today()
    frm = (today - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
    to = today.strftime("%Y-%m-%d")
    url = "https://finnhub.io/api/v1/stock/filings"
    try:
        r = requests.get(url, params={
            "symbol": symbol,
            "from": frm,
            "to": to,
            "token": token,
        }, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json() or []
        out = []
        for item in data:
            form = (item.get("form") or "").upper()
            # 只要 8-K (重大事件); 也可加 10-Q (季報) / 10-K (年報) / S-1 (IPO)
            if form not in {"8-K", "8-K/A", "10-Q", "10-K", "S-1"}:
                continue
            out.append({
                "title": f"[{form}] {item.get('filedDate', '')} - {symbol}",
                "link": item.get("filerUrl") or item.get("reportUrl", ""),
                "publisher": "SEC EDGAR",
                "date": str(item.get("filedDate", ""))[:10],
                "type": form,
                "symbol": symbol,
            })
        return out
    except Exception as e:
        print(f"[event_sources] 8K fetch {symbol} 失敗: {e}", flush=True)
        return []


# ---------------------------------------------------------------------------
# 美股 Press Releases (Finnhub) — 公司主動發布的新聞 (比媒體報導早)
# ---------------------------------------------------------------------------
def fetch_us_press_releases(symbol: str, days_back: int = 3) -> List[Dict]:
    """抓最近 N 天的 company press releases (Finnhub).
    比一般新聞早, 但可能有未被媒體報導.
    """
    token = ds.get_finnhub_token()
    if not token:
        return []
    today = dt.date.today()
    frm = (today - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
    to = today.strftime("%Y-%m-%d")
    url = "https://finnhub.io/api/v1/press-releases"
    try:
        r = requests.get(url, params={
            "symbol": symbol,
            "from": frm,
            "to": to,
            "token": token,
        }, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json() or {}
        # finnhub 回 {"symbol":..., "majorDevelopment":[{headline, datetime, description, url},...]}
        items = data.get("majorDevelopment") if isinstance(data, dict) else data
        if not items:
            return []
        out = []
        for it in items[:20]:
            ts = it.get("datetime") or it.get("date")
            date_str = ""
            if ts:
                try:
                    if isinstance(ts, (int, float)):
                        date_str = dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                    else:
                        date_str = str(ts)[:10]
                except Exception:
                    date_str = str(ts)[:10]
            out.append({
                "title": it.get("headline") or it.get("title") or "",
                "link": it.get("url") or "",
                "publisher": "Company PR",
                "date": date_str,
                "type": "PR",
                "symbol": symbol,
                "summary": (it.get("description") or "")[:300],
            })
        return out
    except Exception as e:
        print(f"[event_sources] PR fetch {symbol} 失敗: {e}", flush=True)
        return []


# ---------------------------------------------------------------------------
# 台股重大訊息 (FinMind TaiwanStockNews / 公開資訊觀測站)
# ---------------------------------------------------------------------------
def fetch_tw_major_announcements(stock_id: Optional[str] = None,
                                   days_back: int = 2) -> List[Dict]:
    """抓台股重大訊息 (FinMind TaiwanStockNews dataset).
    stock_id=None 抓全部 (適合篩 universe);
    stock_id='2330' 抓單檔.
    """
    token = ds.get_finmind_token()
    if not token:
        return []
    today = dt.date.today()
    frm = (today - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockNews",
        "start_date": frm,
        "token": token,
    }
    if stock_id:
        params["data_id"] = str(stock_id)
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        j = r.json() or {}
        rows = j.get("data") or []
        out = []
        for row in rows[:30]:
            out.append({
                # Bug fix: 原本 description 若是「存在但為 None」, None[:80] 會 TypeError 炸掉整批抓取.
                #          用 (... or ... or "")[:80] 先擋掉 None 再切片.
                "title": row.get("title") or (row.get("description") or "")[:80],
                "link": row.get("link") or row.get("source_link") or "",
                "publisher": row.get("source") or "FinMind TW News",
                "date": str(row.get("date", ""))[:10],
                "type": "TW_NEWS",
                "symbol": str(row.get("stock_id", stock_id or "")),
                "summary": (row.get("description") or "")[:300],
            })
        return out
    except Exception as e:
        print(f"[event_sources] TW news fetch 失敗: {e}", flush=True)
        return []


# ---------------------------------------------------------------------------
# (評估後 skip) Twitter / X
# ---------------------------------------------------------------------------
def fetch_twitter_mentions(symbol: str) -> List[Dict]:
    """❌ Twitter 已無免費 API. 第三方 scraper (snscrape, nitter) 常被 ban.
    回 [] 並印警告.
    若要做, 建議:
      1. 付費 Twitter API ($100/month)
      2. 用 Reddit r/stocks 替代 (有 PRAW 免費 API)
      3. 用 StockTwits 免費 API
    """
    return []
