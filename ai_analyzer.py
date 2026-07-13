"""
ai_analyzer.py
Gemini AI 深度個股分析。

把 stock_analyzer 抓到的所有資料 (K線、技術指標、三大法人、融資融券、新聞、條件命中)
組成 prompt，呼叫 Gemini 產出結構化分析報告。

風險口味：中間 — 給合理買進區間 + 停損建議，並明訂「僅供參考」。
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

import data_sources as ds
import tw_screener as tw

# 模型 (Flash 免費版額度大)
DEFAULT_MODEL = "gemini-2.5-flash"


# 寬鬆 safety settings — 避免財經/政治/Trump 言論被預設過濾擋下
# 有 4 個 category，全部設 BLOCK_ONLY_HIGH (只擋極端內容)
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
]


def get_safety_settings():
    """匯出供其他模組使用 (各 Gemini call site 統一傳這個)."""
    return SAFETY_SETTINGS


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


def _get_model(model_name: str = DEFAULT_MODEL):
    """回一個設定好 (含 safety settings) 的 GenerativeModel; 不可用時回 None.

    Bug fix: 先前此函式不存在, 但有 8 個模組 (ipo_calendar / pre_market / trump_policy /
    analyst_insider / news_impact / rate_cycle / tw_post_market / ai_confidence_scorer)
    都 `from ai_analyzer import _get_model` 然後 `model.generate_content(prompt)`. 缺這個
    函式 → 那些 import 全部 ImportError → 被各自 try/except 吞掉 → Gemini 分析默默失效,
    只剩規則式 / 空白 fallback. 補上後一次救活全部 8 個功能.

    呼叫端會處理 None (e.g. `if model is None: return ""`), 故 key/套件不可用時回 None.
    """
    key = get_gemini_key()
    if not key:
        return None
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        return genai.GenerativeModel(model_name, safety_settings=get_safety_settings())
    except Exception as e:
        print(f"[ai_analyzer] _get_model 建立失敗: {e}", flush=True)
        return None


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


def _summarize_deep_analysis(deep_analysis: Optional[Dict]) -> str:
    """格式化 stock_deep_analyzer.get_deep_analysis 的結果, 給 Gemini prompt 用.

    回空字串時表示沒深度資料. 有時回完整 block (含標題).
    """
    if not deep_analysis:
        return ""
    parts = []
    # 估值
    pe = deep_analysis.get("pe_peers") or {}
    if pe.get("stock_pe") is not None:
        parts.append(
            f"  本股 PE {pe['stock_pe']}, 同業中位 {pe.get('peer_median_pe','—')}, "
            f"percentile {pe.get('stock_percentile','—')}%, 估值: {pe.get('valuation','—')}"
        )
    # 籌碼變化
    h = deep_analysis.get("holdings") or {}
    if h.get("trend"):
        if h.get("foreign_pct_now") is not None:
            parts.append(
                f"  外資持股 {h['foreign_pct_now']:.2f}%, "
                f"30 日變化 {h.get('foreign_change_30d',0):+.2f}pp, {h['trend']}"
            )
        elif h.get("fi_30d_lots"):
            parts.append(
                f"  外資 30d 累積 {int(h['fi_30d_lots']):+,} 張, {h['trend']}"
            )
    # K 形態
    cp = deep_analysis.get("candle_patterns") or {}
    if cp.get("summary"):
        parts.append(
            f"  K 線形態 (近 5 日): {cp['summary']}; 短期趨勢: {cp.get('trend_context','—')}"
        )
    # 財報數據
    fund = deep_analysis.get("fundamentals") or {}
    if fund.get("summary"):
        parts.append(f"  財報數據: {fund['summary']}")
    # 重大訊息摘要 + sentiment 統計
    ann = deep_analysis.get("announcements") or {}
    if ann.get("summary"):
        s = ann["summary"].replace("\n", " ").strip()
        parts.append(f"  重大訊息摘要: {s[:200]}")
    if ann.get("sentiment_breakdown"):
        sb = ann["sentiment_breakdown"]
        parts.append(
            f"  訊息 sentiment: 利多 {sb.get('bullish',0)} / 利空 {sb.get('bearish',0)} / 中性 {sb.get('neutral',0)}"
        )
    if ann.get("key_events"):
        parts.append(f"  事件分類: {', '.join(ann['key_events'])}")

    if not parts:
        return ""
    return "\n【深度分析】\n" + "\n".join(parts) + "\n"


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
                 hits: Dict[str, bool], score: float,
                 deep_analysis: Optional[Dict] = None) -> str:
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

    # 財報日期 / 法說會
    events_block = ""
    try:
        import earnings_calendar
        ev = earnings_calendar.get_stock_events(
            stock_meta.get("stock_id", ""),
            market="US" if is_us else "TW",
        )
        if ev and ev.get("summary"):
            events_block = f"\n【財報行事曆】\n{ev['summary']}\n"
    except Exception:
        pass

    return f"""你是資深的股票分析師，風格務實、強調風險意識。請根據以下資料對股票做完整分析。

【基本資料】
代號: {stock_meta.get('stock_id')}
名稱: {stock_meta.get('name')}
產業: {stock_meta.get('industry')}
市場: {stock_meta.get('market')}

{macro_block}{events_block}【K 線與漲跌】
{_summarize_kline(daily)}

【{_summarize_indicators(ind)}】

【{_summarize_margin(margin)}】

{_summarize_institutional(inst)}

【篩選條件】
{_summarize_hits(hits, score)}

【新聞】
{_summarize_news(news_df)}
{_summarize_deep_analysis(deep_analysis)}
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
            model: str = DEFAULT_MODEL,
            deep_analysis: Optional[Dict] = None) -> Tuple[bool, str]:
    """呼叫 Gemini，回傳 (success, text).

    deep_analysis: 來自 stock_deep_analyzer.get_deep_analysis() 的結果,
                   含 PE 估值 / 籌碼變化 / K 形態 / 重大訊息摘要.
                   會被塞進 prompt 提升分析品質. None = 不附深度資料.
    """
    api_key = get_gemini_key()
    if not api_key:
        return False, "尚未設定 GEMINI_API_KEY"
    try:
        import google.generativeai as genai
    except ImportError:
        return False, "google-generativeai 套件未安裝，請更新 requirements.txt 後重新部署"

    news_df = fetch_stock_news(stock_meta.get("stock_id", ""))
    prompt = build_prompt(stock_meta, daily, ind, inst, margin, news_df, hits, score,
                            deep_analysis=deep_analysis)

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
            safety_settings=SAFETY_SETTINGS,
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
    """對開盤分析的 picks 做 AI 觀點補強 (含國際新聞 / 油價 / Trump 等 sentiment context)."""
    api_key = get_gemini_key()
    if not api_key:
        return False, "尚未設定 GEMINI_API_KEY"
    try:
        import google.generativeai as genai
    except ImportError:
        return False, "google-generativeai 未安裝"

    # 市場情緒指數
    fg = ds.fetch_fear_greed()
    tw_p = ds.fetch_tw_market_pulse() if market == "TW" else None
    macro = []
    if tw_p and tw_p.get("score") is not None:
        macro.append(f"台股情緒指數 {tw_p['score']} ({tw_p.get('rating_zh')})")
    if fg and fg.get("score") is not None:
        macro.append(f"美股 F&G {fg['score']:.0f} ({fg.get('rating')})")
    macro_line = "市場大環境: " + " / ".join(macro) if macro else ""

    # 國際新聞 + 油價 + Trump 等 sentiment context
    news_context = ""
    try:
        import news_sources
        news_context = news_sources.build_news_context(include_trump=True)
    except Exception:
        pass

    is_tw_market = (market == 'TW')
    leading_hint = ""
    if is_tw_market:
        leading_hint = (
            "\n📌 **重要分析提示**：日股 (^N225)、韓股 (KOSPI) 比台股早 1 小時開盤 "
            "(08:00 台北 vs 09:00 台北)。請把 JP/KR 當前走勢當作**台股的 leading indicator**：\n"
            "  - 若 JP/KR 同步走強 → 台股延續可能性高，是區域同步\n"
            "  - 若 JP/KR 走弱但台股獨自走強 → 警告台股獨秀，留意尾盤回吐\n"
            "  - JP/KR 急漲急跌應作為台股盤中操作的提前訊號\n"
        )
    prompt = f"""你是專業 {('台股' if is_tw_market else '美股')}分析師。下面是今日開盤後 30 分鐘的市場狀態。
{leading_hint}
{macro_line}

【今日資金流向 + 動能股清單 (含美股隔夜 + 日韓盤中)】
{picks_summary}

【國際新聞 / 大宗商品 / 政治面 sentiment】
{news_context if news_context else '(本次未取得新聞資料)'}

請用繁體中文以下列結構回應：

## 🌎 國際新聞與大宗商品評估
- 哪些屬於「利多」風險性資產？
- 哪些屬於「利空」風險性資產？
- 油價/美元/殖利率/Trump 言論等是否有特別需要警戒的訊號？
(每點 1-2 句，3-5 點即可)

## 📊 今日大盤判讀
- 今日資金主流是什麼類型？(防禦 / 成長 / 循環 / 科技)
- 是否該追進場 / 等回檔 / 完全觀望?

(每點 1-2 句, 全文 ≤ 600 字)
"""

    try:
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 1500},
            safety_settings=SAFETY_SETTINGS,
        )
        text = (getattr(resp, "text", None) or "").strip()
        return (bool(text), text or "Gemini 無回應")
    except Exception as e:
        return False, f"Gemini 呼叫失敗: {e}"


def analyze_systemic_crash(crash_data: Dict, model: str = DEFAULT_MODEL) -> Tuple[bool, str]:
    """系統性大跌時的 Gemini 觀點 — 明確給「加碼 / 觀望 / 減碼」動作建議.

    crash_data: 來自 index_alerts.check_systemic_crash() 的回傳 dict, 含:
      - triggers: 觸發的標的 list (intraday_pct, two_day_pct 等)
      - context: vix, all_snapshots, macro_news
      - alert_index: 今天第幾次
    """
    api_key = get_gemini_key()
    if not api_key:
        return False, "尚未設定 GEMINI_API_KEY"
    try:
        import google.generativeai as genai
    except ImportError:
        return False, "google-generativeai 未安裝"

    triggers = crash_data.get("triggers", []) or []
    ctx = crash_data.get("context", {}) or {}
    vix = ctx.get("vix")
    all_snaps = ctx.get("all_snapshots", []) or []
    macro_news = ctx.get("macro_news") or "(本次未取得國際新聞)"

    # ===== 整理 trigger 資訊 (給 prompt 看) =====
    trigger_lines = []
    for t in triggers:
        name = t.get("name", "")
        sym = t.get("symbol", "")
        ttype = t.get("trigger_type", "")
        tval = t.get("trigger_value", 0)
        _verb = "漲" if t.get("direction", "down") == "up" else "跌"
        if ttype == "intraday":
            trigger_lines.append(f"  - {name} ({sym}): 盤中{_verb} {tval:+.2f}%")
        elif ttype == "two_day":
            trigger_lines.append(f"  - {name} ({sym}): 連 2 日累計{_verb} {tval:+.2f}%")
    triggers_block = "\n".join(trigger_lines) if trigger_lines else "  (無)"

    # 方向判定 (用戶: 漲跌都推) → 決定 prompt 框架 (大漲/大跌/混合)
    _dirs = {t.get("direction", "down") for t in triggers}
    _is_up = (_dirs == {"up"})
    _is_down = (_dirs == {"down"})

    # ===== 全部標的 snapshot (給全局視野) =====
    snap_lines = []
    for s in all_snaps:
        name = s.get("name", "")
        sym = s.get("symbol", "")
        intraday = s.get("intraday_pct", 0)
        twoday = s.get("two_day_pct")
        twoday_str = f", 2日累計 {twoday:+.2f}%" if twoday is not None else ""
        snap_lines.append(f"  {name} ({sym}): 今日 {intraday:+.2f}%{twoday_str}")
    snap_block = "\n".join(snap_lines) if snap_lines else "(無)"

    vix_line = f"VIX 恐慌指數: {vix:.2f}" if vix is not None else "VIX 恐慌指數: 無法取得"
    # VIX 判讀提示
    if vix is not None:
        if vix >= 30:
            vix_hint = "(>30 → 恐慌, 系統性風險升高)"
        elif vix >= 20:
            vix_hint = "(20-30 → 高波動, 警戒區)"
        else:
            vix_hint = "(<20 → 正常區間, 尚無恐慌)"
        vix_line = f"{vix_line} {vix_hint}"

    if _is_up:
        _situation = "可能的系統性大漲 / 急拉"
        _judge_line = ('這是「系統性轉強 (趨勢動能)」, 還是「事件性急拉 (留意拉高出貨)」? '
                       '給出明確判斷 (二選一), 並以 2-3 點支持判斷的事實.')
        _action_block = (
            "- 追多 (適合於: 趨勢轉強 + 量價配合 + 體質佳)\n"
            "- 觀望 (適合於: 訊號不明 / 等回測確認再進)\n"
            "- 獲利了結 / 不追高 (適合於: 急拉過熱 + 量價背離 + 拉高出貨疑慮)"
        )
    elif _is_down:
        _situation = "可能的系統性大跌"
        _judge_line = ('這是「系統性大跌」, 還是「事件性回檔」? '
                       '給出明確判斷 (二選一), 並以 2-3 點支持判斷的事實.')
        _action_block = (
            "- 加碼 (適合於: 事件性回檔 + 恐慌過頭 + 體質佳)\n"
            "- 觀望 (適合於: 訊號不明 / 等更多 confirmation)\n"
            "- 減碼 (適合於: 系統性風險升高 + 趨勢明顯轉空)"
        )
    else:  # 漲跌互現
        _situation = "盤中劇烈波動 (多空互現)"
        _judge_line = ('這是「多空分歧的震盪」, 還是「系統性轉折」? '
                       '給出明確判斷, 並以 2-3 點支持判斷的事實.')
        _action_block = (
            "- 加碼 / 追多 (體質佳且方向明確時)\n"
            "- 觀望 (訊號分歧時的預設)\n"
            "- 減碼 / 獲利了結 (風險升高或過熱時)"
        )

    prompt = f"""你是務實的台股 / 美股市場策略師, 風格冷靜不煽動。
目前偵測到「{_situation}」訊號, 請依下面資料做快速判斷。

【觸發的標的】
{triggers_block}

【全部監控標的當下狀態】
{snap_block}

【市場恐慌指標】
{vix_line}

【國際新聞 / 大宗商品 / 政治面 sentiment】
{macro_news}

請用繁體中文, 嚴格依下列格式回覆 (每段 2-4 句, 全文 ≤ 450 字):

## 🩺 判斷
{_judge_line}

## 🎯 動作建議
從下列三個動作擇一明確建議, 並說明理由 (1-2 句):
{_action_block}

## 🔭 關鍵觀察點
列出 3-5 個未來 1-3 個交易日的觀察重點 (具體價位 / 指標水準).

## ⚠️ 風險提醒
1 句說明本次判斷可能的最大盲點.

結尾務必加註: 「以上分析僅供參考, 不構成投資建議, 請依個人風險承受度與整體配置自行決策。」"""

    try:
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.3, "max_output_tokens": 1200},
            safety_settings=SAFETY_SETTINGS,
        )
        text = (getattr(resp, "text", None) or "").strip()
        return (bool(text), text or "Gemini 無回應 (可能被安全過濾)")
    except Exception as e:
        return False, f"Gemini 呼叫失敗: {e}"


def analyze_reversal_alerts(reversal_alerts, weak_open_alerts=None,
                            model: str = DEFAULT_MODEL) -> Tuple[bool, str]:
    """盤中反轉 / 開盤即弱強 的 Gemini 快評 (給合併推播用, crash 沒觸發時補上 AI)."""
    api_key = get_gemini_key()
    if not api_key:
        return False, "尚未設定 GEMINI_API_KEY"
    try:
        import google.generativeai as genai
    except ImportError:
        return False, "google-generativeai 未安裝"

    revs = reversal_alerts or []
    wos = weak_open_alerts or []
    if not revs and not wos:
        return False, "no alerts"

    _lines = []
    for r in revs:
        _nm = r.get("name", ""); _sy = r.get("symbol", ""); _t = r.get("type", "")
        if _t == "drawdown":
            _dd = r.get("drawdown_pct", 0) or 0
            _vp = r.get("pct_vs_prior")
            _lines.append(f"{_nm}({_sy}) 從高點回吐 {_dd:+.2f}%"
                          + (f", 今日 {_vp:+.2f}% vs昨收" if _vp is not None else ""))
        elif _t == "rebound":
            _lines.append(f"{_nm}({_sy}) 從低點反彈 {r.get('rebound_pct', 0) or 0:+.2f}%")
        elif _t == "recover":
            _lines.append(f"{_nm}({_sy}) 從跌轉漲 (回升 {r.get('recovery_pp', 0) or 0:+.2f}pp)")
    for w in wos:
        _nm = w.get("name", ""); _sy = w.get("symbol", ""); _t = w.get("type", "")
        _lines.append(f"{_nm}({_sy}) 開盤即{'弱' if _t == 'weak' else '強'} "
                      f"{w.get('pct_vs_open', 0) or 0:+.2f}% vs開盤")
    _block = "\n".join(_lines) if _lines else "(無)"

    prompt = (
        "你是台股 / 美股盤中策略師。以下是盤中指數的反轉 / 開盤即弱強訊號:\n"
        f"{_block}\n\n"
        "用繁體中文精簡回覆 (全文 ≤ 200 字, 不要列數據, 只給結論):\n"
        "## 🩺 盤勢\n盤面轉弱 / 轉強 / 震盪? (1-2 句)\n"
        "## 🎯 動作\n持倉該加碼 / 減碼 / 觀望? (1 句, 可行動)\n"
        "## ⚠️ 注意\n一個關鍵位或風險 (1 句)"
    )
    try:
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.3, "max_output_tokens": 700},
            safety_settings=SAFETY_SETTINGS,
        )
        text = (getattr(resp, "text", None) or "").strip()
        return (bool(text), text or "Gemini 無回應")
    except Exception as e:
        return False, f"Gemini 呼叫失敗: {e}"


def analyze_chart_image(image_bytes: bytes, extra_note: str = "",
                          fg: dict | None = None, market_news: list | None = None,
                          model: str = DEFAULT_MODEL):
    """用 Gemini Vision 分析 K 線圖. 回 (success, text)."""
    api_key = get_gemini_key()
    if not api_key:
        return False, "尚未設定 GEMINI_API_KEY"
    try:
        import google.generativeai as genai
    except ImportError:
        return False, "google-generativeai 套件未安裝"
    prompt = _build_chart_prompt(extra_note=extra_note, fg=fg, market_news=market_news)
    try:
        genai.configure(api_key=api_key)
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            [prompt, {"mime_type": "image/png", "data": image_bytes}],
            generation_config={"temperature": 0.4, "max_output_tokens": 1500},
            safety_settings=SAFETY_SETTINGS,
        )
        text = (getattr(resp, "text", None) or "").strip()
        return (bool(text), text or "Gemini 無回應")
    except Exception as e:
        return False, f"Gemini 呼叫失敗: {e}"
