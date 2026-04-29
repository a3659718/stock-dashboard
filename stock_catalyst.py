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
        sid = str(r.get("stock_id", "") or r.get("代號", ""))
        if not sid:
            continue
        nm = r.get("stock_name", "") or r.get("名稱", "") or ""
        pct = r.get("今日%")
        if pct is None:
            pct = r.get("今日%") or r.get("daily_%")
        news = fetch_news_for_stock(sid, market=market, max_items=5, days=7)
        payload.append({
            "stock_id": sid,
            "name": nm,
            "today_pct": pct,
            "news_titles": [n["title"] for n in news[:5] if n.get("title")],
            "_first_news": news[0] if news else None,
        })

    # 嘗試 Gemini 批次
    catalysts = _gemini_batch_catalysts(payload, market=market)
    if catalysts:
        return catalysts

    # Fallback: 取第一則新聞 title 為催化劑
    fallback: Dict[str, str] = {}
    for s in payload:
        first = s.get("_first_news")
        sid = s["stock_id"]
        if first and first.get("title"):
            fallback[sid] = f"📰 {first['title']}"
        else:
            fallback[sid] = ""
    return fallback
