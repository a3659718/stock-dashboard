"""
news_sources.py
整合多個外部新聞 / 訊號來源，提供給 AI 分析時做 sentiment context。

來源：
  - CNN (Top + Business)        RSS
  - Fox News (Business)          RSS
  - BBC (Business + World)       RSS
  - Reuters / NYT (備用)         RSS
  - WTI 油價                     yfinance (CL=F)
  - 美元指數                     yfinance (DX-Y.NYB)
  - 10 年期美債殖利率            yfinance (^TNX)
  - 黃金                          yfinance (GC=F)
  - 比特幣                        yfinance (BTC-USD)
  - Trump Truth Social           Mastodon-compat public API (best effort)

每個函式都用 cache_data 包起來，避免每次掃描都重打。
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List

import pandas as pd
import requests
import streamlit as st

import data_sources as ds


# ---------------------------------------------------------------------------
# RSS feeds
# ---------------------------------------------------------------------------
RSS_FEEDS = {
    "CNN Business":      "http://rss.cnn.com/rss/money_latest.rss",
    "CNN Top":           "http://rss.cnn.com/rss/cnn_topstories.rss",
    "Fox Business":      "https://moxie.foxnews.com/google-publisher/business.xml",
    "Fox World":         "https://moxie.foxnews.com/google-publisher/world.xml",
    "BBC Business":      "http://feeds.bbci.co.uk/news/business/rss.xml",
    "BBC World":         "http://feeds.bbci.co.uk/news/world/rss.xml",
    "NYT Business":      "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "Reuters Top":       "https://feeds.reuters.com/reuters/topNews",
}


@st.cache_data(ttl=900, show_spinner=False)
def fetch_rss_feed(name: str, url: str, max_items: int = 8) -> List[Dict]:
    """通用 RSS parser (用 feedparser)."""
    try:
        import feedparser  # type: ignore
    except ImportError:
        return []
    try:
        feed = feedparser.parse(url)
        items = []
        for e in feed.entries[:max_items]:
            published = e.get("published_parsed") or e.get("updated_parsed")
            ts = ""
            if published:
                try:
                    ts = dt.datetime(*published[:6]).isoformat()
                except Exception:
                    pass
            items.append({
                "source": name,
                "title": getattr(e, "title", "")[:200],
                "link": getattr(e, "link", ""),
                "summary": (getattr(e, "summary", "") or "")[:300],
                "time": ts,
            })
        return items
    except Exception:
        return []


@st.cache_data(ttl=900, show_spinner=False)
def fetch_world_news() -> List[Dict]:
    """所有 RSS 來源彙整，去重後過濾近 24 小時。"""
    out: List[Dict] = []
    seen_titles: set = set()
    for name, url in RSS_FEEDS.items():
        for item in fetch_rss_feed(name, url, max_items=8):
            t = item.get("title", "").strip()
            if not t or t in seen_titles:
                continue
            seen_titles.add(t)
            out.append(item)
    # 過濾近 36 小時
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=36)
    fresh = []
    for it in out:
        ts = it.get("time")
        if not ts:
            fresh.append(it)
            continue
        try:
            d = dt.datetime.fromisoformat(ts.replace("Z", ""))
            if d >= cutoff:
                fresh.append(it)
        except Exception:
            fresh.append(it)
    return fresh


# ---------------------------------------------------------------------------
# 油價 + 其他大宗商品 / 殖利率訊號
# ---------------------------------------------------------------------------
def _pct_change(close: pd.Series, days: int) -> float:
    if len(close) < days + 1:
        return 0.0
    return float((close.iloc[-1] / close.iloc[-(days + 1)] - 1) * 100)


@st.cache_data(ttl=600, show_spinner=False)
def fetch_oil_signal() -> Dict:
    """WTI 油價變化與股市影響訊號."""
    df = ds.fetch_yf_history("CL=F", period="2mo", interval="1d")
    if df.empty or len(df) < 22:
        return {}
    close = df["Close"].astype(float)
    last = float(close.iloc[-1])
    pct_1d = _pct_change(close, 1)
    pct_5d = _pct_change(close, 5)
    pct_20d = _pct_change(close, 20)

    if pct_5d > 5:
        signal = "油價急漲（通膨壓力升溫，**利空**風險性資產）"
        sentiment = "bearish"
    elif pct_5d > 2:
        signal = "油價走揚（中性偏空）"
        sentiment = "slightly_bearish"
    elif pct_5d < -5:
        signal = "油價急跌（需求疑慮或 OPEC+ 鬆動，**混合訊號**）"
        sentiment = "mixed"
    elif pct_5d < -2:
        signal = "油價回落（**利多**通膨敏感類股）"
        sentiment = "slightly_bullish"
    else:
        signal = "油價平穩"
        sentiment = "neutral"

    return {
        "name": "WTI 原油",
        "price": round(last, 2),
        "pct_1d": round(pct_1d, 2),
        "pct_5d": round(pct_5d, 2),
        "pct_20d": round(pct_20d, 2),
        "signal": signal,
        "sentiment": sentiment,
    }


@st.cache_data(ttl=600, show_spinner=False)
def fetch_macro_indicators() -> Dict:
    """整合大宗商品 / 美元 / 殖利率 / 加密貨幣，當作 macro context."""
    indicators = {
        "美元指數": "DX-Y.NYB",
        "10年美債殖利率": "^TNX",
        "黃金": "GC=F",
        "BTC": "BTC-USD",
        "VIX": "^VIX",
    }
    out: Dict[str, Dict] = {}
    for name, sym in indicators.items():
        df = ds.fetch_yf_history(sym, period="1mo", interval="1d")
        if df.empty or len(df) < 6:
            continue
        try:
            close = df["Close"].astype(float)
            last = float(close.iloc[-1])
            out[name] = {
                "symbol": sym,
                "value": round(last, 2),
                "pct_1d": round(_pct_change(close, 1), 2),
                "pct_5d": round(_pct_change(close, 5), 2),
            }
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Trump Truth Social (Mastodon-compat 公開 API)
# ---------------------------------------------------------------------------
TRUMP_TS_ACCOUNT_ID = "107780257626128497"  # @realDonaldTrump 的固定 ID


@st.cache_data(ttl=900, show_spinner=False)
def fetch_trump_truth_social(max_items: int = 10) -> List[Dict]:
    """從 Truth Social 公開 endpoint 抓 Trump 最新貼文。
    Best-effort: API 變更或被擋會回空 list。
    """
    url = f"https://truthsocial.com/api/v1/accounts/{TRUMP_TS_ACCOUNT_ID}/statuses"
    try:
        r = requests.get(
            url,
            params={"limit": max_items, "exclude_replies": True, "exclude_reblogs": False},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        out = []
        for it in data:
            content = it.get("content", "")
            # 簡單去 HTML
            import re
            text = re.sub(r"<[^>]+>", " ", content).strip()
            text = re.sub(r"\s+", " ", text)
            if not text and not it.get("reblog"):
                continue
            out.append({
                "source": "Trump Truth Social",
                "time": it.get("created_at", ""),
                "text": text[:400],
                "link": it.get("url", ""),
                "is_reblog": bool(it.get("reblog")),
            })
        return out[:max_items]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 彙整訊號 → 給 AI 用
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Gemini 批次翻譯英文新聞 → 繁中
# ---------------------------------------------------------------------------
def _has_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text or "")


@st.cache_resource
def _translation_cache() -> Dict[str, str]:
    """記憶體 cache: {english_title: chinese_title}.
    跨 session 共享，container 重啟才清。
    """
    return {}


def _gemini_translate_batch(titles: List[str]) -> Dict[str, str]:
    """一次呼叫 Gemini 翻譯多則 title 為繁中。"""
    if not titles:
        return {}
    try:
        import ai_analyzer as _ai
    except ImportError:
        return {}
    if not _ai.gemini_available():
        return {}
    try:
        import google.generativeai as genai
    except ImportError:
        return {}

    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "請把下列新聞標題翻譯為繁體中文（台灣財經慣用語）。"
        "保留股票代號 / 公司名英文（例如 NVIDIA、Tesla 不翻）。"
        "用嚴格 JSON 格式回應，key 是序號字串，value 是繁中翻譯。\n"
        "範例: {\"1\": \"NVIDIA 與微軟簽下大單\", \"2\": \"歐洲股市受通膨拖累下跌\"}\n\n"
        f"待翻譯標題：\n{numbered}"
    )
    try:
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel("gemini-1.5-flash")
        resp = m.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": 2000,
                "response_mime_type": "application/json",
            },
        )
        text = (resp.text or "").strip()
        if not text:
            return {}
        import json, re
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        out: Dict[str, str] = {}
        for i, original in enumerate(titles, 1):
            zh = data.get(str(i)) or data.get(i)
            if zh and isinstance(zh, str):
                out[original] = zh.strip()
        return out
    except Exception:
        return {}


def translate_news_titles(news_list: List[Dict]) -> List[Dict]:
    """為每則 news 補上 title_zh 欄位 (繁中).
    台股 / 已是中文的不會被翻譯, 只翻英文新聞.
    Gemini 不可用時 title_zh = 原文.
    """
    cache = _translation_cache()
    pending: List[str] = []
    for n in news_list:
        t = (n.get("title") or "").strip()
        if not t or _has_chinese(t) or t in cache:
            continue
        pending.append(t)

    # 去重
    pending = list(dict.fromkeys(pending))

    if pending:
        # 一次最多翻 30 則 (避免 prompt 過長)
        for i in range(0, len(pending), 30):
            batch = pending[i:i + 30]
            new_trans = _gemini_translate_batch(batch)
            cache.update(new_trans)

    out = []
    for n in news_list:
        x = dict(n)
        t = (x.get("title") or "").strip()
        if not t:
            x["title_zh"] = ""
        elif _has_chinese(t):
            x["title_zh"] = t
        else:
            x["title_zh"] = cache.get(t, t)  # 翻不到就 fallback 原文
        out.append(x)
    return out


def time_ago(iso_str: str) -> str:
    """把 ISO 時間轉成「3 小時前 / 2 天前」的人類可讀字串."""
    if not iso_str:
        return ""
    try:
        d = dt.datetime.fromisoformat(iso_str.replace("Z", ""))
    except Exception:
        return iso_str[:16]
    now = dt.datetime.utcnow()
    diff = now - d
    secs = int(diff.total_seconds())
    if secs < 60:
        return "剛剛"
    if secs < 3600:
        return f"{secs // 60} 分鐘前"
    if secs < 86400:
        return f"{secs // 3600} 小時前"
    if secs < 604800:
        return f"{secs // 86400} 天前"
    return d.strftime("%Y-%m-%d")


def enrich_news_with_sentiment(news_list: List[Dict], lang_default: str = "en") -> List[Dict]:
    """為每則新聞補上 sentiment 標籤 + 命中關鍵字 + 相對時間.
    lang_default: 'en' for world news, 'zh' for TaiwanStockNews.
    """
    try:
        import stock_catalyst
    except ImportError:
        return news_list
    out = []
    for n in news_list:
        x = dict(n)
        title = x.get("title", "")
        # Heuristic: 含中文字元 → zh
        has_chinese = any("一" <= ch <= "鿿" for ch in title)
        lang = "zh" if has_chinese else lang_default
        s = stock_catalyst._score_news_sentiment(title, lang=lang)
        x["sentiment"] = s.get("score", 0)
        x["bullish_kw"] = s.get("bullish", [])
        x["bearish_kw"] = s.get("bearish", [])
        x["time_ago"] = time_ago(x.get("time", ""))
        out.append(x)
    return out


def fetch_tw_news_aggregated(stock_ids: List[str] = None, days: int = 3,
                                max_per_stock: int = 3) -> List[Dict]:
    """從 FinMind TaiwanStockNews 抓多檔個股新聞彙整，做台股新聞分頁用."""
    if not stock_ids:
        return []
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    out: List[Dict] = []
    for sid in stock_ids[:30]:  # 最多 30 檔避免太久
        try:
            df = ds._finmind_get("TaiwanStockNews", data_id=sid,
                                  start_date=start, end_date=end)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        df["date"] = pd.to_datetime(df.get("date", pd.NaT), errors="coerce")
        for _, r in df.head(max_per_stock).iterrows():
            ts = ""
            if pd.notna(r.get("date")):
                ts = r["date"].isoformat()
            out.append({
                "source": f"FinMind ({sid})",
                "title": str(r.get("title", "") or "")[:200],
                "link": str(r.get("link", "") or ""),
                "summary": str(r.get("summary", "") or "")[:300],
                "time": ts,
                "stock_id": sid,
            })
    return out


def build_news_context(include_trump: bool = True) -> str:
    """組裝給 Gemini 用的純文字 context."""
    parts = []

    # 油價 + macro
    oil = fetch_oil_signal()
    if oil:
        parts.append(
            f"WTI 原油 ${oil['price']} (1d {oil['pct_1d']:+.1f}%, 5d {oil['pct_5d']:+.1f}%) — {oil['signal']}"
        )
    macro = fetch_macro_indicators()
    if macro:
        macro_lines = []
        for name, m in macro.items():
            macro_lines.append(f"  {name}: {m['value']} (1d {m['pct_1d']:+.2f}%, 5d {m['pct_5d']:+.2f}%)")
        parts.append("Macro 指標:\n" + "\n".join(macro_lines))

    # World news
    news = fetch_world_news()
    if news:
        parts.append(f"國際新聞 ({len(news)} 則):")
        # 按來源分組
        by_source: Dict[str, List[Dict]] = {}
        for n in news[:30]:
            by_source.setdefault(n["source"], []).append(n)
        for src, items in by_source.items():
            parts.append(f"  [{src}]")
            for n in items[:3]:
                parts.append(f"    - {n['title']}")

    # Trump
    if include_trump:
        trumps = fetch_trump_truth_social(max_items=5)
        if trumps:
            parts.append("Trump Truth Social (近期):")
            for t in trumps[:5]:
                parts.append(f"  - {t['text'][:150]}")

    return "\n\n".join(parts)
