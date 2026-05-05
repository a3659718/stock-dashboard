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
        return f"<b>{market_label} 台股篩選 ({latest_date_str})</b>\n今日無符合條件的標的。"

    # 表頭
    n_show = min(max_n, len(combined_df))
    lines = [
        f"<b>{market_label} 台股篩選 ({latest_date_str})</b>",
        f"共 <b>{len(combined_df)}</b> 檔符合，顯示前 {n_show} 檔",
        "",
    ]

    def _is_meaningful(v) -> bool:
        """判斷欄位是否有意義 — 0 / NaN / None / '' 視為「沒命中」, 不顯示."""
        if v is None or v == "":
            return False
        try:
            f = float(v)
            if f != f:  # NaN
                return False
            if f == 0:
                return False
            return True
        except (TypeError, ValueError):
            return bool(str(v).strip())

    for i, row in combined_df.head(max_n).iterrows():
        sid = row.get("stock_id", "")
        name = row.get("stock_name", "")
        hits = row.get("hit_count", 0)
        labels = row.get("hits_label", "")

        # 第 1 行：代號 名稱 (n項)
        lines.append(f"{i+1}. <b><code>{sid}</code></b> {name} <b>({hits}項)</b>")

        # 第 2 行：價量 — 只顯示有意義的欄位
        price_part = []
        if _is_meaningful(row.get("現價")):
            price_part.append(f"{_fmt_num(row.get('現價'))}")
        if _is_meaningful(row.get("今日%")):
            v = row.get("今日%")
            sign = "+" if isinstance(v, (int, float)) and v > 0 else ""
            price_part.append(f"{sign}{_fmt_num(v, '%')}")
        if _is_meaningful(row.get("量比")):
            price_part.append(f"量比{_fmt_num(row.get('量比'), 'x')}")
        if price_part:
            lines.append(f"   {' · '.join(price_part)}")

        # 第 3 行：法人 — 只顯示有買賣超的
        inst_part = []
        if _is_meaningful(row.get("投信今日(張)")):
            v = row.get("投信今日(張)")
            sign = "+" if isinstance(v, (int, float)) and v > 0 else ""
            inst_part.append(f"投信今日 {sign}{_fmt_num(v)}張")
        if _is_meaningful(row.get("投信5日(張)")):
            v = row.get("投信5日(張)")
            sign = "+" if isinstance(v, (int, float)) and v > 0 else ""
            inst_part.append(f"5日累計 {sign}{_fmt_num(v)}張")
        if _is_meaningful(row.get("投本比%")):
            inst_part.append(f"投本比 {_fmt_num(row.get('投本比%'), '%')}")
        if inst_part:
            lines.append(f"   {' · '.join(inst_part)}")

        # 第 4 行：命中條件 (一定顯示, 因為這是篩選的核心)
        if labels:
            lines.append(f"   {labels}")
        lines.append("")

    return "\n".join(lines)


def fmt_us_top_picks(df, fg: dict) -> str:
    if df is None or df.empty:
        return "美股 Top 5 推薦：今日無符合篩選條件的標的。"
    score = fg.get("score") if fg else None
    rating = fg.get("rating") if fg else None
    fg_line = f"恐慌指數 {round(score,1)} ({rating})" if score else "恐慌指數 N/A"
    lines = [f"<b>美股 Top 5 推薦</b> · {fg_line}", ""]
    for i, row in df.head(5).iterrows():
        lines.append(
            f"{i+1}. <b><code>{row['symbol']}</code></b>  日 {row.get('daily_%')}% / 20d {row.get('20d_%')}% · 分數 {row['score']}"
            + (f"\n   題材: {row['題材']}" if row.get("題材") else "")
        )
    return "\n".join(lines)


def fmt_strong_sectors(sectors_df) -> str:
    if sectors_df is None or sectors_df.empty:
        return "即時強勢族群：尚未取得即時資料。"
    lines = ["<b>即時強勢族群 Top 5</b>", ""]
    for _, row in sectors_df.head(5).iterrows():
        lines.append(
            f"• {row.iloc[0]}  平均 {row['avg_change']:.2f}%  上漲 {int(row['up_count'])}/{int(row['n'])}"
        )
    return "\n".join(lines)


def _fmt_prediction_block(prediction: dict, accuracy: dict) -> list:
    """預測 + 準確率區塊."""
    if not prediction or prediction.get("error"):
        return []
    out = ["", f"<b>大盤盤型預測: {prediction.get('pattern','—')} ({prediction.get('confidence','')}信心)</b>"]
    out.append(f"   偏向: {prediction.get('bias','—')}")
    raw_parts = []
    if prediction.get("gap_pct") is not None:
        raw_parts.append(f"開盤跳空 {prediction['gap_pct']:+.2f}%")
    if prediction.get("drift_pct") is not None:
        raw_parts.append(f"30 分鐘走勢 {prediction['drift_pct']:+.2f}%")
    if prediction.get("vol_ratio") is not None:
        raw_parts.append(f"量比 {prediction['vol_ratio']:.1f}x")
    if raw_parts:
        out.append(f"   {' · '.join(raw_parts)}")
    if prediction.get("explanation"):
        out.append(f"   <i>{prediction['explanation']}</i>")
    if accuracy and accuracy.get("n"):
        out.append(f"   過去 30 天準確率: <b>{accuracy['accuracy_pct']}%</b> ({accuracy['correct']}/{accuracy['n']} 次)")
    return out


def _fmt_laggards_block(laggards: dict, laggards_ai: dict, market: str = "TW") -> list:
    """強勢族群落後股 + AI 跟漲機會分析 (簡潔風格).
    laggards: {theme: {theme_avg, leaders, laggards}}
    laggards_ai: {stock_id: {chance, reason}}
    """
    if not laggards:
        return []

    out = ["", "------ 跟漲機會 (族群熱、個股還沒跟漲) ------"]

    # 排序: chance=高 在前
    chance_rank = {"高": 0, "中": 1, "低": 2}

    for theme, info in laggards.items():
        theme_avg = info.get("theme_avg", 0)
        lag_df = info.get("laggards")
        if lag_df is None or (hasattr(lag_df, "empty") and lag_df.empty):
            continue

        # 蒐集所有 lag 加上 AI 分析
        rows_with_ai = []
        for _, r in lag_df.iterrows():
            sid = str(r.get("stock_id") or r.get("symbol") or "")
            ai = laggards_ai.get(sid, {})
            rows_with_ai.append({
                "sid": sid,
                "name": r.get("stock_name", "") or "",
                "today_pct": r.get("今日%"),
                "ratio": r.get("量比"),
                "chance": ai.get("chance", "—"),
                "reason": ai.get("reason", ""),
            })
        # 按 chance 排序
        rows_with_ai.sort(key=lambda x: chance_rank.get(x["chance"], 9))

        out.append("")
        out.append(f"<b>[{theme}] 族群均漲 +{theme_avg}%</b>")
        for r in rows_with_ai[:4]:
            sid = r["sid"]
            nm = r["name"]
            tp = r["today_pct"]
            ratio = r["ratio"]
            chance = r["chance"]
            reason = r["reason"]

            # 跟漲機會用文字標 (少 emoji)
            chance_label = {
                "高": "<b>高</b>",
                "中": "中",
                "低": "低",
            }.get(chance, "—")

            out.append(f"  <b><code>{sid}</code></b> {nm}  今日 {tp}% / 量比 {ratio}x")
            out.append(f"     跟漲機會: {chance_label}")
            if reason:
                out.append(f"     {reason}")
    return out


def _fmt_asia_markets_block(asia: dict) -> list:
    """日股 / 韓股 / 港股 / 上證 摘要 + 事件提醒."""
    if not asia:
        return []
    out = []
    snapshot = asia.get("snapshot", [])
    events = asia.get("events", [])

    if snapshot:
        out.append("")
        out.append("<b>亞洲鄰近市場</b>")
        for s in snapshot:
            country = s.get("country", "")
            name = s.get("market", "")
            last = s.get("last", 0)
            dp = s.get("daily_pct", 0)
            arrow = "+" if dp > 0 else ("-" if dp < 0 else "▪")
            out.append(f"  {country} {name}: {last:,.0f}  {arrow}{dp:+.2f}%")

    if events:
        out.append("")
        out.append("<b>亞洲市場事件</b>")
        # 依 severity 排序
        sev_order = {"high": 0, "medium": 1, "low": 2}
        events_sorted = sorted(events, key=lambda e: sev_order.get(e.get("severity", "low"), 9))
        for ev in events_sorted[:6]:
            country = ev.get("country", "")
            name = ev.get("market", "")
            event_name = ev.get("event", "")
            msg = ev.get("msg", "")
            severity_icon = "🚨" if ev.get("severity") == "high" else ("⚠️" if ev.get("severity") == "medium" else "")
            out.append(f"  {severity_icon} {country} {name} <b>[{event_name}]</b> {msg}")
    return out


def _fmt_external_signals_block() -> list:
    """油價 + macro 指標摘要 (簡短版，TG 用)."""
    try:
        import news_sources
    except ImportError:
        return []
    out = []
    oil = news_sources.fetch_oil_signal()
    if oil:
        out.append("")
        out.append(f"<b>WTI 油價: ${oil['price']} ({oil['pct_5d']:+.1f}% 5d)</b>")
        out.append(f"   {oil['signal']}")
    macro = news_sources.fetch_macro_indicators()
    if macro:
        parts = []
        if "美元指數" in macro:
            parts.append(f"DXY {macro['美元指數']['value']} ({macro['美元指數']['pct_5d']:+.2f}%)")
        if "10年美債殖利率" in macro:
            parts.append(f"10Y {macro['10年美債殖利率']['value']}% ({macro['10年美債殖利率']['pct_5d']:+.2f}%)")
        if "VIX" in macro:
            parts.append(f"VIX {macro['VIX']['value']} ({macro['VIX']['pct_5d']:+.1f}%)")
        if "BTC" in macro:
            parts.append(f"BTC ${macro['BTC']['value']:,.0f} ({macro['BTC']['pct_5d']:+.1f}%)")
        if parts:
            out.append(f"<i>{' · '.join(parts)}</i>")
    # Trump 最近一條
    trumps = news_sources.fetch_trump_truth_social(max_items=2)
    if trumps:
        out.append("")
        out.append("<b>Trump 最新言論</b>")
        for t in trumps[:1]:
            text = t.get("text", "")
            if len(text) > 220:
                text = text[:220] + "…"
            out.append(f"   {text}")
    return out


def fmt_tw_open_picks(data: dict, ai_text: str = "") -> str:
    """台股開盤後 30 分推播."""
    if data.get("error"):
        return f"台股開盤分析：{data['error']}"
    lines = [f"<b>台股開盤後 30 分鐘 · 資金流向</b>"]

    # 加權指數即時 (盤中)
    twii = data.get("twii") or {}
    if twii and twii.get("current") is not None and twii.get("change_pct") is not None:
        cur = twii["current"]
        pct = twii["change_pct"]
        pts = twii.get("change_pts", 0) or 0
        prev = twii.get("prev_close")
        op = twii.get("today_open")
        hi = twii.get("day_high")
        lo = twii.get("day_low")
        sign = "+" if pct > 0 else ""
        sign_p = "+" if pts > 0 else ""
        direction = "紅" if pct > 0 else ("黑" if pct < 0 else "平")
        lines.append("")
        lines.append(f"<b>加權指數</b>  {cur:,.2f}  <b>{sign_p}{pts} 點 ({sign}{pct:.2f}%)</b>  {direction}盤")
        sub_parts = []
        if op is not None:
            sub_parts.append(f"開 {op:,.2f}")
        if hi is not None:
            sub_parts.append(f"高 {hi:,.2f}")
        if lo is not None:
            sub_parts.append(f"低 {lo:,.2f}")
        if prev is not None:
            sub_parts.append(f"昨收 {prev:,.2f}")
        if sub_parts:
            lines.append("  " + " · ".join(sub_parts))

    # 大盤預測
    lines.extend(_fmt_prediction_block(data.get("prediction"), data.get("accuracy")))
    # 美股隔夜行情 (給 reference)
    us_overnight = data.get("us_overnight") or {}
    if us_overnight:
        spy = us_overnight.get("SPY", {})
        qqq = us_overnight.get("QQQ", {})
        dia = us_overnight.get("DIA", {})
        if any([spy, qqq, dia]):
            lines.append("")
            lines.append("------ 美股隔夜行情 (參考) ------")
            parts = []
            if spy.get("pct") is not None:
                parts.append(f"SPY {spy['pct']:+.2f}%")
            if qqq.get("pct") is not None:
                parts.append(f"QQQ {qqq['pct']:+.2f}%")
            if dia.get("pct") is not None:
                parts.append(f"DIA {dia['pct']:+.2f}%")
            if parts:
                lines.append("  " + " · ".join(parts))
            us_sectors = us_overnight.get("sectors")
            if us_sectors is not None and not us_sectors.empty:
                top3 = us_sectors.head(3)
                bot1 = us_sectors.tail(1)
                top_str = "、".join(f"{r['symbol']} {r.get('1d_%', 0):+.2f}%" for _, r in top3.iterrows())
                bot_str = ", ".join(f"{r['symbol']} {r.get('1d_%', 0):+.2f}%" for _, r in bot1.iterrows())
                lines.append(f"  領漲: {top_str}")
                lines.append(f"  落後: {bot_str}")
    # 亞洲鄰近市場
    lines.extend(_fmt_asia_markets_block(data.get("asia") or {}))
    # 國際訊號
    lines.extend(_fmt_external_signals_block())
    themes_df = data.get("themes")
    if themes_df is not None and not themes_df.empty:
        lines.append("")
        for _, row in themes_df.iterrows():
            name = row.get("題材")
            avg = row.get("平均%")
            up = int(row.get("上漲家數", 0))
            n = int(row.get("樣本數", 0))
            lines.append(f"<b>{name}</b>  平均 {avg}% · 上漲 {up}/{n}")

    picks = data.get("picks", [])
    catalysts = data.get("catalysts", {})
    events = data.get("events", {})
    chips = data.get("chips", {})
    if picks:
        lines.append("")
        lines.append("<b>各族群動能潛在股 (3 檔)</b>")
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
                    f"  • <b><code>{sid}</code></b> {nm}  今日 {today}% · 量比 {ratio}x · 5d {five}%"
                )
                cat = catalysts.get(str(sid))
                if cat:
                    lines.append(f"    催化劑: {cat}")
                ev = events.get(str(sid))
                if ev and ev.get("summary") and ev["summary"] != "—":
                    lines.append(f"    財報: {ev['summary']}")
                ch = chips.get(str(sid))
                if ch:
                    direction = ch.get("direction", "")
                    prob = ch.get("change_prob", 0)
                    rec = ch.get("recommendation", "")
                    reason = ch.get("reason", "")
                    line = f"    主力{direction} · 換手{prob}% · 建議: <b>{rec}</b>"
                    lines.append(line)
                    if reason:
                        lines.append(f"       {reason}")

    # 落後股 / 跟漲機會
    lines.extend(_fmt_laggards_block(
        data.get("laggards") or {},
        data.get("laggards_ai") or {},
        market="TW",
    ))

    if ai_text:
        lines.append("")
        lines.append("<b>AI 觀點</b>")
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


def fmt_monitor_alerts(watchlist_alerts: list, index_alerts: list, crypto_alerts: list) -> str:
    """盤中監控警報推播 (極簡風格, 無 emoji)."""
    if not (watchlist_alerts or index_alerts or crypto_alerts):
        return ""

    lines = ["<b>盤中警報</b>", ""]

    if watchlist_alerts:
        lines.append("<b>自選股</b>")
        for a in watchlist_alerts:
            sid = a.get("stock_id", "")
            name = a.get("name", "")
            cur = a.get("current", 0)
            d = a.get("direction", "")
            thr = a.get("threshold", 0)
            anchor_label = a.get("primary_anchor_label", "")
            anchor_price = a.get("primary_anchor_price", 0)
            primary_pct = a.get("primary_pct", 0)
            today_pct = a.get("today_pct")
            day_pct = a.get("day_pct")

            sign = "+" if primary_pct > 0 else ""
            # 主行 (取較極端那個錨點)
            lines.append(
                f"<b><code>{sid}</code></b> {name} {cur} <b>{d}{int(thr)}%</b> "
                f"{sign}{primary_pct:.2f}% vs {anchor_label} {anchor_price}"
            )
            # 次行 — 顯示另一個錨點對照 (如果兩個都有)
            other_parts = []
            if today_pct is not None and a.get("primary_anchor") != "open":
                s2 = "+" if today_pct > 0 else ""
                other_parts.append(f"開盤 {s2}{today_pct:.2f}%")
            if day_pct is not None and a.get("primary_anchor") != "close":
                s2 = "+" if day_pct > 0 else ""
                other_parts.append(f"昨收 {s2}{day_pct:.2f}%")
            if other_parts:
                lines.append(f"  ({' · '.join(other_parts)})")
        lines.append("")

    if index_alerts:
        lines.append("<b>大盤</b>")
        for a in index_alerts:
            country = a.get("country", "")
            name = a.get("name", "")
            diff = a.get("diff", 0)
            cur = a.get("current", 0)
            today_open = a.get("today_open", 0)
            last_p = a.get("last_alert_price", today_open)
            last_diff = a.get("last_alert_diff", 0)
            leg = a.get("leg_pts", 0)
            consecutive = a.get("consecutive", 1)

            sign_t = "+" if diff > 0 else ""
            sign_l = "+" if leg > 0 else ""

            if abs(last_diff) < 0.01:
                lines.append(
                    f"[{country}] {name} {cur:,.0f} "
                    f"開盤至今 {sign_t}{int(diff)}點"
                )
            else:
                lines.append(
                    f"[{country}] {name} {cur:,.0f} "
                    f"自上次 {sign_l}{int(leg)}點 ({last_p:,.0f}→{cur:,.0f}, 開盤累計 {sign_t}{int(diff)}點)"
                )
            if consecutive >= 2:
                d = a.get("direction", "")
                lines.append(f"  連{consecutive}次同方向{d}")
        lines.append("")

    if crypto_alerts:
        slot_zh = crypto_alerts[0].get("slot_label_zh", "")
        alert_type = crypto_alerts[0].get("alert_type", "scheduled")
        if alert_type == "intra_slot":
            lines.append(f"<b>幣 ({slot_zh} 盤中變動)</b>")
        else:
            lines.append(f"<b>幣 ({slot_zh})</b>" if slot_zh else "<b>幣</b>")

        for a in crypto_alerts:
            name = a.get("name", "")
            cur = a.get("current", 0)
            prev = a.get("prev_price")
            pct = a.get("change_pct", 0)
            is_first = a.get("is_first", False)
            a_type = a.get("alert_type", "scheduled")

            sign = "+" if pct > 0 else ""
            if a_type == "intra_slot":
                lines.append(
                    f"{name} {cur:,.0f} 自{slot_zh}首推 ${prev:,.0f} {sign}{pct:.2f}%"
                )
            elif is_first or prev is None:
                lines.append(f"{name} {cur:,.0f} (首次紀錄)")
            else:
                lines.append(
                    f"{name} {cur:,.0f} 自上次 ${prev:,.0f} {sign}{pct:.2f}%"
                )
        lines.append("")

    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3900] + "\n…(節錄)"
    return out


def fmt_holiday_news(data: dict) -> str:
    """假日 22:00 重大消息推播."""
    if not data:
        return "假日重大消息: 資料不足"

    spy_pct = data.get("spy_pct", 0)
    qqq_pct = data.get("qqq_pct", 0)
    dia_pct = data.get("dia_pct", 0)
    asia = data.get("asia") or {}
    oil = data.get("oil") or {}
    fg = data.get("fg") or {}
    news = data.get("news") or []
    trump = data.get("trump") or []
    ai_text = data.get("ai_text", "")

    lines = [
        "<b>台股休市日 · 全球重大消息整理</b>",
        "",
        f"美股: SPY {spy_pct:+.2f}%   QQQ {qqq_pct:+.2f}%   DIA {dia_pct:+.2f}%",
    ]
    if fg.get("score") is not None:
        lines.append(f"CNN F&G: {fg['score']:.0f} ({fg.get('rating','')})")

    # 亞洲市場
    if asia.get("snapshot"):
        lines.append("")
        lines.append("------ 亞洲鄰近市場 ------")
        for s in asia["snapshot"]:
            country = s.get("country", "")
            name = s.get("market", "")
            dp = s.get("daily_pct", 0)
            lines.append(f"  {country} {name}: {dp:+.2f}%")
        if asia.get("events"):
            for ev in asia["events"][:3]:
                lines.append(f"    ⚠ {ev['country']} {ev['market']} [{ev['event']}]")

    # 油價
    if oil:
        lines.append("")
        lines.append(f"🛢 WTI 油價: ${oil.get('price')} ({oil.get('pct_5d', 0):+.1f}% 5d)")
        if oil.get("signal"):
            lines.append(f"   {oil['signal']}")

    # 重要新聞 top 8 (利多利空優先)
    if news:
        lines.append("")
        lines.append("------ 重要新聞 (利多/利空 優先) ------")
        for n in news[:8]:
            sent = n.get("sentiment", 0)
            tag = "📈" if sent > 0 else ("📉" if sent < 0 else "▪")
            t = n.get("title_zh") or n.get("title", "")
            src = n.get("source", "")
            lines.append(f"  {tag} <b>[{src}]</b> {t[:120]}")

    # Trump
    if trump:
        lines.append("")
        lines.append("------ Trump 言論 ------")
        for t in trump[:2]:
            text = t.get("text", "")[:200]
            lines.append(f"  • {text}")

    # 5 支台股潛力股 + 目標價
    lines.extend(_fmt_potential_picks_block(data.get("potential_picks") or []))

    # AI 推理
    if ai_text:
        lines.append("")
        for ln in ai_text.split("\n"):
            lines.append(ln)

    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3900] + "\n…(節錄)"
    return out


def _fmt_potential_picks_block(picks: list) -> list:
    """5 支台股潛力股 + 目標價 (簡潔風格少 emoji)."""
    if not picks:
        return []
    out = ["", "------ 台股潛力股 Top 5 (含目標價) ------"]
    for i, p in enumerate(picks, 1):
        sid = p.get("stock_id", "")
        nm = p.get("name", "")
        theme = p.get("theme", "")
        cur = p.get("current", 0)
        e_low = p.get("entry_low", 0)
        e_high = p.get("entry_high", 0)
        target = p.get("target_price", 0)
        target_pct = p.get("target_pct", 0)
        stop = p.get("stop_loss", 0)
        stop_pct = p.get("stop_pct", 0)
        win_prob = p.get("win_prob", "")
        hold = p.get("hold_period", "")
        reason = p.get("reason", "")

        out.append("")
        out.append(f"<b>{i}. {sid} {nm}</b>  [{theme}]")
        out.append(f"   現價 {cur} / 進場區間 {e_low}~{e_high}")
        out.append(f"   目標 {target} ({target_pct:+}%) / 停損 {stop} ({stop_pct:+}%)")
        out.append(f"   上漲機率 {win_prob} · 建議持有 {hold}")
        if reason:
            out.append(f"   {reason}")
    return out


def fmt_us_close_analysis(data: dict) -> str:
    """美股收盤 +2h 推播 — 全日板塊 + 對台股次日開盤推理."""
    if not data:
        return "美股盤後分析：資料不足"

    spy_pct = data.get("spy_pct", 0)
    qqq_pct = data.get("qqq_pct", 0)
    dia_pct = data.get("dia_pct", 0)
    sectors = data.get("sectors")
    fg = data.get("fg") or {}
    ai_text = data.get("ai_text", "")

    lines = [
        "<b>美股盤後 (+2h) · 全日綜合 + 對台股次日開盤推理</b>",
        "",
        f"SPY: {spy_pct:+.2f}%   QQQ: {qqq_pct:+.2f}%   DIA: {dia_pct:+.2f}%",
    ]
    if fg.get("score") is not None:
        lines.append(f"CNN F&G: {fg['score']:.0f} ({fg.get('rating','')})")

    if sectors is not None and not sectors.empty:
        lines.append("")
        lines.append("------ 板塊輪動 (1d) ------")
        for _, r in sectors.head(11).iterrows():
            sym = r.get("symbol")
            name = r.get("sector", "")
            r1 = r.get("1d_%", 0)
            sign = "+" if r1 >= 0 else ""
            lines.append(f"  {sym} {name}: {sign}{r1}%")

    # 5 支台股潛力股 + 目標價
    lines.extend(_fmt_potential_picks_block(data.get("potential_picks") or []))

    # 受惠美股的台股推薦
    beneficiaries = data.get("beneficiaries") or {}
    reasons = data.get("beneficiary_reasons") or {}
    if beneficiaries:
        lines.append("")
        lines.append("------ 受惠美股強勢 · 台股可能上漲 ------")
        for theme, info in beneficiaries.items():
            drivers = info.get("drivers", [])
            picks = info.get("picks", [])
            if not picks:
                continue
            drivers_str = "、".join(drivers)
            lines.append("")
            lines.append(f"<b>[{theme}]</b> 受美股 {drivers_str} 帶動")
            for p in picks:
                sid = p["stock_id"]
                nm = p["name"]
                reason = reasons.get(sid, "")
                lines.append(f"  <b><code>{sid}</code></b> {nm}")
                if reason:
                    lines.append(f"     {reason}")

    if ai_text:
        lines.append("")
        for ln in ai_text.split("\n"):
            s = ln.strip()
            if s.startswith("## "):
                lines.append(f"<b>{s[3:]}</b>")
            else:
                lines.append(ln)

    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3900] + "\n…(節錄)"
    return out


def fmt_tw_close_analysis(data: dict) -> str:
    """台股盤後 15:00 推播 — 全日表現 + 日韓比對 + AI 推理."""
    if not data:
        return "台股盤後分析：資料不足"

    twii_close = data.get("twii_close", 0)
    twii_pct = data.get("twii_pct", 0)
    jp_pct = data.get("jp_pct", 0)
    kr_pct = data.get("kr_pct", 0)
    themes_df = data.get("themes")
    jp_kr_sectors = data.get("jp_kr_sectors", {})
    theme_map = data.get("theme_to_asia_map", {})
    ai_text = data.get("ai_text", "")

    lines = [
        f"<b>台股盤後 (15:00) · 全日綜合分析</b>",
        "",
        f"加權指數: {twii_close:,.0f} ({twii_pct:+.2f}%)",
        f"日經 225:   {jp_pct:+.2f}%",
        f"韓國 KOSPI: {kr_pct:+.2f}%",
    ]

    if themes_df is not None and not themes_df.empty:
        lines.append("")
        lines.append("------ 台股族群 vs 日韓對應產業 ------")
        for _, r in themes_df.head(8).iterrows():
            theme = r["題材"]
            avg = r.get("平均%", 0)
            asia_secs = theme_map.get(theme, [])
            tw_arrow = "+" if avg >= 0 else ""
            line = f"\n<b>{theme}</b>: 台股 {tw_arrow}{avg}%"
            if asia_secs:
                asia_parts = []
                for sec in asia_secs:
                    p = jp_kr_sectors.get(sec)
                    if p is not None:
                        sign = "+" if p >= 0 else ""
                        asia_parts.append(f"{sec} {sign}{p}%")
                if asia_parts:
                    line += "  |  " + " · ".join(asia_parts)
            lines.append(line)

    if ai_text:
        lines.append("")
        # AI 文字本身就有 "------ section ------" 結構，直接附上
        for ln in ai_text.split("\n"):
            s = ln.strip()
            if s.startswith("## "):
                lines.append(f"<b>{s[3:]}</b>")
            else:
                lines.append(ln)

    # 外資出貨嫌疑 top 5
    foreign_dumping = data.get("foreign_dumping") or []
    if foreign_dumping:
        lines.append("")
        lines.append("------ 盤後外資出貨嫌疑 (Top 5) ------")
        for d in foreign_dumping[:5]:
            sid = d.get("stock_id", "")
            name = d.get("name", "")
            conf = d.get("confidence", 0)
            reason = d.get("reason", "")
            lines.append(f"  <b><code>{sid}</code></b> {name}  信心 {conf}%")
            if reason:
                lines.append(f"     {reason}")

    # 隔日上漲機率高 top 3
    next_day = data.get("next_day_picks") or []
    if next_day:
        lines.append("")
        lines.append("------ 隔日上漲機率高 (Top 3) ------")
        for d in next_day[:3]:
            sid = d.get("stock_id", "")
            name = d.get("name", "")
            cur = d.get("current")
            today = d.get("today_pct")
            up_prob = d.get("up_prob", 0)
            target = d.get("target_pct", 0)
            reason = d.get("reason", "")
            head = f"  <b><code>{sid}</code></b> {name}"
            if cur is not None:
                sign = "+" if (today or 0) > 0 else ""
                head += f"  {cur} ({sign}{today}%)"
            lines.append(head)
            lines.append(f"     上漲機率 <b>{up_prob}%</b> · 預期 +{target:.1f}%")
            if reason:
                lines.append(f"     {reason}")

    out = "\n".join(lines)
    if len(out) > 3900:
        out = out[:3900] + "\n…(節錄)"
    return out


def fmt_us_open_picks(data: dict, ai_text: str = "") -> str:
    """美股開盤後 30 分推播."""
    if data.get("error"):
        return f"美股開盤分析：{data['error']}"
    lines = [f"<b>美股開盤後 30 分鐘 · 資金流向</b>"]
    # 大盤預測
    lines.extend(_fmt_prediction_block(data.get("prediction"), data.get("accuracy")))
    # 國際訊號
    lines.extend(_fmt_external_signals_block())
    sectors = data.get("sectors")
    if sectors is not None and not sectors.empty:
        lines.append("")
        for _, row in sectors.iterrows():
            sym = row.get("symbol")
            sname = row.get("sector", "")
            r1 = row.get("1d_%")
            lines.append(f"<b>{sym} {sname}</b>  1d {r1:.2f}%")

    sector_picks = data.get("sector_picks", [])
    catalysts = data.get("catalysts", {})
    events = data.get("events", {})
    if sector_picks:
        lines.append("")
        lines.append("<b>各板塊動能潛在股 (3 檔)</b>")
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
                    f"  • <b><code>{sym}</code></b>  今日 {today}% · 量比 {ratio}x · 20d {twenty}%"
                )
                cat = catalysts.get(str(sym))
                if cat:
                    lines.append(f"    催化劑: {cat}")
                ev = events.get(str(sym))
                if ev and ev.get("summary") and ev["summary"] != "—":
                    lines.append(f"    財報: {ev['summary']}")

    growth = data.get("growth")
    if growth is not None and not growth.empty:
        lines.append("")
        lines.append("<b>成長動能極強 / 近期 IPO Top 5</b>")
        for _, s in growth.head(5).iterrows():
            sym = s.get("symbol", "")
            today = s.get("今日%")
            twenty = s.get("20日%")
            ratio = s.get("量比")
            score = s.get("growth_score")
            lines.append(
                f"  • <b><code>{sym}</code></b>  今日 {today}% · 20d {twenty}% · 量比 {ratio}x · {score}/10"
            )
            cat = catalysts.get(str(sym))
            if cat:
                lines.append(f"    催化劑: {cat}")
            ev = events.get(str(sym))
            if ev and ev.get("summary") and ev["summary"] != "—":
                lines.append(f"    財報: {ev['summary']}")

    # 落後股 / 跟漲機會
    lines.extend(_fmt_laggards_block(
        data.get("laggards") or {},
        data.get("laggards_ai") or {},
        market="US",
    ))

    # 5 支台股潛力股 + 目標價
    lines.extend(_fmt_potential_picks_block(data.get("potential_picks") or []))

    if ai_text:
        lines.append("")
        lines.append("<b>AI 觀點</b>")
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
        lines.append(f"{i+1}. <b><code>{sid}</code></b> {name}  [{theme}]")
        lines.append(f"   今日 {today}% / 5d {five}% / 量比 {ratio}x")
    return "\n".join(lines)


def fmt_growth_picks(picks_df) -> str:
    if picks_df is None or picks_df.empty:
        return "🌱 成長動能 Top 10：今日無符合條件的標的。"
    lines = ["<b>🌱 成長動能 Top 10 (消息面 + K 線健康度)</b>", ""]
    for i, r in picks_df.iterrows():
        lines.append(f"{i+1}. <b><code>{r['代號']}</code></b> {r['名稱']} · {r.get('題材','')} · {r['score']}/10")
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
                arrow = "+" if row["今日%"] > 0 else ("-" if row["今日%"] < 0 else "")
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
            body.append(" " + " · ".join(inst_parts))
    body.append("命中: " + ", ".join(hits))
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
