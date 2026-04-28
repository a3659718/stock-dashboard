"""
ai_analyzer.py
Gemini AI 深度個股分析。

把 stock_analyzer 抓到的所有資料 (K線、技術指標、三大法人、融資融券、新聞、條件命中)
組成 prompt，呼叫 Gemini 產出結構化分析報告。

風險口味：中間 — 給合理買進區間 + 停損建議，並明訂「僅供參考」。
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st

import data_sources as ds
import tw_screener as tw

# 模型 (Flash 免費版額度大)
DEFAULT_MODEL = "gemini-1.5-flash"


def get_gemini_key() -> str:
    return ds._secret("GEMINI_API_KEY")


def gemini_available() -> bool:
    if not get_gemini_key():
        return False
    try:
        import google.generativeai  # noqa
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 把資料壓縮成適合 LLM 的精簡 prompt
# ---------------------------------------------------------------------------
def _summarize_kline(daily: pd.DataFrame) -> str:
    if daily.empty:
        return "(無 K 線資料)"
    last = daily.iloc[-1]
    n = min(20, len(daily))
    tail = daily.tail(n)
    pct_5d = None
    pct_20d = None
    try:
        c = daily["close"].astype(float)
        if len(c) >= 6:
            pct_5d = (c.iloc[-1] / c.iloc[-6] - 1) * 100
        if len(c) >= 21:
            pct_20d = (c.iloc[-1] / c.iloc[-21] - 1) * 100
    except Exception:
        pass

    rows = []
    for _, r in tail.iterrows():
        d = r["date"].strftime("%m-%d") if hasattr(r["date"], "strftime") else str(r["date"])[:10]
        try:
            rows.append(
                f"{d} O:{r['open']:.1f} H:{r['high']:.1f} L:{r['low']:.1f} C:{r['close']:.1f} V:{int(r['Trading_Volume'])}"
            )
        except Exception:
            continue
    return (
        f"最近 {n} 個交易日 K 線:\n" + "\n".join(rows[-10:])  # 只送最後 10 天節省 token
        + (f"\n5 日漲幅: {pct_5d:+.2f}%" if pct_5d is not None else "")
        + (f"\n20 日漲幅: {pct_20d:+.2f}%" if pct_20d is not None else "")
    )


def _summarize_indicators(ind: pd.DataFrame) -> str:
    if ind.empty:
        return ""
    last = ind.iloc[-1]
    parts = []
    for col in ["MA20", "MA60", "K", "D", "DIF", "MACD", "Hist"]:
        if col in ind.columns:
            v = last[col]
            if pd.notna(v):
                parts.append(f"{col}={v:.2f}")
    return "技術指標: " + " · ".join(parts) if parts else ""


def _summarize_institutional(inst: pd.DataFrame) -> str:
    if inst.empty:
        return "(無法人資料)"
    inst = inst.copy()
    inst["net"] = inst["buy"].astype(float) - inst["sell"].astype(float)
    by_name = inst.groupby("name")["net"].sum().to_dict()
    name_map = {
        "Foreign_Investor": "外資",
        "Investment_Trust": "投信",
        "Dealer_self": "自營商-自行",
        "Dealer_Hedging": "自營商-避險",
        "Foreign_Dealer_Self": "外資自營",
    }
    lines = []
    for k, v in by_name.items():
        lines.append(f"  {name_map.get(k, k)}: {int(v):+,} 張")
    return "30 日法人累計買賣超:\n" + "\n".join(lines)


def _summarize_margin(margin: pd.DataFrame) -> str:
    if margin.empty:
        return ""
    margin = margin.sort_values("date")
    last = margin.iloc[-1]
    parts = []
    if "MarginPurchaseTodayBalance" in margin.columns:
        parts.append(f"融資餘額 {int(last['MarginPurchaseTodayBalance']):,} 張")
    for c in ["ShortSaleTodayBalance", "ShortSaleAfterBalance"]:
        if c in margin.columns:
            cur = int(last[c])
            chg = int(last[c] - margin.iloc[-2][c]) if len(margin) >= 2 else 0
            parts.append(f"融券餘額 {cur:,} 張 (近日 {chg:+,})")
            break
    return "融資融券: " + " · ".join(parts)


def _summarize_news(news_df: pd.DataFrame, max_items: int = 8) -> str:
    if news_df is None or news_df.empty:
        return "(無近期新聞)"
    keep_col = "title" if "title" in news_df.columns else None
    if not keep_col:
        return "(無新聞 title 欄)"
    items = news_df.head(max_items)
    lines = []
    for _, r in items.iterrows():
        d = r.get("date", "")
        if hasattr(d, "strftime"):
            d = d.strftime("%m-%d")
        else:
            d = str(d)[:10]
        title = str(r.get(keep_col, "")).strip()[:80]
        lines.append(f"  [{d}] {title}")
    return f"近 14 天新聞 ({len(items)} 則):\n" + "\n".join(lines)


def _summarize_hits(hits: Dict[str, bool], score: float) -> str:
    on = [tw.CONDITION_LABELS.get(k, k) for k, v in hits.items() if v]
    off = [tw.CONDITION_LABELS.get(k, k) for k, v in hits.items() if not v]
    s = f"綜合分數 {score}/10\n命中: {', '.join(on) if on else '無'}\n未命中: {', '.join(off) if off else '無'}"
    return s


# ---------------------------------------------------------------------------
# Prompt 組裝
# ---------------------------------------------------------------------------
def build_prompt(stock_meta: Dict, daily: pd.DataFrame, ind: pd.DataFrame,
                 inst: pd.DataFrame, margin: pd.DataFrame, news_df: pd.DataFrame,
                 hits: Dict[str, bool], score: float) -> str:
    # 注入市場情緒 macro context
    is_us = stock_meta.get("market") == "US"
    macro_lines = []
    if is_us:
        fg = ds.fetch_fear_greed()
        if fg and fg.get("score") is not None:
            macro_lines.append(f"美股市場情緒 (CNN F&G): {fg['score']:.0f} ({fg.get('rating')})")
    else:
        tw_p = ds.fetch_tw_market_pulse()
        if tw_p and tw_p.get("score") is not None:
            raw = tw_p.get("raw", {})
            macro_lines.append(
                f"台股市場情緒指數: {tw_p['score']} ({tw_p.get('rating_zh')}) | "
                f"加權 {raw.get('TWII')} | 5日 {raw.get('5日%')}% | 距 MA60 {raw.get('距 MA60 %')}%"
            )
        fg = ds.fetch_fear_greed()
        if fg and fg.get("score") is not None:
            macro_lines.append(f"美股市場情緒 (作參考): {fg['score']:.0f} ({fg.get('rating')})")

    macro_block = "【市場大環境】\n" + "\n".join(macro_lines) + "\n" if macro_lines else ""

    return f"""你是資深的股票分析師，風格務實、強調風險意識。請根據以下資料對股票做完整分析。

【基本資料】
代號: {stock_meta.get('stock_id')}
名稱: {stock_meta.get('name')}
產業: {stock_meta.get('industry')}
市場: {stock_meta.get('market')}

{macro_block}【K 線與漲跌】
{_summarize_kline(daily)}

【{_summarize_indicators(ind)}】

【{_summarize_margin(margin)}】

{_summarize_institutional(inst)}

【篩選條件】
{_summarize_hits(hits, score)}

【新聞】
{_summarize_news(news_df)}

──────────────────
請依照以下格式產出分析（用繁體中文）。每個段落 3–5 句即可，避免冗長：

## 📊 技術面
評估 MA 結構、KD、MACD、量價是否健康，是否站上支撐。

## 🏛️ 籌碼面
評估三大法人態度、融資融券變化，籌碼是否集中或鬆動。

## 📰 新聞題材
摘要近期最關鍵的 1–3 則新聞，評估屬利多/利空，並指出市場可能反應。

## ⚠️ 風險點
具體列出 2–4 點本檔目前的潛在風險（不是泛泛而談）。

## 🎯 進場建議
- 偏向: [看多 / 中性偏多 / 中性 / 中性偏空 / 看空]
- 合理買進區間: [若看多才填，給具體價位區間]
- 停損點: [若看多才填，給具體價位]
- 探高目標: [若看多才填，給具體價位]
- 操作節奏: [短線觀望 / 分批進場 / 等回測支撐 / 不建議進場 等]

## 💯 AI 信心分數
給 0–10 分並簡短說明依據。

──────────────────
⚠️ 注意事項：
1. 若資料不足以判斷，誠實說「資料不足」。
2. 進場價格務必貼近最新收盤，不要給離譜數字。
3. 若偏向中性以下，「合理買進區間」「停損點」「探高目標」可寫「不適用」。
4. 結尾務必加註「以上分析僅供參考，不構成投資建議」。"""


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_stock_news(stock_id: str, days: int = 14) -> pd.DataFrame:
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        df = ds._finmind_get("TaiwanStockNews", data_id=stock_id,
                              start_date=start, end_date=end)
        if df is not None and not df.empty:
            df["date"] = pd.to_datetime(df.get("date", pd.NaT), errors="coerce")
            df = df.sort_values("date", ascending=False).reset_index(drop=True)
            return df
    except Exception:
        pass
    return pd.DataFrame()


def analyze(stock_meta: Dict, daily: pd.DataFrame, ind: pd.DataFrame,
            inst: pd.DataFrame, margin: pd.DataFrame,
            hits: Dict[str, bool], score: float,
            model: str = DEFAULT_MODEL) -> Tuple[bool, str]:
    """呼叫 Gemini，回傳 (success, text)."""
    api_key = get_gemini_key()
    if not api_key:
        return False, "尚未設定 GEMINI_API_KEY"
    try:
        import google.generativeai as genai
    except ImportError:
        return False, "google-generativeai 套件未安裝，請更新 requirements.txt 後重新部署"

    news_df = fetch_stock_news(stock_meta.get("stock_id", ""))
    prompt = build_prompt(stock_meta, daily, ind, inst, margin, news_df, hits, score)

    try:
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={
                "temperature": 0.4,
                "top_p": 0.9,
                "max_output_tokens": 1500,
            },
        )
        text = (resp.text or "").strip()
        if not text:
            return False, "Gemini 沒有回應內容 (可能被安全過濾擋下)"
        return True, text
    except Exception as e:
        return False, f"Gemini 呼叫失敗: {e}"


# ---------------------------------------------------------------------------
# 圖片分析: 上傳 K 線截圖 → Gemini Vision
# ---------------------------------------------------------------------------
def _build_chart_prompt(extra_note: str = "", fg: dict | None = None,
                        market_news: list | None = None) -> str:
    fg_line = ""
    if fg and fg.get("score") is not None:
        fg_line = f"目前 CNN Fear & Greed Index = {fg['score']:.0f} ({fg.get('rating','')})"
    news_block = ""
    if market_news:
        news_block = "近 24 小時市場新聞題材:\n" + "\n".join(
            f"- {n.get('title','')[:80]}" for n in market_news[:8]
        )

    user_note = f"使用者備註: {extra_note}\n" if extra_note.strip() else ""

    return f"""你是專業技術分析師。請看圖片中的 K 線/走勢圖，做完整判讀。

{user_note}{fg_line}

{news_block}

請以下列格式產出分析（繁體中文，每段 3–5 句）：

## 📈 圖中觀察
描述圖中能看到的：時間區間、近期走勢、明顯的形態 (W底/M頂/三角收斂/突破等)、量能配合。

## 📊 技術面評估
評估均線結構 (若可見)、可能的支撐壓力位置、目前位置處於高/中/低檔。

## 🌍 市場大環境
結合上面提供的恐慌指數與市場新聞題材，評估目前進場的整體風險。

## ⚠️ 風險點
列出 2–3 點本圖看到的潛在風險。

## 🎯 進場建議
- 偏向: [看多 / 中性偏多 / 中性 / 中性偏空 / 看空]
- 合理買進區間: [若看多才填，給具體價位區間或形態觸發點]
- 停損點: [若看多才填]
- 探高目標: [若看多才填]

## 💯 AI 信心分數
給 0–10 分並簡短說明。

⚠️ 注意：無法從圖讀到的資訊請誠實說「無法判讀」。結尾加註「以上分析僅供參考，不構成投資建議」。"""


def analyze_open_picks(market: str, picks_summary: str,
                        model: str = DEFAULT_MODEL) -> Tuple[bool, str]:
    """對開盤分析的 picks 做 AI 觀點補強 (3-5 句)."""
    api_key = get_gemini_key()
    if not api_key:
        return False, "尚未設定 GEMINI_API_KEY"
    try:
        import google.generativeai as genai
    except ImportError:
        return False, "google-generativeai 未安裝"

    fg = ds.fetch_fear_greed()
    tw_p = ds.fetch_tw_market_pulse() if market == "TW" else None
    macro = []
    if tw_p and tw_p.get("score") is not None:
        macro.append(f"台股情緒指數 {tw_p['score']} ({tw_p.get('rating_zh')})")
    if fg and fg.get("score") is not None:
        macro.append(f"美股 F&G {fg['score']:.0f} ({fg.get('rating')})")
    macro_line = "市場大環境: " + " / ".join(macro) if macro else ""

    prompt = f"""你是專業 {('台股' if market == 'TW' else '美股')}分析師。下面是今日開盤後 30 分鐘的資金流向與動能股分析：

{macro_line}

{picks_summary}

請用 4-6 句繁體中文總結：
1. 今日資金主流是什麼類型？(防禦/成長/題材/輪動?)
2. 上述族群中，哪一個有機會延續整天？哪一個可能只是早盤反彈?
3. 給投資人 1-2 個操作節奏建議 (例如「等回測支撐」、「分批佈局 A 族群」、「規避 B 族群」)。

避免空泛、不要逐檔評論。結尾加「以上分析僅供參考，不構成投資建議」。"""

    try:
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.5, "max_output_tokens": 600},
        )
        text = (resp.text or "").strip()
        return (bool(text), text or "Gemini 沒有回應")
    except Exception as e:
        return False, f"Gemini 失敗: {e}"


def analyze_chart_image(image_bytes: bytes, extra_note: str = "",
                        fg: dict | None = None, market_news: list | None = None,
                        model: str = DEFAULT_MODEL) -> Tuple[bool, str]:
    """上傳 K 線截圖 → Gemini Vision 分析."""
    api_key = get_gemini_key()
    if not api_key:
        return False, "尚未設定 GEMINI_API_KEY"
    try:
        import google.generativeai as genai
        from PIL import Image
        import io
    except ImportError as e:
        return False, f"套件未安裝: {e}. 請更新 requirements.txt 後重新部署"

    try:
        genai.configure(api_key=api_key)
        img = Image.open(io.BytesIO(image_bytes))
        m = genai.GenerativeModel(model)
        prompt = _build_chart_prompt(extra_note, fg, market_news)
        resp = m.generate_content(
            [prompt, img],
            generation_config={
                "temperature": 0.4,
                "top_p": 0.9,
                "max_output_tokens": 1500,
            },
        )
        text = (resp.text or "").strip()
        if not text:
            return False, "Gemini 沒有回應內容 (可能被安全過濾擋下)"
        return True, text
    except Exception as e:
        return False, f"Gemini 圖片分析失敗: {e}"
