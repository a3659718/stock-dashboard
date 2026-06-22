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
        "你是專業股市分析師, 同時看美股、台股與台指期 (TX)。以下是剛收到的重大新聞事件。",
        "請用純文字、繁體中文、精簡作答 (不要 markdown 標頭, 不要前後客套)。分兩部分:",
        "",
        "【逐則分析】每則一段, 段與段之間用 ━━━ 分隔:",
        "  第一行: [市場-代號] 標題重點",
        "  📊 含意: 這則新聞實際代表什麼 (20 字內)",
        "  📈 方向: 續漲 / 續跌 / 震盪 — 並說短線還有沒有空間、是否已 priced in",
        "  🔗 台股對應: 點名對應的台股「受惠股」或「受害股」(代號+名稱, 可含族群);",
        "             沒有明顯對應就寫「無明顯個股」。美股供應鏈消息盡量對到台廠。",
        "  💡 建議: 加碼 / 持有 / 出場 / 觀望 / 反向 之一, 給一句可操作的話",
        "",
        "【🌏 對台股/台指連動】最後務必加這一段 (綜合上述所有消息):",
        "  🇺🇸 美股: 整體偏多 / 偏空 / 分歧 + 主導族群",
        "  🇹🇼 台股: 明早偏多開 / 偏空開 / 震盪 + 受惠族群與個股 / 受害族群與個股",
        "  📉 台指期(TX): 偏多 / 偏空 / 中性 + 一句理由 (日夜盤連動)",
        "",
        "範例:",
        "[US-NVDA] 8-K: $50B 庫藏股",
        "📊 含意: 買回稀釋 EPS、公司看好後市",
        "📈 方向: 續漲, 但盤前可能已 priced in, 追高留意",
        "🔗 台股對應: 受惠股 台積電(2330)、緯穎(6669)、AI 伺服器供應鏈",
        "💡 建議: 持有為主, 拉回承接再加碼",
        "━━━",
        "🌏 對台股/台指連動",
        "🇺🇸 美股: 偏多, 半導體領漲",
        "🇹🇼 台股: 偏多開; 受惠 AI 伺服器/半導體 (台積電 2330、鴻海 2317), 散熱留意跟漲",
        "📉 台指期(TX): 偏多, 美半導體強帶動電子權值",
        "",
        "現在分析以下事件:",
        "",
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
    high_alerts = [a for a in alerts if a.get("urgency") == "HIGH"]
    if not high_alerts:
        return ""
    if not ai_analyzer.gemini_available():
        return ""

    # 限制最多 5 則 (避免 prompt 爆量 + Gemini quota)
    high_alerts = high_alerts[:5]

    try:
        prompt = _build_impact_prompt(high_alerts)
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

    # 包裝成 TG HTML 區塊
    return (
        "\n━━━━━━━ 🤖 AI 影響分析 ━━━━━━━\n"
        "<i>(Gemini 對 HIGH 事件的含意 / 續漲續跌方向 / 美股·台股·台指建議)</i>\n\n"
        f"{text_esc}\n"
    )
