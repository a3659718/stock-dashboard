"""
stock_catalyst.py
為熱門股票自動找出上漲催化劑（產品 / 新聞 / 事件）。

策略：
  1) 對每檔股票抓近 7 天新聞 (TW: FinMind, US: yfinance)
  2) 把所有股票 + 新聞 + 漲幅打包成一個 prompt 餵給 Gemini
  3) Gemini 回 JSON: {stock_id: "1-2 句具體催化劑"}
  4) 一次 Gemini 呼叫處理 10-15 支股票，省 quota

無 Gemini 時會 fallback 顯示第一則新聞 title。
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import data_sources as ds


# ---------------------------------------------------------------------------
# Sentiment 關鍵字字典 (用於沒有 Gemini 時 fallback)
# ---------------------------------------------------------------------------
BULLISH_KW_TW = [
    # 業績相關
    "EPS 創新高", "創歷史新高", "創新高", "再創高", "暴賺", "獲利大增", "獲利成長",
    "轉虧為盈", "扭虧", "獲利翻倍", "利多", "成長", "營收創高", "營收新高",
    "毛利率提升", "毛利攀升", "上修財測", "上修目標價", "目標價調升",
    # 訂單 / 業務
    "大單", "大量訂單", "簽約", "得標", "入選", "認證通過", "獲認證", "入列供應鏈",
    "出貨", "新單", "接單", "拿下", "搶下", "放量", "擴產", "新廠", "新品上市",
    "推出新品", "合作", "策略結盟", "合資", "結盟",
    # 題材
    "AI 訂單", "AI 概念", "AI 伺服器", "受惠 AI", "AI 紅利", "供應鏈", "黃金供應鏈",
    "電動車", "低軌衛星", "重電", "儲能", "散熱", "矽光子", "ABF",
    # 法人 / 籌碼
    "投信買超", "外資買超", "三大法人買超", "大股東增持", "庫藏股",
    # 股價表現
    "漲停", "亮燈漲停", "強勢", "領漲",
]

BEARISH_KW_TW = [
    "虧損", "下修", "下滑", "減產", "停工", "裁員", "減資", "利空", "看空",
    "賣超", "跌停", "認列損失", "認列減損", "衰退", "衝擊", "停牌", "下市",
    "警示", "全額交割", "訴訟", "罰款", "違法", "失敗", "退單", "縮減", "暫停",
    "大跌", "重挫", "腰斬", "停損", "出售", "減持", "賣出",
    "降評", "目標價調降", "下修目標價", "預期下調", "獲利下滑",
]

BULLISH_KW_EN = [
    "beat", "beats", "raise", "raises", "raised", "upgrade", "upgraded",
    "outperform", "growth", "record high", "record", "all-time high",
    "surge", "surged", "soar", "soared", "rally", "rallied",
    "win", "wins", "won", "deal", "contract", "approval", "approved",
    "expand", "expanded", "launch", "launched", "partnership",
    "boost", "boosted", "exceed", "exceeded", "strong",
    "buyback", "dividend hike", "guidance raised", "AI demand",
]

BEARISH_KW_EN = [
    "miss", "missed", "cut", "downgrade", "downgraded", "underperform",
    "decline", "declined", "loss", "losses", "plunge", "plunged",
    "drop", "dropped", "slump", "slumped", "lawsuit", "fine", "fined",
    "warning", "delay", "delayed", "halt", "halted", "recall",
    "investigation", "probe", "scandal", "weak", "guidance cut",
]


def _score_news_sentiment(title: str, lang: str = "zh") -> Dict:
    """為一則新聞 title 計分.
    回傳 {score, bullish_words[], bearish_words[]}.
    """
    bull_kw = BULLISH_KW_TW if lang == "zh" else BULLISH_KW_EN
    bear_kw = BEARISH_KW_TW if lang == "zh" else BEARISH_KW_EN
    t = title.lower() if lang == "en" else title

    bullish_hits = []
    bearish_hits = []
    for w in bull_kw:
        if (w.lower() if lang == "en" else w) in t:
            bullish_hits.append(w)
    for w in bear_kw:
        if (w.lower() if lang == "en" else w) in t:
            bearish_hits.append(w)
    score = len(bullish_hits) - len(bearish_hits)
    return {"score": score, "bullish": bullish_hits, "bearish": bearish_hits}


def _pick_relevant_news(news_list: List[Dict], lang: str = "zh") -> Optional[Dict]:
    """從新聞列表挑出最有 sentiment 訊號的 (利多優先 → 利空 → 最新)."""
    if not news_list:
        return None
    scored = []
    for n in news_list:
        title = (n.get("title") or "").strip()
        if not title:
            continue
        s = _score_news_sentiment(title, lang=lang)
        scored.append({**n, **s})
    if not scored:
        return None
    # 排序：score 由高到低，相同 score 取較新
    scored.sort(key=lambda x: (x.get("score", 0), x.get("date", "")), reverse=True)
    return scored[0]


# ---------------------------------------------------------------------------
# 抓個股新聞
# ---------------------------------------------------------------------------
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_news_for_stock(stock_id: str, market: str = "TW", max_items: int = 5,
                          days: int = 7) -> List[Dict]:
    """取近 N 天個股新聞，TW 用 FinMind, US 用 yfinance."""
    if market == "TW":
        today = dt.date.today()
        end = today.strftime("%Y-%m-%d")
        start = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            df = ds._finmind_get("TaiwanStockNews", data_id=stock_id,
                                  start_date=start, end_date=end)
        except Exception:
            return []
        if df is None or df.empty:
            return []
        items = []
        for _, r in df.head(max_items).iterrows():
            items.append({
                "title": str(r.get("title", "") or "")[:180],
                "date": str(r.get("date", "") or "")[:10],
                "source": str(r.get("source", "") or "")[:30],
                "link": str(r.get("link", "") or ""),
            })
        return items
    else:  # US
        items = ds.fetch_yahoo_news(stock_id, max_n=max_items)
        return [
            {
                "title": it.get("title", "") or "",
                "date": "",
                "source": it.get("publisher", "") or "",
                "link": it.get("link", "") or "",
            } for it in items if it.get("title")
        ]


# ---------------------------------------------------------------------------
# Gemini 批次催化劑摘要
# ---------------------------------------------------------------------------
def _gemini_batch_catalysts(payload: List[Dict], market: str = "TW",
                              model: str = "gemini-1.5-flash") -> Dict[str, str]:
    """payload: [{stock_id, name, today_pct, news_titles: [str,...]}, ...]
    回傳 {stock_id: 1-2 句催化劑}.
    """
    try:
        import ai_analyzer as _ai  # avoid circular at top-level
    except ImportError:
        return {}
    if not _ai.gemini_available():
        return {}

    if not payload:
        return {}

    # 組 prompt (每支股票一段)
    blocks = []
    for s in payload:
        sid = s.get("stock_id", "")
        name = s.get("name", "")
        pct = s.get("today_pct")
        news_titles = s.get("news_titles", []) or []
        news_str = "\n".join(f"  - {t}" for t in news_titles[:5]) if news_titles else "  (無近期新聞，請推測可能原因)"
        pct_str = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—"
        blocks.append(f"### {sid} {name} (今日 {pct_str})\n{news_str}")

    market_label = "台股" if market == "TW" else "美股"
    blocks_text = "\n\n".join(blocks)
    prompt = f"""你是專業{market_label}分析師。下面有 {len(payload)} 檔今日熱門股，請為每檔列出 **1-2 句具體上漲催化劑** (產品 / 客戶 / 訂單 / 法說 / 政策 / 政治事件 / 產業利多 等)。

如果新聞中找不到明顯催化劑，根據公司主業推測「市場可能反應的題材」(例如 AI 伺服器需求、低軌衛星布局、無人機題材、半導體供應鏈位置等)。**不要寫「無催化劑」之類的廢話，盡量給有資訊量的內容。**

請用嚴格 JSON 格式回應，key 是 stock_id，value 是 1-2 句中文催化劑。範例:
{{"2330": "AI 伺服器訂單湧入，1nm 製程即將量產帶動法人上修目標價", "6669": "緯穎 AI 伺服器接獲 Meta 大單，估值重估"}}

不要加任何前後 markdown，只回 JSON。

待分析個股：

{blocks_text}"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 1500,
                                "response_mime_type": "application/json"},
        )
        text = (resp.text or "").strip()
        if not text:
            return {}
        # 防呆: 移除 markdown code fence
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        # 容錯：解析失敗就回空，不讓整個推播 fail
        return {}


# ---------------------------------------------------------------------------
# 對外: 對 picks 補催化劑
# ---------------------------------------------------------------------------
def annotate_picks_with_catalysts(picks_data: List[Dict], market: str = "TW") -> Dict[str, str]:
    """
    picks_data: list of dicts/rows with at least stock_id / stock_name / 今日%

    回傳 {stock_id: catalyst_text}.

    當沒有 Gemini 時，fallback 用最新一則新聞 title 取代。
    """
    if not picks_data:
        return {}

    # 整理成 batch payload
    payload: List[Dict] = []
    for r in picks_data:
        sid = str(r.get("stock_id", "") or r.get("代號", "") or r.get("symbol", ""))
        if not sid:
            continue
        nm = r.get("stock_name", "") or r.get("名稱", "") or ""
        pct = r.get("今日%")
        if pct is None:
            pct = r.get("daily_%")
        news = fetch_news_for_stock(sid, market=market, max_items=8, days=14)
        payload.append({
            "stock_id": sid,
            "name": nm,
            "today_pct": pct,
            "news_titles": [n["title"] for n in news[:5] if n.get("title")],
            "_news_list": news[:8],
        })

    # 嘗試 Gemini 批次
    catalysts = _gemini_batch_catalysts(payload, market=market)
    if catalysts:
        return catalysts

    # Fallback: 用關鍵字 sentiment 挑最相關新聞並標 利多 / 利空 / 中性
    fallback: Dict[str, str] = {}
    lang = "zh" if market == "TW" else "en"
    for s in payload:
        sid = s["stock_id"]
        news_list = s.get("_news_list") or []
        if not news_list:
            fallback[sid] = ""
            continue

        best = _pick_relevant_news(news_list, lang=lang)
        if not best:
            fallback[sid] = ""
            continue

        title = best.get("title", "")
        score = best.get("score", 0)
        if score > 0:
            kws = "、".join(best.get("bullish", [])[:3]) if lang == "zh" else ", ".join(best.get("bullish", [])[:3])
            tag = f"〔{kws}〕" if kws else ""
            fallback[sid] = f"📈 利多：{title} {tag}"
        elif score < 0:
            kws = "、".join(best.get("bearish", [])[:3]) if lang == "zh" else ", ".join(best.get("bearish", [])[:3])
            tag = f"〔{kws}〕" if kws else ""
            fallback[sid] = f"📉 注意利空：{title} {tag}"
        else:
            fallback[sid] = f"📰 {title}"
    return fallback
