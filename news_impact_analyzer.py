"""
news_impact_analyzer.py
對 HIGH urgency 新聞事件用 Gemini 分析「對股市影響 + 操作建議」.

設計:
  - 只對 urgency=HIGH 跑 (8-K / 併購 / FDA / CEO 變動 / TW 重大訊息)
  - 1 次 API call 分析所有 HIGH 事件 (省 quota)
  - 失敗 graceful — 回空字串, 主推播流程不受影響

API:
  analyze_news_impact(alerts: List[Dict]) -> str
    回 HTML 格式區塊, 直接拼接到 TG 訊息末尾. 沒 HIGH/Gemini 不可用 → 回 ""
"""
from __future__ import annotations

from typing import Dict, List

import ai_analyzer


def _build_impact_prompt(high_alerts: List[Dict]) -> str:
    lines = [
        "你是股市分析師, 同時看美股/台股/台指期(TX)。分析以下重大新聞。",
        "純文字、繁中、精簡、不要 markdown、不要客套。每句直接給結論, 別贅述。",
        "",
        "逐則 (每則 3 行, 段間用 ━━━ 分隔):",
        "  [市場-代號] 標題重點",
        "  📊 含意+方向: <代表什麼>; 續漲/續跌/震盪, 是否已 priced in",
        "  🔗 受惠股: 🇺🇸美股<代號或「—」> · 🇹🇼台股<代號或「—」> · 💡建議 加碼/持有/出場/觀望/反向",
        "  ※ 受惠股要點名具體個股 (含 ticker/代號); 美股、台股「兩邊都要給」, 沒有才寫「—」。",
        "",
        "最後一段固定:",
        "  🌏 連動",
        "  🇺🇸 美股 偏多/偏空/分歧 · 推薦關注的美股個股(ticker)",
        "  🇹🇼 台股 偏多開/偏空開/震盪 · 受惠或受害個股(代號)",
        "  📉 台指(TX) 偏多/偏空/中性 · 一句理由",
        "",
        "範例:",
        "[US-NVDA] 8-K $50B 庫藏股",
        "📊 買回稀釋 EPS、看好後市; 續漲但盤前恐已 priced in",
        "🔗 受惠股: 🇺🇸 NVDA、AMD、AVGO · 🇹🇼 台積電(2330)、緯穎(6669) · 💡 持有為主拉回加碼",
        "━━━",
        "🌏 連動",
        "🇺🇸 美股 偏多 · 半導體領漲, 留意 NVDA、AMD、AVGO、TSM",
        "🇹🇼 台股 偏多開 · 受惠 AI 伺服器(台積電2330、鴻海2317)",
        "📉 台指(TX) 偏多 · 美半導體強帶動電子權值",
        "",
        "事件:",
    ]
    for i, a in enumerate(high_alerts, 1):
        sym = a.get("symbol", "?")
        market = a.get("market", "")
        title = a.get("title", "")
        src = a.get("source_type", "")
        lines.append(f"{i}. [{market}-{sym}] {src}: {title[:120]}")
    return "\n".join(lines)


def analyze_news_impact(alerts: List[Dict]) -> str:
    """對 HIGH urgency 跑 Gemini 影響分析, 回 HTML 區塊 (TG 直接拼用).
    Graceful: 任何失敗回 "".
    """
    if not alerts:
        return ""
    # 用戶要求: AI 分析從「只有 HIGH 急報」擴到「HIGH 急報 + MED 注意」。
    # 優先序 HIGH > MED; LOW(一般快訊)不進 AI。合併取前 3 則控 Gemini quota + 訊息長度。
    high_alerts = [a for a in alerts if a.get("urgency") == "HIGH"]
    med_alerts = [a for a in alerts if a.get("urgency") == "MED"]
    focus_alerts = (high_alerts + med_alerts)[:3]
    if not focus_alerts:
        return ""
    if not ai_analyzer.gemini_available():
        return ""

    try:
        prompt = _build_impact_prompt(focus_alerts)
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None:
            return ""
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip() if resp else ""
        if not text:
            return ""
    except Exception as e:
        print(f"[news_impact] Gemini failed graceful: {type(e).__name__}: {e}", flush=True)
        return ""

    # 用 _esc 避 HTML tag 破壞 (notifier import 防循環)
    try:
        from notifier import _esc
        text_esc = _esc(text)
    except Exception:
        text_esc = (text.replace("<", "&lt;").replace(">", "&gt;")
                        .replace("&", "&amp;"))

    # 包裝成 TG HTML 區塊 (標頭精簡, 省字給內容)
    return (
        "\n🤖 <b>AI 分析</b>\n\n"
        f"{text_esc}\n"
    )
