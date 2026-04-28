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
def _fmt_num(v, suffix: str = "") -> str:
    """安全格式化數字，None / NaN 顯示 —"""
    try:
        if v is None:
            return "—"
        import math
        if isinstance(v, float) and math.isnan(v):
            return "—"
        if isinstance(v, (int, float)):
            return f"{v:,.2f}{suffix}" if isinstance(v, float) else f"{v:,}{suffix}"
    except Exception:
        pass
    return str(v) if v not in (None, "") else "—"


def fmt_tw_combined(combined_df, latest_date_str: str, market_label: str, max_n: int = 25) -> str:
    """台股篩選結果訊息（含現價、今日%、投信張數、投本比、量比）。"""
    if combined_df is None or combined_df.empty:
        return f"<b>📊 {market_label} 台股篩選 ({latest_date_str})</b>\n今日無符合條件的標的。"

    # 表頭
    n_show = min(max_n, len(combined_df))
    lines = [
        f"<b>📊 {market_label} 台股篩選 ({latest_date_str})</b>",
        f"共 <b>{len(combined_df)}</b> 檔符合，顯示前 {n_show} 檔",
        "",
    ]

    for i, row in combined_df.head(max_n).iterrows():
        sid = row.get("stock_id", "")
        name = row.get("stock_name", "")
        hits = row.get("hit_count", 0)
        labels = row.get("hits_label", "")

        # 第 1 行：代號 名稱 (n項)
        lines.append(f"{i+1}. <code>{sid}</code> {name} <b>({hits}項)</b>")

        # 第 2 行：價量
        price_part = []
        if "現價" in combined_df.columns and row.get("現價") is not None:
            price_part.append(f"💰{_fmt_num(row.get('現價'))}")
        if "今日%" in combined_df.columns and row.get("今日%") is not None:
            v = row.get("今日%")
            arrow = "🔺" if (isinstance(v, (int, float)) and v > 0) else ("🔻" if (isinstance(v, (int, float)) and v < 0) else "▪")
            price_part.append(f"{arrow}{_fmt_num(v, '%')}")
        if "量比" in combined_df.columns and row.get("量比") is not None:
            price_part.append(f"📊量比{_fmt_num(row.get('量比'), 'x')}")
        if price_part:
            lines.append(f"   {' · '.join(price_part)}")

        # 第 3 行：法人
        inst_part = []
        if "投信今日(張)" in combined_df.columns and row.get("投信今日(張)") is not None:
            v = row.get("投信今日(張)")
            sign = "+" if isinstance(v, (int, float)) and v > 0 else ""
            inst_part.append(f"投信今日 {sign}{_fmt_num(v)}張")
        if "投信5日(張)" in combined_df.columns and row.get("投信5日(張)") is not None:
            v = row.get("投信5日(張)")
            sign = "+" if isinstance(v, (int, float)) and v > 0 else ""
            inst_part.append(f"5日累計 {sign}{_fmt_num(v)}張")
        if "投本比%" in combined_df.columns and row.get("投本比%") is not None:
            inst_part.append(f"投本比 {_fmt_num(row.get('投本比%'), '%')}")
        if inst_part:
            lines.append(f"   🏛️ {' · '.join(inst_part)}")

        # 第 4 行：命中條件
        lines.append(f"   ✅ {labels}")
        lines.append("")

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


def fmt_tw_open_picks(data: dict, ai_text: str = "") -> str:
    """台股開盤後 30 分推播."""
    if data.get("error"):
        return f"🇹🇼 台股開盤分析：{data['error']}"
    lines = [f"<b>🇹🇼 台股開盤後 30 分鐘 · 資金流向</b>"]
    themes_df = data.get("themes")
    if themes_df is not None and not themes_df.empty:
        lines.append("")
        for _, row in themes_df.iterrows():
            name = row.get("題材")
            avg = row.get("平均%")
            up = int(row.get("上漲家數", 0))
            n = int(row.get("樣本數", 0))
            lines.append(f"🔥 <b>{name}</b>  平均 {avg}% · 上漲 {up}/{n}")

    picks = data.get("picks", [])
    if picks:
        lines.append("")
        lines.append("<b>📌 各族群動能潛在股 (3 檔)</b>")
        for p in picks:
            theme = p["theme"]
            stocks = p["stocks"]
            if stocks is None or (hasattr(stocks, 'empty') and stocks.empty):
                continue
            lines.append(f"\n<b>[{theme}]</b>")
            for _, s in stocks.iterrows():
                sid = s.get("stock_id", "")
                nm = s.get("stock_name", "")
                today = s.get("今日%")
                ratio = s.get("量比")
                five = s.get("5日%")
                lines.append(
                    f"  • <code>{sid}</code> {nm}  今日 {today}% · 量比 {ratio}x · 5d {five}%"
                )

    if ai_text:
        lines.append("")
        lines.append("<b>🤖 AI 觀點</b>")
        # 簡化 markdown
        for line in ai_text.split("\n"):
            s = line.strip()
            if s.startswith("## "):
                lines.append(f"<b>{s[3:]}</b>")
            else:
                lines.append(line)

    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3900] + "\n…(節錄)"
    return out


def fmt_us_open_picks(data: dict, ai_text: str = "") -> str:
    """美股開盤後 30 分推播."""
    if data.get("error"):
        return f"🇺🇸 美股開盤分析：{data['error']}"
    lines = [f"<b>🇺🇸 美股開盤後 30 分鐘 · 資金流向</b>"]
    sectors = data.get("sectors")
    if sectors is not None and not sectors.empty:
        lines.append("")
        for _, row in sectors.iterrows():
            sym = row.get("symbol")
            sname = row.get("sector", "")
            r1 = row.get("1d_%")
            lines.append(f"🔥 <b>{sym} {sname}</b>  1d {r1:.2f}%")

    sector_picks = data.get("sector_picks", [])
    if sector_picks:
        lines.append("")
        lines.append("<b>📌 各板塊動能潛在股 (3 檔)</b>")
        for sp in sector_picks:
            sec = sp["sector"]
            stocks = sp["stocks"]
            if stocks is None or stocks.empty:
                continue
            lines.append(f"\n<b>[{sec}]</b>")
            for _, s in stocks.iterrows():
                sym = s.get("symbol", "")
                today = s.get("今日%")
                ratio = s.get("量比")
                twenty = s.get("20日%")
                lines.append(
                    f"  • <code>{sym}</code>  今日 {today}% · 量比 {ratio}x · 20d {twenty}%"
                )

    growth = data.get("growth")
    if growth is not None and not growth.empty:
        lines.append("")
        lines.append("<b>🚀 成長動能極強 / 近期 IPO Top 5</b>")
        for _, s in growth.head(5).iterrows():
            sym = s.get("symbol", "")
            today = s.get("今日%")
            twenty = s.get("20日%")
            ratio = s.get("量比")
            score = s.get("growth_score")
            lines.append(
                f"  • <code>{sym}</code>  今日 {today}% · 20d {twenty}% · 量比 {ratio}x · {score}/10"
            )

    if ai_text:
        lines.append("")
        lines.append("<b>🤖 AI 觀點</b>")
        for line in ai_text.split("\n"):
            s = line.strip()
            if s.startswith("## "):
                lines.append(f"<b>{s[3:]}</b>")
            else:
                lines.append(line)

    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3900] + "\n…(節錄)"
    return out


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


def fmt_watchlist_alert(stock_id: str, name: str, hits: list, latest_date: str,
                         row: dict | None = None) -> str:
    """watchlist 命中通知，含詳細數值。"""
    head = f"<b>🔔 自選股警報 — {stock_id} {name}</b>\n資料日期: {latest_date}"
    body = [head]
    if row:
        if row.get("現價") is not None:
            arrow = ""
            if isinstance(row.get("今日%"), (int, float)):
                arrow = "🔺" if row["今日%"] > 0 else ("🔻" if row["今日%"] < 0 else "")
            body.append(f"現價 {_fmt_num(row.get('現價'))} {arrow}{_fmt_num(row.get('今日%'), '%')}")
        if row.get("量比") is not None:
            body.append(f"量比 {_fmt_num(row.get('量比'), 'x')}")
        inst_parts = []
        if row.get("投信今日(張)") is not None:
            v = row.get("投信今日(張)")
            sign = "+" if isinstance(v, (int, float)) and v > 0 else ""
            inst_parts.append(f"投信今日 {sign}{_fmt_num(v)}張")
        if row.get("投信5日(張)") is not None:
            v = row.get("投信5日(張)")
            sign = "+" if isinstance(v, (int, float)) and v > 0 else ""
            inst_parts.append(f"5日累計 {sign}{_fmt_num(v)}張")
        if row.get("投本比%") is not None:
            inst_parts.append(f"投本比 {_fmt_num(row.get('投本比%'), '%')}")
        if inst_parts:
            body.append("🏛️ " + " · ".join(inst_parts))
    body.append("✅ 命中: " + ", ".join(hits))
    return "\n".join(body)


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
