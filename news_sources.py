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
    cutoff = dt.datetime.now(timezone.utc) - dt.timedelta(hours=36)
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
# 財經新聞專用 (給 holiday_news 推播用 — 過濾掉純世界新聞 / 政治 / 體育)
# ---------------------------------------------------------------------------
# 純財經 feeds (這些 source 內容已是財經主軸, 不需關鍵字過濾)
FINANCE_RSS_FEEDS = {
    "CNN Business":      "http://rss.cnn.com/rss/money_latest.rss",
    "Fox Business":      "https://moxie.foxnews.com/google-publisher/business.xml",
    "BBC Business":      "http://feeds.bbci.co.uk/news/business/rss.xml",
    "NYT Business":      "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
}

# 一般新聞 source 出現這些字才視為財經 (過濾政治 / 體育 / 娛樂)
_FINANCE_KEYWORDS = [
    # 市場
    "stock", "market", "shares", "index", "indices", "rally", "selloff", "sell-off",
    "bull", "bear", "wall street", "nasdaq", "s&p", "dow", "futures", "etf",
    # 央行 / 利率
    "fed", "fomc", "rate cut", "rate hike", "rate decision", "interest rate",
    "yield", "treasury", "bonds", "central bank", "ecb", "boj",
    # 經濟
    "inflation", "cpi", "ppi", "gdp", "recession", "unemployment", "jobs report",
    "economy", "economic", "growth", "deficit", "tariff", "trade",
    # 公司 / 財報
    "earnings", "eps", "revenue", "guidance", "ipo", "buyback", "dividend",
    "merger", "acquisition", "takeover", "spin-off",
    # 商品 / 匯率
    "oil", "crude", "wti", "brent", "gold", "silver", "copper", "natural gas",
    "dollar", "euro", "yen", "yuan", "currency", "fx",
    # 加密貨幣 / 金融科技
    "bitcoin", "btc", "ethereum", "crypto", "cryptocurrency",
    # 公司名 (Mag 7 + 知名股票)
    "apple", "microsoft", "google", "amazon", "tesla", "nvidia", "meta", "openai",
    "tsmc", "samsung", "intel", "amd", "boeing", "exxon", "jpmorgan", "berkshire",
]


def _is_finance_news(item: Dict, source: str) -> bool:
    """判斷一則 RSS item 是不是財經新聞.

    純財經 source (Business 系列) 全部視為財經.
    其他 source 看 title + summary 是否含財經關鍵字.
    """
    if source in FINANCE_RSS_FEEDS:
        return True
    text = (
        (item.get("title", "") or "").lower()
        + " "
        + (item.get("summary", "") or "").lower()
    )
    return any(kw in text for kw in _FINANCE_KEYWORDS)


@st.cache_data(ttl=900, show_spinner=False)
def fetch_finance_news(max_items: int = 15) -> List[Dict]:
    """財經新聞聚合 — 給假日推播用.

    來源:
      1. 純財經 RSS (CNN/Fox/BBC/NYT Business)
      2. 一般 RSS 中過濾出含財經關鍵字的

    回傳近 36 小時內的 unique items, 至多 max_items 筆.
    """
    out: List[Dict] = []
    seen_titles: set = set()
    # 先抓純財經 feeds (優先)
    for name, url in FINANCE_RSS_FEEDS.items():
        for item in fetch_rss_feed(name, url, max_items=8):
            t = item.get("title", "").strip()
            if not t or t in seen_titles:
                continue
            seen_titles.add(t)
            out.append(item)

    # 再從一般 feeds 抓含財經關鍵字的 (補滿)
    for name, url in RSS_FEEDS.items():
        if name in FINANCE_RSS_FEEDS:
            continue  # 已經抓過
        for item in fetch_rss_feed(name, url, max_items=8):
            t = item.get("title", "").strip()
            if not t or t in seen_titles:
                continue
            if not _is_finance_news(item, name):
                continue
            seen_titles.add(t)
            out.append(item)

    # 過濾近 36 小時
    cutoff = dt.datetime.now(timezone.utc) - dt.timedelta(hours=36)
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
    return fresh[:max_items]


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
        m = genai.GenerativeModel("gemini-2.5-flash")
        resp = m.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": 2000,
                "response_mime_type": "application/json",
            },
            safety_settings=_ai.get_safety_settings(),
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
        delta = dt.datetime.now(timezone.utc) - d
        sec = int(delta.total_seconds())
        if sec < 60:
            return f"{sec} 秒前"
        if sec < 3600:
            return f"{sec // 60} 分鐘前"
        if sec < 86400:
            return f"{sec // 3600} 小時前"
        return f"{sec // 86400} 天前"
    except Exception:
        return iso_str[:16]


def _humanize_iso_time(iso_str: str) -> str:
    """把 ISO 時間轉成「3 小時前 / 2 天前」的人類可讀字串."""
    if not iso_str:
        return ""
    try:
        d = dt.datetime.fromisoformat(iso_str.replace("Z", ""))
        delta = dt.datetime.now(timezone.utc) - d
        sec = int(delta.total_seconds())
        if sec < 60:
            return f"{sec} 秒前"
        if sec < 3600:
            return f"{sec // 60} 分鐘前"
        if sec < 86400:
            return f"{sec // 3600} 小時前"
        return f"{sec // 86400} 天前"
    except Exception:
        return iso_str[:16]


def enrich_news_with_sentiment(news_list, lang_default: str = "en"):
    """對每個 news item 加 sentiment 分數 (基於關鍵字) + time_ago 顯示文字.

    item 已存在的 sentiment / time_ago 不會被覆蓋. 給 dashboard 顯示用.
    """
    if not news_list:
        return news_list
    try:
        import stock_catalyst
    except ImportError:
        return news_list
    out = []
    for n in news_list:
        if not isinstance(n, dict):
            out.append(n)
            continue
        n2 = dict(n)
        if "sentiment" not in n2:
            try:
                title = n2.get("title", "") or ""
                # 中文 title 用中文字典, 英文用英文字典
                lang = "zh" if any('\u4e00' <= ch <= '\u9fff' for ch in title[:30]) else lang_default
                s = stock_catalyst._score_news_sentiment(title, lang=lang)
                n2["sentiment"] = s.get("score", 0)
                n2["sentiment_keywords"] = (s.get("bullish") or s.get("bearish") or [])[:3]
            except Exception:
                n2["sentiment"] = 0
        if "time_ago" not in n2:
            try:
                n2["time_ago"] = _humanize_iso_time(n2.get("time", ""))
            except Exception:
                n2["time_ago"] = ""
        out.append(n2)
    return out


def translate_news_titles(news_list, max_items: int = 15):
    """用 Gemini 把英文 news title 翻成繁中 (一次批次). 失敗就 keep original.

    每個 item 多一個 "title_zh" 欄位 (跟 fmt_holiday_news 對接).
    """
    if not news_list:
        return news_list
    # 只翻譯沒 title_zh 的
    targets = [(i, n) for i, n in enumerate(news_list[:max_items])
               if isinstance(n, dict) and not n.get("title_zh") and n.get("title")]
    if not targets:
        return news_list

    try:
        import ai_analyzer as _ai
        if not _ai.gemini_available():
            return news_list
    except ImportError:
        return news_list

    titles = [t[1].get("title", "") for t in targets]
    prompt = (
        "請把以下英文新聞標題翻成繁體中文 (一行一個, 順序對應). "
        "保留專有名詞 / 公司股票代號 / 數字百分比. 簡潔, 不超過 35 字.\n\n"
        + "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
        + "\n\n用 JSON array 回 (不要 markdown), 順序對應上面 1..N: [\"翻譯1\", \"翻譯2\", ...]"
    )
    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel("gemini-2.5-flash")
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.2, "max_output_tokens": 1500,
                                "response_mime_type": "application/json"},
            safety_settings=_ai.get_safety_settings(),
        )
        import json, re as _re
        text = (getattr(resp, "text", None) or "").strip()
        text = _re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=_re.MULTILINE)
        translated = json.loads(text)
        if not isinstance(translated, list):
            return news_list
        # 寫回
        out = list(news_list)
        for j, (orig_idx, n) in enumerate(targets):
            if j < len(translated):
                n2 = dict(n)
                n2["title_zh"] = str(translated[j])[:200]
                out[orig_idx] = n2
        return out
    except Exception as e:
        print(f"[news_sources] translate_news_titles failed: {e}", flush=True)
        return news_list


def build_news_context(include_trump: bool = True, max_items: int = 8) -> str:
    """整合多源新聞 + Trump 言論 + 油價 + macro, 給 AI 分析個股時當 context.

    回一個多行字串 (可以直接餵 prompt). 失敗回空字串.
    """
    parts = []
    try:
        # 油價 + macro
        oil = fetch_oil_signal()
        if oil:
            parts.append(f"WTI 油價: ${oil.get('price','—')} ({oil.get('pct_5d',0):+.1f}% 5d) — {oil.get('signal','')}")
        macro = fetch_macro_indicators()
        if macro:
            macro_parts = []
            for k in ("美元指數", "10年美債殖利率", "VIX", "BTC"):
                if k in macro:
                    v = macro[k]
                    macro_parts.append(f"{k} {v.get('value','—')} ({v.get('pct_5d',0):+.2f}%)")
            if macro_parts:
                parts.append("Macro: " + " · ".join(macro_parts))
    except Exception:
        pass

    try:
        # 財經新聞 top N (sentiment 強的優先)
        news = fetch_finance_news(max_items=max_items)
        news = enrich_news_with_sentiment(news, lang_default="en")
        # 排序 sentiment abs 大的優先
        news.sort(key=lambda n: abs(n.get("sentiment", 0) or 0), reverse=True)
        if news:
            parts.append("近期重要新聞:")
            for n in news[:max_items]:
                src = n.get("source", "")
                t = n.get("title_zh") or n.get("title", "")
                sent = n.get("sentiment", 0) or 0
                tag = "📈" if sent > 0 else ("📉" if sent < 0 else "▪")
                parts.append(f"  {tag} [{src}] {t[:120]}")
    except Exception:
        pass

    if include_trump:
        try:
            trumps = fetch_trump_truth_social(max_items=2)
            if trumps:
                parts.append("Trump 最新言論:")
                for t in trumps[:1]:
                    txt = (t.get("text") or "")[:200]
                    parts.append(f"  • {txt}")
        except Exception:
            pass

    return "\n".join(parts)
