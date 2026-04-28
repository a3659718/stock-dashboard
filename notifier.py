"""
notifier.py
Telegram 通知封裝。同步呼叫 Bot HTTP API (sendMessage)，避免 streamlit 中跑 asyncio。
"""

from __future__ import annotations

from typing import List, Optional

import requests
import streamlit as st

import data_sources as ds


def _bot_token() -> str:
    return ds._secret("TELEGRAM_BOT_TOKEN")


def _chat_id() -> str:
    return ds._secret("TELEGRAM_CHAT_ID")


def is_configured() -> bool:
    return bool(_bot_token() and _chat_id())


def send_message(text: str, disable_preview: bool = True) -> tuple[bool, str]:
    """直接呼叫 Bot API。回傳 (成功, 訊息)."""
    token = _bot_token()
    chat_id = _chat_id()
    if not (token and chat_id):
        return False, "尚未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code == 200:
            return True, "已送出"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# 訊息模板
# ---------------------------------------------------------------------------
def fmt_tw_combined(combined_df, latest_date_str: str, market_label: str) -> str:
    """台股篩選結果訊息."""
    if combined_df is None or combined_df.empty:
        return f"<b>📊 {market_label} 台股篩選 ({latest_date_str})</b>\n今日無符合條件的標的。"
    lines = [f"<b>📊 {market_label} 台股篩選 ({latest_date_str})</b>", ""]
    for i, row in combined_df.head(40).iterrows():
        lines.append(
            f"{i+1}. <code>{row['stock_id']}</code> {row.get('stock_name','')} ({row['hit_count']}項) — {row['hits_label']}"
        )
    return "\n".join(lines)


def fmt_us_top_picks(df, fg: dict) -> str:
    if df is None or df.empty:
        return "🇺🇸 美股 Top 5 推薦：今日無符合篩選條件的標的。"
    score = fg.get("score") if fg else None
    rating = fg.get("rating") if fg else None
    fg_line = f"恐慌指數 {round(score,1)} ({rating})" if score else "恐慌指數 N/A"
    lines = [f"<b>🇺🇸 美股 Top 5 推薦</b> · {fg_line}", ""]
    for i, row in df.head(5).iterrows():
        lines.append(
            f"{i+1}. <code>{row['symbol']}</code>  日 {row.get('daily_%')}% / 20d {row.get('20d_%')}% · 分數 {row['score']}"
            + (f"\n   題材: {row['題材']}" if row.get("題材") else "")
        )
    return "\n".join(lines)


def fmt_strong_sectors(sectors_df) -> str:
    if sectors_df is None or sectors_df.empty:
        return "🇹🇼 即時強勢族群：尚未取得即時資料。"
    lines = ["<b>🇹🇼 即時強勢族群 Top 5</b>", ""]
    for _, row in sectors_df.head(5).iterrows():
        lines.append(
            f"• {row.iloc[0]}  平均 {row['avg_change']:.2f}%  上漲 {int(row['up_count'])}/{int(row['n'])}"
        )
    return "\n".join(lines)


def fmt_ai_analysis(stock_id: str, name: str, ai_text: str) -> str:
    """AI 個股分析推送格式. Telegram 訊息上限 4096 字，必要時截斷."""
    head = f"<b>🤖 AI 深度分析 — {stock_id} {name}</b>\n"
    body = ai_text
    # 將 markdown 標題行轉成粗體
    out_lines = []
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("## "):
            out_lines.append(f"<b>{s[3:]}</b>")
        else:
            out_lines.append(line)
    full = head + "\n".join(out_lines)
    if len(full) > 3900:
        full = full[:3900] + "\n…(節錄)"
    return full


def fmt_stealth_picks(stealth_df, hot_themes_df=None) -> str:
    """潛伏題材股推送格式."""
    if stealth_df is None or stealth_df.empty:
        return "🌱 潛伏題材股：今日無符合條件的標的。"
    lines = ["<b>🌱 潛伏題材股 (族群熱、本身還沒大漲)</b>"]
    if hot_themes_df is not None and not hot_themes_df.empty:
        themes = "、".join(hot_themes_df["題材"].head(3).tolist())
        lines.append(f"<i>熱門題材: {themes}</i>")
    lines.append("")
    for i, row in stealth_df.head(15).iterrows():
        sid = row.get("stock_id", "")
        name = row.get("stock_name", "")
        theme = row.get("題材", "")
        today = row.get("今日%", "—")
        five = row.get("5日%", "—")
        ratio = row.get("量比", "—")
        lines.append(f"{i+1}. <code>{sid}</code> {name}  [{theme}]")
        lines.append(f"   今日 {today}% / 5d {five}% / 量比 {ratio}x")
    return "\n".join(lines)


def fmt_growth_picks(picks_df) -> str:
    if picks_df is None or picks_df.empty:
        return "🌱 成長動能 Top 10：今日無符合條件的標的。"
    lines = ["<b>🌱 成長動能 Top 10 (消息面 + K 線健康度)</b>", ""]
    for i, r in picks_df.iterrows():
        lines.append(f"{i+1}. <code>{r['代號']}</code> {r['名稱']} · {r.get('題材','')} · {r['score']}/10")
        if r.get("理由"):
            lines.append(f"   {r['理由']}")
    return "\n".join(lines)


def fmt_watchlist_alert(stock_id: str, name: str, hits: list, latest_date: str) -> str:
    """watchlist 命中通知."""
    line = f"<b>🔔 自選股警報 — {stock_id} {name}</b>\n資料日期: {latest_date}\n命中: {', '.join(hits)}"
    return line


def fmt_tw_pulse_alert(pulse: dict, threshold_low: int = 25, threshold_high: int = 75) -> Optional[str]:
    if not pulse or pulse.get("score") is None:
        return None
    s = pulse["score"]
    if s <= threshold_low:
        return (f"⚠️ <b>台股市場極度恐慌</b>\n台股情緒指數: {s} ({pulse.get('rating_zh')})\n"
                f"加權: {pulse['raw'].get('TWII')} · 5日 {pulse['raw'].get('5日%')}% · "
                f"距 MA60 {pulse['raw'].get('距 MA60 %')}%\n"
                "歷史經驗為逢低布局訊號，仍須個股控管風險。")
    if s >= threshold_high:
        return (f"⚠️ <b>台股市場極度貪婪</b>\n台股情緒指數: {s} ({pulse.get('rating_zh')})\n"
                f"加權: {pulse['raw'].get('TWII')} · 5日 {pulse['raw'].get('5日%')}% · "
                f"距 MA60 {pulse['raw'].get('距 MA60 %')}%\n"
                "注意風控、避免追高。")
    return None


def fmt_fear_greed_alert(fg: dict, threshold_low: int = 25, threshold_high: int = 75) -> Optional[str]:
    if not fg or fg.get("score") is None:
        return None
    s = fg["score"]
    if s <= threshold_low:
        return f"⚠️ <b>市場極度恐慌</b>\nFear & Greed 指數: {round(s,1)} ({fg.get('rating')})\n通常為逢低布局訊號，請保持紀律。"
    if s >= threshold_high:
        return f"⚠️ <b>市場極度貪婪</b>\nFear & Greed 指數: {round(s,1)} ({fg.get('rating')})\n注意風控、避免追高。"
    return None
