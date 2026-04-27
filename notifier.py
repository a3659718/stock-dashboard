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


def fmt_fear_greed_alert(fg: dict, threshold_low: int = 25, threshold_high: int = 75) -> Optional[str]:
    if not fg or fg.get("score") is None:
        return None
    s = fg["score"]
    if s <= threshold_low:
        return f"⚠️ <b>市場極度恐慌</b>\nFear & Greed 指數: {round(s,1)} ({fg.get('rating')})\n通常為逢低布局訊號，請保持紀律。"
    if s >= threshold_high:
        return f"⚠️ <b>市場極度貪婪</b>\nFear & Greed 指數: {round(s,1)} ({fg.get('rating')})\n注意風控、避免追高。"
    return None
