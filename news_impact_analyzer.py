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
        "你是專業股市分析師. 以下是剛收到的重大新聞事件, 每則請判斷:",
        "  1. 對該股短期 (1-3 天) 股價影響: 大利多 / 小利多 / 中性 / 小利空 / 大利空",
        "  2. 對相關族群/大盤的連動效應",
        "  3. 操作建議: 加碼 / 持有 / 出場 / 觀望 / 反向操作",
        "",
        "輸出格式 (純文字, 每則用 ━━━ 分隔, 中文簡短回覆, 每點 30 字內):",
        "",
        "範例:",
        "[NVDA] 8-K: $50B 庫藏股",
        "📊 影響: 大利多 (買回稀釋 EPS 提升)",
        "🌐 連動: 半導體族群同步; SOX 可能跟漲",
        "💡 建議: 持有加碼; 留意盤前是否跳空已 priced in",
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
    lines.append("")
    lines.append("⚠️ 只回分析內容, 不要 markdown 標頭, 不要前後說明.")
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
        "<i>(對上述 HIGH urgency 事件的 Gemini 評估)</i>\n\n"
        f"{text_esc}\n"
    )
