"""
notifier.py
Telegram 通知封裝。同步呼叫 Bot HTTP API (sendMessage)，避免 streamlit 中跑 asyncio。
"""

from __future__ import annotations

import html as _html
import math
from typing import List, Optional

import requests
import streamlit as st

import data_sources as ds


def _esc(s) -> str:
    """HTML escape 任何非結構性字串 (Gemini 輸出 / 新聞標題 / Trump 言論等).

    Telegram parse_mode=HTML 對 `<`, `>`, `&` 嚴格要求，這些字若出現在 user-facing
    內容裡會讓整封訊息 HTTP 400. 對 None / 數字也安全 — 統一轉字串再 escape.

    回傳: 已 escape 的字串. 若原本是 None / "" 回傳空字串.
    """
    if s is None:
        return ""
    try:
        return _html.escape(str(s), quote=False)
    except Exception:
        return ""


def _safe_pct(v, fmt: str = "+.2f", default: str = "—", suffix: str = "%") -> str:
    """安全格式化百分比. v 為 None / NaN / 非數字時回 default.

    例: _safe_pct(1.234) -> "+1.23%", _safe_pct(None) -> "—",
        _safe_pct(0, fmt=".2f") -> "0.00%"
    """
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f"{f:{fmt}}{suffix}"
    except (TypeError, ValueError):
        return default


def _safe_int(v, default: int = 0) -> int:
    """把 v 轉成 int, 失敗 (None/NaN/字串)時回傳 default."""
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return int(f)


def _safe_float(v, default: float = 0.0) -> float:
    """把 v 轉成 float, 失敗或 NaN 時回傳 default."""
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _truncate_tg_msg(out: str, max_bytes: int = 3900) -> str:
    """Telegram parse_mode=HTML 限 4096 bytes (UTF-8). 中文 3 bytes/char,
    所以 char-based truncate (out[:3900]) 在中文訊息中常爆 byte 限制 → HTTP 400.

    這個 helper 用 byte-length 切割, errors='ignore' 防斷尾砍到中文中間,
    取代散落各處的:
        if len(out) > 3900: out = out[:3900] + ...

    取 max_bytes=3900 留 ~200 bytes 給 (節錄) 標籤 + safety margin.
    """
    if not out:
        return ""
    out_bytes = out.encode("utf-8")
    if len(out_bytes) <= max_bytes:
        return out
    return out_bytes[:max_bytes].decode("utf-8", errors="ignore") + "\n…(節錄)"


def _bot_token() -> str:
    # strip 避免 secret 前後空白導致 401
    return (ds._secret("TELEGRAM_BOT_TOKEN") or "").strip()


def _chat_id() -> str:
    return (ds._secret("TELEGRAM_CHAT_ID") or "").strip()


def is_configured() -> bool:
    return bool(_bot_token() and _chat_id())


def _looks_like_html_parse_error(resp_text: str) -> bool:
    """偵測 Telegram parse_mode=HTML 解析失敗的字串特徵."""
    s = (resp_text or "").lower()
    return ("can't parse entities" in s or "parse_entities" in s
            or "unsupported start tag" in s or "tag" in s and "entities" in s)


def build_stock_action_keyboard(stock_id: str, market: str = "TW") -> dict:
    """建一組 inline keyboard, 給個股推播附加「快捷動作」按鈕.

    callback_data 短碼定義 (給未來 webhook handler 配對):
      wl:<sid>      加入自選
      ai:<sid>      AI 深入分析
      sl:<sid>      設停損
      tv:<sid>      開 TradingView
      X:<id>        忽略 (本則訊息不再追蹤)

    無 webhook 時, url button (TradingView) 仍可直接跳轉; callback button 會
    顯示「無法處理」, 但不會崩, 也不影響主訊息.
    """
    if market == "US":
        tv_url = f"https://www.tradingview.com/symbols/{stock_id}/"
    else:
        tv_url = f"https://www.tradingview.com/symbols/TWSE-{stock_id}/"
    sid_short = (stock_id or "")[:32]  # callback_data 上限 64 bytes
    return {
        "inline_keyboard": [
            [
                {"text": "➕ 加自選", "callback_data": f"wl:{sid_short}"},
                {"text": "🤖 AI 分析", "callback_data": f"ai:{sid_short}"},
            ],
            [
                {"text": "🛡️ 設停損", "callback_data": f"sl:{sid_short}"},
                {"text": "📊 看圖", "url": tv_url},
            ],
        ]
    }


def send_message(text: str, disable_preview: bool = True,
                  reply_markup: Optional[dict] = None) -> tuple[bool, str]:
    """直接呼叫 Bot API。回傳 (成功, 訊息).

    強化點:
      1. token / chat_id 自動 strip 前後空白
      2. HTML parse 失敗 → 自動 retry 一次純文字 (避免單一字元擋住整則推播)
      3. 失敗時把更多診斷資訊 (chat_id 長度、message 前 80 字) 包進 info
      4. reply_markup: optional inline keyboard (來自 build_stock_action_keyboard)
      5. 對 None / 空 text early return, 避免 text[:80] 在診斷字串炸 TypeError
    """
    import json as _json
    # 防 None / 空文 — 不能 raise, 要 graceful 回傳
    if text is None or not str(text).strip():
        return False, "送出失敗: 訊息為空 (text is None or whitespace)"
    text = str(text)
    token = _bot_token()
    chat_id = _chat_id()
    if not (token and chat_id):
        return False, "尚未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _post(payload: dict) -> requests.Response:
        return requests.post(url, data=payload, timeout=15)

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup:
        payload["reply_markup"] = _json.dumps(reply_markup)
    try:
        r = _post(payload)
        if r.status_code == 200:
            return True, "已送出"

        # HTTP 400 + parse error → 退回純文字再試一次
        if r.status_code == 400 and _looks_like_html_parse_error(r.text):
            import re as _re
            plain = _re.sub(r"</?[a-zA-Z][^>]*>", "", text)
            payload2 = {
                "chat_id": chat_id,
                "text": plain,
                "disable_web_page_preview": disable_preview,
            }
            if reply_markup:
                payload2["reply_markup"] = _json.dumps(reply_markup)
            try:
                r2 = _post(payload2)
                if r2.status_code == 200:
                    return True, f"已送出 (HTML parse 失敗→純文字 retry 成功; 原因: {r.text[:120]})"
                return False, (f"HTTP {r.status_code} (HTML): {r.text[:160]} | "
                               f"retry HTTP {r2.status_code}: {r2.text[:160]}")
            except Exception as e2:
                return False, f"HTTP {r.status_code}: {r.text[:160]} | retry exception: {e2}"

        # 其他錯誤 — 帶上更多診斷
        diag = (
            f"HTTP {r.status_code}: {r.text[:200]} "
            f"| chat_id_len={len(chat_id)} token_len={len(token)} msg_preview={text[:80]!r}"
        )
        return False, diag
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


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
        lines.append(f"{i+1}. <b><code>{_esc(sid)}</code></b> {_esc(name)} <b>({_esc(hits)}項)</b>")

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
            lines.append(f"   {_esc(labels)}")
        lines.append("")

    return "\n".join(lines)


def fmt_us_top_picks(df, fg: dict) -> str:
    if df is None or df.empty:
        return "美股 Top 5 推薦：今日無符合篩選條件的標的。"
    score = fg.get("score") if fg else None
    rating = fg.get("rating") if fg else None
    try:
        fg_line = f"恐慌指數 {round(float(score),1)} ({_esc(rating)})" if score is not None else "恐慌指數 N/A"
    except (TypeError, ValueError):
        fg_line = "恐慌指數 N/A"
    lines = [f"<b>美股 Top 5 推薦</b> · {fg_line}", ""]
    for i, row in df.head(5).iterrows():
        # pandas Series .get(key, default) 缺 key 時回 default; 但 None 值仍會回 None
        # → 用 `or "—"` 同時擋 None 跟空字串
        sym = row.get("symbol") or "—"
        sc = row.get("score") or "—"
        theme_v = row.get("題材")
        lines.append(
            f"{i+1}. <b><code>{_esc(sym)}</code></b>  "
            f"日 {_esc(row.get('daily_%'))}% / 20d {_esc(row.get('20d_%'))}% · 分數 {_esc(sc)}"
            + (f"\n   題材: {_esc(theme_v)}" if theme_v else "")
        )
    return "\n".join(lines)


def fmt_strong_sectors(sectors_df, leaders_map: dict = None, themes_df=None,
                       theme_leaders: dict = None) -> str:
    """強勢族群 TG 推播 — 包含族群排名 + 每族群推薦股票.

    參數:
      sectors_df: 證交所產業分類熱度 (compute_strong_sectors 的 sectors)
      leaders_map: 對應的各產業 leaders DataFrame (compute_strong_sectors 的 stocks/leaders)
      themes_df: 熱門題材熱度 (compute_hot_themes 的 themes)
      theme_leaders: 各題材的 leaders (compute_hot_themes 的 leaders)
    """
    lines = []
    seen_stocks: set = set()  # 跨族群/題材 dedup, 同一支股一份就好

    if sectors_df is not None and not sectors_df.empty:
        lines.append("<b>強勢族群 (證交所分類) Top 5</b>")
        for _, row in sectors_df.head(5).iterrows():
            sec_name = row.iloc[0]
            avg = _safe_float(row.get("avg_change"))
            up = _safe_int(row.get("up_count"))
            n = _safe_int(row.get("n"))
            lines.append(f"<b>{_esc(sec_name)}</b> 平均 {avg:.2f}% · 上漲 {up}/{n}")
            # 該族群龍頭股 (從 leaders_map 過濾)
            if leaders_map is not None and hasattr(leaders_map, "empty") and not leaders_map.empty:
                # leaders_map 是 single DataFrame, filter by industry
                ind_col = "industry_category" if "industry_category" in leaders_map.columns else None
                sub = leaders_map[leaders_map[ind_col] == sec_name] if ind_col else leaders_map
                if sub is not None and not sub.empty:
                    for _, sr in sub.head(3).iterrows():
                        sid = str(sr.get("stock_id", "")).strip()
                        if not sid or sid in seen_stocks:
                            continue
                        seen_stocks.add(sid)
                        nm = str(sr.get("stock_name", ""))
                        cur = sr.get("現價")
                        pct = sr.get("今日%")
                        ratio = sr.get("量比")
                        parts = []
                        if cur is not None:
                            try:
                                parts.append(f"{float(cur):.2f}")
                            except Exception:
                                pass
                        if pct is not None:
                            try:
                                p = float(pct)
                                sign = "+" if p > 0 else ""
                                parts.append(f"{sign}{p:.2f}%")
                            except Exception:
                                pass
                        if ratio is not None:
                            try:
                                parts.append(f"量比{float(ratio):.2f}x")
                            except Exception:
                                pass
                        details = " · ".join(parts) if parts else ""
                        lines.append(f"  <code>{_esc(sid)}</code> {_esc(nm)}  {details}")
                        # 催化劑 (若 DataFrame 有此欄)
                        cat = sr.get("催化劑") if hasattr(sr, "get") else None
                        if cat and str(cat).strip() and str(cat).strip() != "—":
                            lines.append(f"     催化劑: {_esc(cat)}")
        lines.append("")

    if themes_df is not None and not themes_df.empty:
        lines.append("<b>熱門題材 Top 5</b>")
        for _, row in themes_df.head(5).iterrows():
            theme = row.get("題材", "")
            avg = _safe_float(row.get("平均%"))
            up = _safe_int(row.get("上漲家數"))
            n = _safe_int(row.get("樣本數"))
            lines.append(f"<b>{_esc(theme)}</b> 平均 {avg:.2f}% · 上漲 {up}/{n}")
            if theme_leaders:
                ldf = theme_leaders.get(theme)
                if ldf is not None and not ldf.empty:
                    shown_in_theme = 0
                    for _, sr in ldf.iterrows():
                        sid = str(sr.get("stock_id", "")).strip()
                        if not sid or sid in seen_stocks:
                            continue  # 整個訊息都去重 (一檔只出現一次)
                        seen_stocks.add(sid)
                        nm = str(sr.get("stock_name", ""))
                        cur = sr.get("現價")
                        pct = sr.get("今日%")
                        ratio = sr.get("量比")
                        parts = []
                        if cur is not None:
                            try:
                                parts.append(f"{float(cur):.2f}")
                            except Exception:
                                pass
                        if pct is not None:
                            try:
                                p = float(pct)
                                sign = "+" if p > 0 else ""
                                parts.append(f"{sign}{p:.2f}%")
                            except Exception:
                                pass
                        if ratio is not None:
                            try:
                                parts.append(f"量比{float(ratio):.2f}x")
                            except Exception:
                                pass
                        details = " · ".join(parts) if parts else ""
                        lines.append(f"  <code>{_esc(sid)}</code> {_esc(nm)}  {details}")
                        # 催化劑 (compute_hot_themes 已塞 "催化劑" 欄)
                        cat = sr.get("催化劑") if hasattr(sr, "get") else None
                        if cat and str(cat).strip() and str(cat).strip() != "—":
                            lines.append(f"     催化劑: {_esc(cat)}")
                        shown_in_theme += 1
                        if shown_in_theme >= 3:
                            break
        lines.append("")

    if not lines:
        return "強勢族群: 尚未取得資料"
    return "\n".join(lines).rstrip()


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
        out.append(f"<b>[{_esc(theme)}] 族群均漲 +{_esc(theme_avg)}%</b>")
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

            out.append(f"  <b><code>{_esc(sid)}</code></b> {_esc(nm)}  今日 {_esc(tp)}% / 量比 {_esc(ratio)}x")
            out.append(f"     跟漲機會: {chance_label}")
            if reason:
                out.append(f"     {_esc(reason)}")
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
            country = _esc(s.get("country", ""))
            name = _esc(s.get("market", ""))
            try:
                last = float(s.get("last", 0) or 0)
            except (TypeError, ValueError):
                last = 0.0
            try:
                dp = float(s.get("daily_pct", 0) or 0)
            except (TypeError, ValueError):
                dp = 0.0
            arrow = "+" if dp > 0 else ("-" if dp < 0 else "▪")
            out.append(f"  {country} {name}: {last:,.0f}  {arrow}{dp:+.2f}%")

    if events:
        out.append("")
        out.append("<b>亞洲市場事件</b>")
        # 依 severity 排序
        sev_order = {"high": 0, "medium": 1, "low": 2}
        events_sorted = sorted(events, key=lambda e: sev_order.get(e.get("severity", "low"), 9))
        for ev in events_sorted[:6]:
            country = _esc(ev.get("country", ""))
            name = _esc(ev.get("market", ""))
            event_name = _esc(ev.get("event", ""))
            msg = _esc(ev.get("msg", ""))
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
        out.append(f"<b>WTI 油價: ${_esc(oil.get('price'))} ({_safe_pct(oil.get('pct_5d'), '+.1f', suffix='% 5d')})</b>")
        if oil.get("signal"):
            out.append(f"   {_esc(oil.get('signal'))}")
    macro = news_sources.fetch_macro_indicators()
    if macro:
        parts = []
        if "美元指數" in macro:
            parts.append(f"DXY {_esc(macro['美元指數'].get('value'))} ({_safe_pct(macro['美元指數'].get('pct_5d'))})")
        if "10年美債殖利率" in macro:
            parts.append(f"10Y {_esc(macro['10年美債殖利率'].get('value'))}% ({_safe_pct(macro['10年美債殖利率'].get('pct_5d'))})")
        if "VIX" in macro:
            parts.append(f"VIX {_esc(macro['VIX'].get('value'))} ({_safe_pct(macro['VIX'].get('pct_5d'), '+.1f')})")
        if "BTC" in macro:
            btc_val = macro['BTC'].get('value')
            try:
                btc_str = f"{float(btc_val):,.0f}" if btc_val is not None else "—"
            except (TypeError, ValueError):
                btc_str = "—"
            parts.append(f"BTC ${btc_str} ({_safe_pct(macro['BTC'].get('pct_5d'), '+.1f')})")
        if parts:
            out.append(f"<i>{' · '.join(parts)}</i>")
    # Trump 最近一條
    trumps = news_sources.fetch_trump_truth_social(max_items=2)
    if trumps:
        out.append("")
        out.append("<b>Trump 最新言論</b>")
        for t in trumps[:1]:
            text = t.get("text", "") or ""
            if len(text) > 220:
                text = text[:220] + "…"
            out.append(f"   {_esc(text)}")
    return out


def fmt_tw_open_picks(data: dict, ai_text: str = "") -> str:
    """台股開盤後 30 分推播."""
    if data.get("error"):
        return f"台股開盤分析：{_esc(data['error'])}"
    lines = [f"<b>台股開盤後 30 分鐘 · 資金流向</b>"]

    # Regime banner (最頂部, 空頭時警告)
    regime = data.get("regime") or {}
    if regime:
        try:
            import regime_detector
            banner = regime_detector.fmt_regime_banner(regime)
            if banner:
                lines.append("")
                lines.append(banner)
        except Exception:
            pass

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
                # 防 None — fetch_sector_rotation 資料不足時 1d_% 是 None
                top_str = "、".join(f"{_esc(r['symbol'])} {_safe_pct(r.get('1d_%'))}" for _, r in top3.iterrows())
                bot_str = ", ".join(f"{_esc(r['symbol'])} {_safe_pct(r.get('1d_%'))}" for _, r in bot1.iterrows())
                lines.append(f"  領漲: {top_str}")
                lines.append(f"  落後: {bot_str}")
    # 亞洲鄰近市場
    lines.extend(_fmt_asia_markets_block(data.get("asia") or {}))
    # 國際訊號
    lines.extend(_fmt_external_signals_block())
    themes_df = data.get("themes")
    if themes_df is not None and not themes_df.empty:
        lines.append("")
        lines.append("<b>熱門題材</b>")
        for _, row in themes_df.iterrows():
            name = row.get("題材")
            avg = row.get("平均%")
            up = int(row.get("上漲家數", 0))
            n = int(row.get("樣本數", 0))
            lines.append(f"<b>{_esc(name)}</b>  平均 {_esc(avg)}% · 上漲 {up}/{n}")

    # 萌芽族群 (還沒上排行榜) — 真正能賺的領先訊號
    emerging = data.get("emerging") or []
    if emerging:
        try:
            import emerging_themes
            lines.extend(emerging_themes.fmt_emerging_themes_block(emerging))
        except Exception:
            pass

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
            lines.append(f"\n<b>[{_esc(theme)}]</b>")
            for _, s in stocks.iterrows():
                sid = s.get("stock_id", "")
                nm = s.get("stock_name", "")
                today = s.get("今日%")
                ratio = s.get("量比")
                five = s.get("5日%")
                lines.append(
                    f"  • <b><code>{_esc(sid)}</code></b> {_esc(nm)}  "
                    f"今日 {_esc(today)}% · 量比 {_esc(ratio)}x · 5d {_esc(five)}%"
                )
                cat = catalysts.get(str(sid))
                if cat:
                    lines.append(f"    催化劑: {_esc(cat)}")
                ev = events.get(str(sid))
                if ev and ev.get("summary") and ev["summary"] != "—":
                    lines.append(f"    財報: {_esc(ev['summary'])}")
                ch = chips.get(str(sid))
                if ch:
                    direction = _esc(ch.get("direction", ""))
                    prob = ch.get("change_prob", 0)
                    rec = _esc(ch.get("recommendation", ""))
                    reason = _esc(ch.get("reason", ""))
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
        # 簡化 markdown — AI 內容用 _esc (Gemini 偶爾會輸出 < > 字元)
        for line in ai_text.split("\n"):
            s = line.strip()
            if s.startswith("## "):
                lines.append(f"<b>{_esc(s[3:])}</b>")
            else:
                lines.append(_esc(line))

    # G7 fix: byte-length truncation (取代舊的 char-based len() 檢查)
    return _truncate_tg_msg("\n".join(lines))


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
            try:
                thr = int(float(a.get("threshold", 0) or 0))
            except (TypeError, ValueError):
                thr = 0
            anchor_label = a.get("primary_anchor_label", "")
            anchor_price = a.get("primary_anchor_price", 0)
            try:
                primary_pct = float(a.get("primary_pct", 0) or 0)
            except (TypeError, ValueError):
                primary_pct = 0.0
            today_pct = a.get("today_pct")
            day_pct = a.get("day_pct")

            sign = "+" if primary_pct > 0 else ""
            # 主行 (取較極端那個錨點)
            lines.append(
                f"<b><code>{_esc(sid)}</code></b> {_esc(name)} {_esc(cur)} <b>{_esc(d)}{thr}%</b> "
                f"{sign}{primary_pct:.2f}% vs {_esc(anchor_label)} {_esc(anchor_price)}"
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
            country = _esc(a.get("country", ""))
            name = _esc(a.get("name", ""))
            def _f(v, d=0.0):
                try:
                    return float(v) if v is not None else d
                except (TypeError, ValueError):
                    return d
            diff = _f(a.get("diff", 0))
            cur = _f(a.get("current", 0))
            today_open = _f(a.get("today_open", 0))
            last_p = _f(a.get("last_alert_price", today_open))
            last_diff = _f(a.get("last_alert_diff", 0))
            leg = _f(a.get("leg_pts", 0))
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
                d = _esc(a.get("direction", ""))
                lines.append(f"  連{consecutive}次同方向{d}")
            # 顯示動態門檻 + 今日次數 (給 user 知道 throttle 狀態)
            extra_parts = []
            try:
                t_used = a.get("threshold_used")
                # 只在動態門檻 != 預設時才顯示 (避免每次都顯示同樣的)
                if t_used is not None and abs(float(t_used) - float(a.get("threshold_bucket_size", t_used) or t_used)) > 0.1:
                    extra_parts.append(f"門檻 {int(float(t_used))}點")
            except Exception:
                pass
            alerts_today = a.get("alerts_today")
            if alerts_today:
                extra_parts.append(f"今日第 {alerts_today}/4 次")
            if extra_parts:
                lines.append(f"  ({' · '.join(extra_parts)})")
        lines.append("")

    if crypto_alerts:
        slot_zh = _esc(crypto_alerts[0].get("slot_label_zh", ""))
        alert_type = crypto_alerts[0].get("alert_type", "scheduled")
        if alert_type == "intra_slot":
            lines.append(f"<b>幣 ({slot_zh} 盤中變動)</b>")
        else:
            lines.append(f"<b>幣 ({slot_zh})</b>" if slot_zh else "<b>幣</b>")

        for a in crypto_alerts:
            name = _esc(a.get("name", ""))
            try:
                cur = float(a.get("current", 0) or 0)
            except (TypeError, ValueError):
                cur = 0.0
            prev = a.get("prev_price")
            try:
                prev_f = float(prev) if prev is not None else None
            except (TypeError, ValueError):
                prev_f = None
            try:
                pct = float(a.get("change_pct", 0) or 0)
            except (TypeError, ValueError):
                pct = 0.0
            is_first = a.get("is_first", False)
            a_type = a.get("alert_type", "scheduled")

            sign = "+" if pct > 0 else ""
            if a_type == "intra_slot" and prev_f is not None:
                lines.append(
                    f"{name} {cur:,.0f} 自{slot_zh}首推 ${prev_f:,.0f} {sign}{pct:.2f}%"
                )
            elif is_first or prev_f is None:
                lines.append(f"{name} {cur:,.0f} (首次紀錄)")
            else:
                lines.append(
                    f"{name} {cur:,.0f} 自上次 ${prev_f:,.0f} {sign}{pct:.2f}%"
                )
        lines.append("")

    # G7 fix: byte-length truncation (取代舊的 char-based len() 檢查)
    return _truncate_tg_msg("\n".join(lines))


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

    # 防 None — 任何 *_pct 來源端有可能為 None
    def _to_f(v):
        try:
            f = float(v) if v is not None else 0.0
            return f if not (math.isnan(f) or math.isinf(f)) else 0.0
        except (TypeError, ValueError):
            return 0.0
    spy_pct, qqq_pct, dia_pct = _to_f(spy_pct), _to_f(qqq_pct), _to_f(dia_pct)
    lines = [
        "<b>台股休市日 · 全球重大消息整理</b>",
        "",
        f"美股: SPY {spy_pct:+.2f}%   QQQ {qqq_pct:+.2f}%   DIA {dia_pct:+.2f}%",
    ]
    if fg.get("score") is not None:
        try:
            lines.append(f"CNN F&amp;G: {float(fg['score']):.0f} ({_esc(fg.get('rating',''))})")
        except (TypeError, ValueError):
            pass

    # 亞洲市場
    if asia.get("snapshot"):
        lines.append("")
        lines.append("------ 亞洲鄰近市場 ------")
        for s in asia["snapshot"]:
            country = _esc(s.get("country", ""))
            name = _esc(s.get("market", ""))
            dp = _to_f(s.get("daily_pct", 0))
            lines.append(f"  {country} {name}: {dp:+.2f}%")
        if asia.get("events"):
            for ev in asia["events"][:3]:
                lines.append(f"    ⚠ {_esc(ev.get('country',''))} {_esc(ev.get('market',''))} [{_esc(ev.get('event',''))}]")

    # 油價
    if oil:
        lines.append("")
        lines.append(f"🛢 WTI 油價: ${_esc(oil.get('price'))} ({_safe_pct(oil.get('pct_5d'), '+.1f', suffix='% 5d')})")
        if oil.get("signal"):
            lines.append(f"   {_esc(oil.get('signal'))}")

    # 重要新聞 top 8 (利多利空優先)
    if news:
        lines.append("")
        lines.append("------ 重要新聞 (利多/利空 優先) ------")
        for n in news[:8]:
            sent = n.get("sentiment", 0) or 0
            tag = "📈" if sent > 0 else ("📉" if sent < 0 else "▪")
            t = n.get("title_zh") or n.get("title", "") or ""
            src = n.get("source", "") or ""
            lines.append(f"  {tag} <b>[{_esc(src)}]</b> {_esc(t[:120])}")

    # Trump
    if trump:
        lines.append("")
        lines.append("------ Trump 言論 ------")
        for t in trump[:2]:
            text = (t.get("text", "") or "")[:200]
            lines.append(f"  • {_esc(text)}")

    # 5 支台股潛力股 + 目標價
    lines.extend(_fmt_potential_picks_block(data.get("potential_picks") or []))

    # AI 推理
    if ai_text:
        lines.append("")
        for ln in ai_text.split("\n"):
            s = ln.strip()
            if s.startswith("## "):
                lines.append(f"<b>{_esc(s[3:])}</b>")
            else:
                lines.append(_esc(ln))

    # G7 fix: byte-length truncation (取代舊的 char-based len() 檢查)
    return _truncate_tg_msg("\n".join(lines))


def _compute_rr(entry_low, entry_high, target, stop) -> Optional[float]:
    """算 risk:reward 比. 用「進場區間中位數」當參考進場價.
    R:R = (target - entry_mid) / (entry_mid - stop)
    """
    try:
        el = float(entry_low) if entry_low not in (None, "") else None
        eh = float(entry_high) if entry_high not in (None, "") else None
        t = float(target) if target not in (None, "") else None
        s = float(stop) if stop not in (None, "") else None
        if not (el and eh and t and s):
            return None
        entry = (el + eh) / 2
        if entry <= s:
            return None
        risk = entry - s
        reward = t - entry
        if risk <= 0:
            return None
        return round(reward / risk, 2)
    except (TypeError, ValueError):
        return None


def _entry_zone_bar(current, entry_low, entry_high, width: int = 14) -> str:
    """畫一條 ASCII bar 顯示現價在進場區間的相對位置.
    例:
      [── ●   ──]  在區間中段
      [─────●─]   在區間上緣
      [●         ]  低於下緣 (可進場)
      [        ●] 高於上緣 (追高)
    """
    try:
        cur = float(current)
        el = float(entry_low)
        eh = float(entry_high)
        if eh <= el:
            return ""
    except (TypeError, ValueError):
        return ""
    span = eh - el
    if cur < el:
        # 低於下緣 — 顯示 [●---區間---] 加距離標記
        dist_pct = (el - cur) / el * 100
        return f"[●  區間  ]  低於下緣 {dist_pct:.1f}% (可進場)"
    if cur > eh:
        dist_pct = (cur - eh) / eh * 100
        return f"[  區間  ●]  高於上緣 {dist_pct:.1f}% (追高警告)"
    # 在區間內 — 算位置
    pos = int((cur - el) / span * (width - 1))
    pos = max(0, min(width - 1, pos))
    bar = "─" * pos + "●" + "─" * (width - 1 - pos)
    return f"[{bar}]  在區間內 (進場中)"


def _fmt_potential_picks_block(picks: list, hide_low_rr: bool = True,
                                  rr_threshold: float = 1.5) -> list:
    """5 支台股潛力股 + 目標價 (簡潔風格少 emoji).

    hide_low_rr=True 時, R:R < rr_threshold 的會直接剔除 (不顯示).
    """
    if not picks:
        return []
    out = ["", "------ 台股潛力股 Top 5 (含目標價) ------"]
    def _safe_int_or_dash(v):
        try:
            if v is None:
                return "—"
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                return "—"
            return int(f)
        except (TypeError, ValueError):
            return "—"

    # 先算每檔 R:R, 過濾掉低 R:R 後再 render
    enriched = []
    for p in picks:
        rr = _compute_rr(p.get("entry_low"), p.get("entry_high"),
                         p.get("target_price"), p.get("stop_loss"))
        if hide_low_rr and rr is not None and rr < rr_threshold:
            continue
        enriched.append((p, rr))

    if not enriched:
        out.append("(本日無 R:R ≥ {:.1f} 的標的)".format(rr_threshold))
        return out

    for i, (p, rr) in enumerate(enriched, 1):
        sid = _esc(p.get("stock_id", ""))
        nm = _esc(p.get("name", ""))
        theme = _esc(p.get("theme", ""))
        cur_raw = p.get("current", 0)
        cur = _esc(cur_raw)
        el_raw = p.get("entry_low", 0)
        eh_raw = p.get("entry_high", 0)
        e_low = _esc(el_raw)
        e_high = _esc(eh_raw)
        target = _esc(p.get("target_price", 0))
        target_pct = _safe_int_or_dash(p.get("target_pct", 0))
        stop = _esc(p.get("stop_loss", 0))
        stop_pct = _safe_int_or_dash(p.get("stop_pct", 0))
        win_prob = _esc(p.get("win_prob", ""))
        hold = _esc(p.get("hold_period", ""))
        reason = _esc(p.get("reason", ""))

        # R:R 標籤 (>= 2 加星, >= 3 雙星)
        if rr is None:
            rr_label = ""
        elif rr >= 3.0:
            rr_label = f" · <b>R:R {rr} ⭐⭐</b>"
        elif rr >= 2.0:
            rr_label = f" · <b>R:R {rr} ⭐</b>"
        else:
            rr_label = f" · R:R {rr}"

        out.append("")
        out.append(f"<b>{i}. {sid} {nm}</b>  [{theme}]{rr_label}")
        # 進場區間視覺化
        zone = _entry_zone_bar(cur_raw, el_raw, eh_raw)
        if zone:
            out.append(f"   {zone}")
        out.append(f"   現價 {cur} / 進場區間 {e_low}~{e_high}")
        # target_pct / stop_pct 仍可能是 "—"; format spec 只在數字時套用 +
        tp_str = f"{target_pct:+}" if isinstance(target_pct, int) else target_pct
        sp_str = f"{stop_pct:+}" if isinstance(stop_pct, int) else stop_pct
        out.append(f"   目標 {target} ({tp_str}%) / 停損 {stop} ({sp_str}%)")
        out.append(f"   上漲機率 {win_prob} · 建議持有 {hold}")
        # 部位規模建議 (依 user 設定的帳戶資金 + 單筆風險 %)
        try:
            import position_sizer as _ps
            cfg = _ps.load_user_config()
            entry_mid = ((float(el_raw) + float(eh_raw)) / 2) if el_raw and eh_raw else None
            sizing = _ps.compute_position_size(
                entry_price=entry_mid, stop_price=p.get("stop_loss"),
                account_capital=cfg["account_capital"],
                risk_per_trade_pct=cfg["risk_per_trade_pct"],
                max_position_pct=cfg["max_position_pct"],
                market="TW",
            ) if entry_mid else None
            advice = _ps.fmt_position_advice(sizing, market="TW")
            if advice:
                out.append(f"   {_esc(advice)}")
        except Exception:
            pass
        if reason:
            out.append(f"   {reason}")
    return out


def fmt_us_close_analysis(data: dict) -> str:
    """美股收盤 +2h 推播 — 全日板塊 + 對台股次日開盤推理."""
    if not data:
        return "美股盤後分析：資料不足"

    def _to_f(v):
        try:
            f = float(v) if v is not None else 0.0
            return f if not (math.isnan(f) or math.isinf(f)) else 0.0
        except (TypeError, ValueError):
            return 0.0
    spy_pct = _to_f(data.get("spy_pct", 0))
    qqq_pct = _to_f(data.get("qqq_pct", 0))
    dia_pct = _to_f(data.get("dia_pct", 0))
    sectors = data.get("sectors")
    fg = data.get("fg") or {}
    ai_text = data.get("ai_text", "")

    lines = [
        "<b>美股盤後 (+2h) · 全日綜合 + 對台股次日開盤推理</b>",
        "",
        f"SPY: {spy_pct:+.2f}%   QQQ: {qqq_pct:+.2f}%   DIA: {dia_pct:+.2f}%",
    ]
    if fg.get("score") is not None:
        try:
            lines.append(f"CNN F&amp;G: {float(fg['score']):.0f} ({_esc(fg.get('rating',''))})")
        except (TypeError, ValueError):
            pass

    if sectors is not None and not sectors.empty:
        lines.append("")
        lines.append("------ 板塊輪動 (1d) ------")
        for _, r in sectors.head(11).iterrows():
            sym = r.get("symbol")
            name = r.get("sector", "")
            # 防 None: fetch_sector_rotation 在資料不足時把 1d_% 寫成 None,
            # `.get(key, 0)` 不會在「value is None」回 default
            r1_raw = r.get("1d_%")
            r1 = r1_raw if isinstance(r1_raw, (int, float)) else 0
            sign = "+" if r1 >= 0 else ""
            lines.append(f"  {_esc(sym)} {_esc(name)}: {sign}{r1}%")

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
            drivers_str = _esc("、".join(drivers))
            lines.append("")
            lines.append(f"<b>[{_esc(theme)}]</b> 受美股 {drivers_str} 帶動")
            for p in picks:
                sid = p["stock_id"]
                nm = p["name"]
                reason = reasons.get(sid, "")
                lines.append(f"  <b><code>{_esc(sid)}</code></b> {_esc(nm)}")
                if reason:
                    lines.append(f"     {_esc(reason)}")

    if ai_text:
        lines.append("")
        for ln in ai_text.split("\n"):
            s = ln.strip()
            if s.startswith("## "):
                lines.append(f"<b>{_esc(s[3:])}</b>")
            else:
                lines.append(_esc(ln))

    # G7 fix: byte-length truncation (取代舊的 char-based len() 檢查)
    return _truncate_tg_msg("\n".join(lines))


def fmt_tw_close_analysis(data: dict) -> str:
    """台股盤後 15:00 推播 — 全日表現 + 日韓比對 + AI 推理."""
    if not data:
        return "台股盤後分析：資料不足"

    def _to_f(v):
        try:
            f = float(v) if v is not None else 0.0
            return f if not (math.isnan(f) or math.isinf(f)) else 0.0
        except (TypeError, ValueError):
            return 0.0
    twii_close = _to_f(data.get("twii_close", 0))
    twii_pct = _to_f(data.get("twii_pct", 0))
    jp_pct = _to_f(data.get("jp_pct", 0))
    kr_pct = _to_f(data.get("kr_pct", 0))
    themes_df = data.get("themes")
    jp_kr_sectors = data.get("jp_kr_sectors", {})
    theme_map = data.get("theme_to_asia_map", {})
    ai_text = data.get("ai_text", "")
    why_summary = data.get("why_summary", "") or ""

    lines = [
        f"<b>台股盤後 (15:00) · 全日綜合分析</b>",
    ]
    # 一句話 why summary (最重要, 放最前面)
    if why_summary:
        lines.append("")
        for ln in why_summary.split("\n"):
            ln = ln.strip()
            if ln:
                lines.append(f"<i>{_esc(ln)}</i>")
    lines.extend([
        "",
        f"加權指數: {twii_close:,.0f} ({twii_pct:+.2f}%)",
        f"日經 225:   {jp_pct:+.2f}%",
        f"韓國 KOSPI: {kr_pct:+.2f}%",
    ])

    if themes_df is not None and not themes_df.empty:
        lines.append("")
        lines.append("------ 台股族群 vs 日韓對應產業 ------")
        for _, r in themes_df.head(8).iterrows():
            theme = r["題材"]
            avg = _to_f(r.get("平均%", 0))
            asia_secs = theme_map.get(theme, [])
            tw_arrow = "+" if avg >= 0 else ""
            line = f"\n<b>{_esc(theme)}</b>: 台股 {tw_arrow}{avg}%"
            if asia_secs:
                asia_parts = []
                for sec in asia_secs:
                    p = jp_kr_sectors.get(sec)
                    if p is not None:
                        try:
                            pf = float(p)
                            sign = "+" if pf >= 0 else ""
                            asia_parts.append(f"{_esc(sec)} {sign}{pf}%")
                        except (TypeError, ValueError):
                            pass
                if asia_parts:
                    line += "  |  " + " · ".join(asia_parts)
            lines.append(line)

    if ai_text:
        lines.append("")
        # AI 文字本身就有 "------ section ------" 結構，直接附上
        for ln in ai_text.split("\n"):
            s = ln.strip()
            if s.startswith("## "):
                lines.append(f"<b>{_esc(s[3:])}</b>")
            else:
                lines.append(_esc(ln))

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
            lines.append(f"  <b><code>{_esc(sid)}</code></b> {_esc(name)}  信心 {conf}%")
            if reason:
                lines.append(f"     {_esc(reason)}")

    # 避開訊號 — 跟外資出貨互補, 涵蓋跌破月線 / 散戶接刀 / 放量下跌等
    avoid_picks = data.get("avoid_picks") or []
    if avoid_picks:
        lines.append("")
        lines.append("------ ⚠ 今日避開 (Top 5) ------")
        for d in avoid_picks[:5]:
            sid = d.get("stock_id", "")
            name = d.get("name", "")
            score = d.get("score", 0)
            reasons = d.get("reasons", "")
            cur = d.get("current")
            today_pct = d.get("today_pct")
            head = f"  <b><code>{_esc(sid)}</code></b> {_esc(name)}"
            if cur is not None:
                head += f"  {_esc(cur)}"
            if today_pct is not None:
                try:
                    tp = float(today_pct)
                    sign = "+" if tp > 0 else ""
                    head += f" ({sign}{tp:.2f}%)"
                except (TypeError, ValueError):
                    pass
            head += f"  風險分 {_esc(score)}"
            lines.append(head)
            if reasons:
                lines.append(f"     {_esc(reasons)}")

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
            target = _to_f(d.get("target_pct", 0))
            reason = d.get("reason", "")
            head = f"  <b><code>{_esc(sid)}</code></b> {_esc(name)}"
            if cur is not None:
                sign = "+" if (today or 0) > 0 else ""
                head += f"  {_esc(cur)} ({sign}{_esc(today)}%)"
            lines.append(head)
            lines.append(f"     上漲機率 <b>{_esc(up_prob)}%</b> · 預期 +{target:.1f}%")
            if reason:
                lines.append(f"     {_esc(reason)}")

    # 訊號準確率 (本月各類訊號命中率) — accuracy_block 來自 signal_tracker
    accuracy_block = data.get("accuracy_block", "")
    if accuracy_block:
        lines.append("")
        lines.append(accuracy_block)

    # G7 fix: byte-length truncation (取代舊的 char-based len() 檢查)
    return _truncate_tg_msg("\n".join(lines))


def fmt_stop_loss_alerts(breaches: list) -> str:
    """停損警報訊息 (給 tw_open / tw_mid / monitor 用).

    支援兩種 alert_type:
      - "breach": 已跌破停損 (緊急)
      - "near_stop": 距停損 ≤ 2% (預警)
    """
    if not breaches:
        return ""
    # 分組: 先警報已破的, 再警報接近的
    breached = [b for b in breaches if b.get("alert_type", "breach") == "breach"]
    near = [b for b in breaches if b.get("alert_type") == "near_stop"]

    lines = []
    if breached:
        lines.extend(["<b>🚨 持倉停損警報 (已跌破)</b>", ""])
        for b in breached:
            sid = b.get("stock_id", "")
            name = b.get("name", "")
            cur = b.get("current", 0)
            sl = b.get("stop_loss", 0)
            try:
                pct = float(b.get("breach_pct", 0) or 0)
            except (TypeError, ValueError):
                pct = 0.0
            sign = "+" if pct > 0 else ""
            lines.append(
                f"<b><code>{_esc(sid)}</code></b> {_esc(name)} {_esc(cur)} 跌破停損 {_esc(sl)} "
                f"({sign}{pct:.2f}%)"
            )
            sd = b.get("set_date", "")
            if sd:
                lines.append(f"  停損設定日: {_esc(sd)}")
        lines.append("")
    if near:
        lines.extend(["<b>⚠ 持倉接近停損 (距離 ≤ 2%)</b>", ""])
        for b in near:
            sid = b.get("stock_id", "")
            name = b.get("name", "")
            cur = b.get("current", 0)
            sl = b.get("stop_loss", 0)
            try:
                d_pct = float(b.get("distance_pct", 0) or 0)
            except (TypeError, ValueError):
                d_pct = 0
            lines.append(
                f"<code>{_esc(sid)}</code> {_esc(name)} {_esc(cur)} · 停損 {_esc(sl)} · 距停損 {d_pct:.2f}%"
            )
        lines.append("")
    return "\n".join(lines).rstrip() if lines else ""


def fmt_intraday_reversal_alerts(alerts: list) -> str:
    """盤中反轉警報訊息 — 從高點回吐 / 從低點反彈.

    alerts: 來自 index_alerts.check_intraday_reversal() 的 list.
    每個 dict 含: symbol, name, country, type ("drawdown"/"rebound"),
                  current, today_open, today_high, today_low,
                  drawdown_pct, rebound_pct, pct_vs_open, alerts_today.
    """
    if not alerts:
        return ""

    drawdowns = [a for a in alerts if a.get("type") == "drawdown"]
    rebounds = [a for a in alerts if a.get("type") == "rebound"]

    lines = ["<b>🔄 盤中反轉警報</b>", ""]

    if drawdowns:
        lines.append("<b>📉 從今日高點回吐</b>")
        for a in drawdowns:
            country = _esc(a.get("country", ""))
            name = _esc(a.get("name", ""))
            sym = _esc(a.get("symbol", ""))
            try:
                cur = float(a.get("current", 0) or 0)
                high = float(a.get("today_high", 0) or 0)
                dd_pct = float(a.get("drawdown_pct", 0) or 0)
                vs_open = float(a.get("pct_vs_open", 0) or 0)
            except (TypeError, ValueError):
                cur = high = dd_pct = vs_open = 0.0
            sign_o = "+" if vs_open > 0 else ""
            lines.append(
                f"[{country}] {name} <code>{sym}</code> {cur:,.2f}"
            )
            lines.append(
                f"  高點 {high:,.2f} → 回吐 <b>{dd_pct:+.2f}%</b> "
                f"(vs 開盤 {sign_o}{vs_open:.2f}%)"
            )
            alerts_today = a.get("alerts_today")
            if alerts_today:
                lines.append(f"  (今日第 {alerts_today}/3 次反轉警報)")
        lines.append("")

    if rebounds:
        lines.append("<b>📈 從今日低點反彈</b>")
        for a in rebounds:
            country = _esc(a.get("country", ""))
            name = _esc(a.get("name", ""))
            sym = _esc(a.get("symbol", ""))
            try:
                cur = float(a.get("current", 0) or 0)
                low = float(a.get("today_low", 0) or 0)
                rb_pct = float(a.get("rebound_pct", 0) or 0)
                vs_open = float(a.get("pct_vs_open", 0) or 0)
            except (TypeError, ValueError):
                cur = low = rb_pct = vs_open = 0.0
            sign_o = "+" if vs_open > 0 else ""
            lines.append(
                f"[{country}] {name} <code>{sym}</code> {cur:,.2f}"
            )
            lines.append(
                f"  低點 {low:,.2f} → 反彈 <b>+{rb_pct:.2f}%</b> "
                f"(vs 開盤 {sign_o}{vs_open:.2f}%)"
            )
            alerts_today = a.get("alerts_today")
            if alerts_today:
                lines.append(f"  (今日第 {alerts_today}/3 次反轉警報)")
        lines.append("")

    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_active_etf_change(diff: dict) -> str:
    """主動式 ETF 持股變動推播 (供 active_etf_monitor 使用).

    diff: 來自 active_etf_monitor.detect_changes() 的回傳.
          含 is_baseline=True 時 → 「監控已啟動」訊息
          含 is_baseline=False 時 → 變動明細
    """
    if not diff:
        return ""
    etf_code = diff.get("etf_code", "")
    etf_name = diff.get("etf_name", etf_code)
    issuer = diff.get("etf_issuer", "")

    # Hidden Bug #5: baseline 第一次跑也推一封 (讓 user 知道監控啟動了)
    if diff.get("is_baseline"):
        new_d = diff.get("new_data_date", "")
        n = diff.get("stocks_count", 0)
        unmapped = diff.get("unmapped_count", 0)
        baseline_lines = [
            f"<b>📊 主動式 ETF 監控已啟動</b>",
            f"<code>{_esc(etf_code)}</code> {_esc(etf_name)} · {_esc(issuer)}",
            f"資料日期: <b>{_esc(new_d)}</b> · 已記錄 {n} 檔持股",
            "",
            "<i>明日起會自動偵測新增/移除/比例變動並推播。</i>",
        ]
        if unmapped:
            baseline_lines.append(
                f"<i>⚠️ 注意: {unmapped} 個 row 在 MoneyDJ 沒 (XXXX.TW) link, 已略過</i>"
            )
        return "\n".join(baseline_lines)

    prev_d = diff.get("prev_data_date", "")
    new_d = diff.get("new_data_date", "")
    added = diff.get("added", []) or []
    removed = diff.get("removed", []) or []
    changed = diff.get("changed", []) or []
    unmapped = diff.get("unmapped_count", 0)

    lines = [
        f"<b>📊 主動式 ETF 持股變動</b>",
        f"<code>{_esc(etf_code)}</code> {_esc(etf_name)} · {_esc(issuer)}",
        f"資料日期: {_esc(prev_d)} → <b>{_esc(new_d)}</b>",
        "",
    ]

    if added:
        lines.append(f"<b>🟢 新進 top-10 ({len(added)} 檔)</b>")
        for s in added:
            sid = _esc(s.get("sid", ""))
            name = _esc(s.get("name", ""))
            try:
                pct = float(s.get("pct", 0) or 0)
            except (TypeError, ValueError):
                pct = 0.0
            lines.append(f"  • <code>{sid}</code> {name} {pct:.2f}%")
        lines.append("")

    if removed:
        lines.append(f"<b>🔴 退出 top-10 ({len(removed)} 檔)</b>")
        for s in removed:
            sid = _esc(s.get("sid", ""))
            name = _esc(s.get("name", ""))
            try:
                pct = float(s.get("pct", 0) or 0)
            except (TypeError, ValueError):
                pct = 0.0
            lines.append(f"  • <code>{sid}</code> {name} (上次 {pct:.2f}%)")
        lines.append("")

    if changed:
        lines.append(f"<b>📈 持股比例變動 ≥0.5pp ({len(changed)} 檔)</b>")
        for s in changed:
            sid = _esc(s.get("sid", ""))
            name = _esc(s.get("name", ""))
            try:
                old_p = float(s.get("old_pct", 0) or 0)
                new_p = float(s.get("new_pct", 0) or 0)
                delta = float(s.get("delta_pp", 0) or 0)
            except (TypeError, ValueError):
                old_p = new_p = delta = 0.0
            arrow = "↑" if delta > 0 else "↓"
            lines.append(
                f"  • <code>{sid}</code> {name} {old_p:.2f}%→{new_p:.2f}% "
                f"<b>{arrow}{abs(delta):.2f}pp</b>"
            )
        lines.append("")

    if unmapped:
        lines.append(
            f"<i>⚠️ 本期有 {unmapped} 個 row 在 MoneyDJ 沒 (XXXX.TW) link, "
            f"diff 可能有 false positive</i>"
        )
    lines.append("<i>資料來源: MoneyDJ · 僅供參考</i>")
    out = "\n".join(lines)
    return _truncate_tg_msg(out)


def _md_to_tg_html(text: str) -> str:
    """把 markdown (## header, **bold**, *italic*) 轉成 Telegram HTML.

    先 _esc() 處理掉 <, >, & 等危險字元, 再用 regex 套上 HTML 標籤.
    這個順序很重要: 反過來會把我們自己加的 <b> 跳脫成 &lt;b&gt;.

    支援:
      ## H2     → <b>H2</b>
      # H1      → <b>H1</b>
      **bold**  → <b>bold</b>
    """
    if not text:
        return ""
    import re
    s = _esc(text)
    # Header: ## 或 # 開頭整行轉粗體
    s = re.sub(r"^##\s+(.+)$", r"<b>\1</b>", s, flags=re.MULTILINE)
    s = re.sub(r"^#\s+(.+)$", r"<b>\1</b>", s, flags=re.MULTILINE)
    # **bold**
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return s


def fmt_systemic_crash_alert(crash_data: dict, ai_text: str = "") -> str:
    """系統性大跌警報訊息 (含 trigger 摘要 + 規則式快評 + Gemini 動作建議).

    crash_data: 來自 index_alerts.check_systemic_crash() 的回傳 dict.
    ai_text:    Gemini 回的分析文字 (可空, 空的時候只附規則式快評).
    """
    if not crash_data:
        return ""
    triggers = crash_data.get("triggers", []) or []
    ctx = crash_data.get("context", {}) or {}
    if not triggers:
        return ""

    alert_idx = crash_data.get("alert_index", 1)
    max_per_day = crash_data.get("max_per_day", 2)
    vix = ctx.get("vix")
    all_snaps = ctx.get("all_snapshots", []) or []

    lines = [
        f"<b>🚨 系統性大跌警報 (本日 {alert_idx}/{max_per_day})</b>",
        "",
        "<b>觸發標的</b>",
    ]
    for t in triggers:
        name = _esc(t.get("name", ""))
        sym = _esc(t.get("symbol", ""))
        ttype = t.get("trigger_type", "")
        try:
            tval = float(t.get("trigger_value", 0) or 0)
        except (TypeError, ValueError):
            tval = 0.0
        try:
            cur = float(t.get("current", 0) or 0)
        except (TypeError, ValueError):
            cur = 0.0
        ttype_zh = "盤中" if ttype == "intraday" else "連2日累計"
        lines.append(f"• {name} <code>{sym}</code> {cur:,.2f}, {ttype_zh} <b>{tval:+.2f}%</b>")
    lines.append("")

    # 全部監控標的 (給全局視野)
    if all_snaps:
        lines.append("<b>當下全市場</b>")
        for s in all_snaps:
            name = _esc(s.get("name", ""))
            try:
                ip = float(s.get("intraday_pct", 0) or 0)
            except (TypeError, ValueError):
                ip = 0.0
            twoday = s.get("two_day_pct")
            try:
                tw_v = float(twoday) if twoday is not None else None
            except (TypeError, ValueError):
                tw_v = None
            two_str = f" / 2日 {tw_v:+.2f}%" if tw_v is not None else ""
            lines.append(f"  {name} 今日 {ip:+.2f}%{two_str}")
        if vix is not None:
            try:
                vix_f = float(vix)
                if vix_f >= 30:
                    vix_zone = " (恐慌)"
                elif vix_f >= 20:
                    vix_zone = " (警戒)"
                else:
                    vix_zone = " (正常)"
                lines.append(f"  VIX 恐慌指數 {vix_f:.2f}{vix_zone}")
            except Exception:
                pass
        lines.append("")

    # ===== 規則式快評 (我自己, 不靠 Gemini) =====
    # 簡單的 rule-based action hint, 用於 Gemini 不可用時 fallback
    rule_hint = _rule_based_crash_verdict(triggers, ctx)
    if rule_hint:
        lines.append("<b>📐 規則式快評</b>")
        lines.append(rule_hint)
        lines.append("")

    # Fix #5: 國際新聞抓取失敗時, 提醒 AI 判斷可信度降低
    macro_news = ctx.get("macro_news")
    news_missing = (not macro_news) or (not str(macro_news).strip())
    if news_missing and ai_text:
        lines.append("<i>⚠️ 國際新聞抓取失敗, AI 判斷僅依價量資料, 可信度降低</i>")
        lines.append("")

    # ===== Gemini 深入分析 =====
    if ai_text:
        lines.append("<b>🤖 Gemini 動作建議</b>")
        # Fix #4: markdown → TG HTML (## 轉 <b>, **bold** 轉 <b>)
        lines.append(_md_to_tg_html(ai_text))
        lines.append("")
    else:
        lines.append("<i>(Gemini 暫不可用, 僅顯示規則式快評)</i>")
        lines.append("")

    lines.append("⚠️ 僅供參考, 不構成投資建議")

    return _truncate_tg_msg("\n".join(lines))


def _rule_based_crash_verdict(triggers: list, ctx: dict) -> str:
    """非 AI 的快速判斷: 用 VIX + 觸發數 + 跌幅深度 給粗略動作建議.

    回傳格式化 string. 失敗回空字串.
    """
    try:
        n_trig = len(triggers)
        max_intraday = min((float(t.get("intraday_pct", 0) or 0) for t in triggers), default=0.0)
        vix = ctx.get("vix")
        try:
            vix_f = float(vix) if vix is not None else None
        except (TypeError, ValueError):
            vix_f = None

        verdict_lines = []
        # 判斷類型: 系統性 vs 事件性
        if vix_f is not None and vix_f >= 28:
            verdict_lines.append("• 類型: 偏向<b>系統性風險</b> (VIX 已進入恐慌區)")
            action = "減碼或觀望"
        elif n_trig >= 3 and max_intraday <= -4:
            verdict_lines.append("• 類型: 偏向<b>系統性大跌</b> (多市場同步重挫)")
            action = "觀望, 避免接刀"
        elif n_trig <= 2 and (vix_f is None or vix_f < 22):
            verdict_lines.append("• 類型: 偏向<b>事件性回檔</b> (VIX 仍低, 非全面恐慌)")
            action = "可分批承接優質標的, 但不宜重押"
        else:
            verdict_lines.append("• 類型: 訊號混合 (跨市場走勢分歧)")
            action = "觀望等待 confirmation"

        verdict_lines.append(f"• 建議: <b>{action}</b>")
        verdict_lines.append(
            f"• 依據: 觸發 {n_trig} 檔標的, 最大盤中跌幅 {max_intraday:+.2f}%"
            + (f", VIX {vix_f:.1f}" if vix_f is not None else "")
        )
        return "\n".join(verdict_lines)
    except Exception:
        return ""


def _DEPRECATED_fmt_stop_loss_alerts(breaches: list) -> str:
    """(僅留作對照, 已被新版取代) 停損警報訊息."""
    if not breaches:
        return ""
    lines = ["<b>持倉停損警報</b>", ""]
    for b in breaches:
        sid = b.get("stock_id", "")
        name = b.get("name", "")
        cur = b.get("current", 0)
        sl = b.get("stop_loss", 0)
        try:
            pct = float(b.get("breach_pct", 0) or 0)
        except (TypeError, ValueError):
            pct = 0.0
        sign = "+" if pct > 0 else ""
        lines.append(
            f"<b><code>{_esc(sid)}</code></b> {_esc(name)} {_esc(cur)} 跌破停損 {_esc(sl)} "
            f"({sign}{pct:.2f}%)"
        )
        sd = b.get("set_date", "")
        if sd:
            lines.append(f"  停損設定日: {_esc(sd)}")
    return "\n".join(lines)


def fmt_holdings_accuracy(acc: dict) -> str:
    """持倉預測準確率摘要 (附在 tw_close 推播末)."""
    if not acc or not acc.get("total"):
        return ""
    lines = []
    lines.append(
        f"<b>過去 {acc.get('lookback_days',30)} 天預測準確率</b>: "
        f"{acc['correct']}/{acc['total']} ({acc['accuracy_pct']}%)"
    )
    by_prob = acc.get("by_prob_range") or {}
    parts = []
    for k in [">=70%", "50-70%", "<50%"]:
        v = by_prob.get(k, {})
        if v.get("total"):
            pct = round(v["correct"] / v["total"] * 100, 1)
            parts.append(f"{k}: {v['correct']}/{v['total']} ({pct}%)")
    if parts:
        lines.append("  分機率區間: " + " · ".join(parts))
    return "\n".join(lines)


def fmt_holdings_daily(holdings: list) -> str:
    """持倉清單盤後日報 — 每檔一段, 含技術/籌碼/Gemini 建議."""
    if not holdings:
        return ""
    lines = [f"<b>持倉日報 · {len(holdings)} 檔</b>", ""]

    def _to_f(v, default=0.0):
        try:
            f = float(v) if v is not None else default
            return f if not (math.isnan(f) or math.isinf(f)) else default
        except (TypeError, ValueError):
            return default

    # 摘要表
    summary_lines = []
    for h in holdings:
        sid = h.get("stock_id", "")
        name = h.get("name", "")
        tech = h.get("tech", {}) or {}
        adv = h.get("advice", {}) or {}
        cur = tech.get("current", 0)
        today = _to_f(tech.get("today_pct", 0))
        action = adv.get("action", "—")
        prob = adv.get("next_day_up_prob", 50)
        sign = "+" if today > 0 else ""
        summary_lines.append(
            f"  <code>{_esc(sid)}</code> {_esc(name)} {_esc(cur)} ({sign}{today:.2f}%) → "
            f"<b>{_esc(action)}</b> 隔日{_esc(prob)}%"
        )
    if summary_lines:
        lines.append("<b>摘要</b>")
        lines.extend(summary_lines)
        lines.append("")

    # 每檔詳細
    for h in holdings:
        sid = h.get("stock_id", "")
        name = h.get("name", "")
        ep = h.get("entry_price")
        tech = h.get("tech", {}) or {}
        chip = h.get("chip", {}) or {}
        adv = h.get("advice", {}) or {}
        news = h.get("news", []) or []

        cur = _to_f(tech.get("current", 0))
        today = _to_f(tech.get("today_pct", 0))
        sign = "+" if today > 0 else ""

        # 標題行
        head = f"<b>{_esc(sid)} {_esc(name)}</b> {cur} ({sign}{today:.2f}%)"
        if ep:
            try:
                ep_f = float(ep)
                roi = (cur / ep_f - 1) * 100 if ep_f > 0 else 0
                roi_sign = "+" if roi > 0 else ""
                head += f" · 進場{ep_f} ROI {roi_sign}{roi:.2f}%"
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        lines.append(head)

        # 技術
        tech_parts = []
        if tech.get("ma_status"): tech_parts.append(_esc(tech["ma_status"]))
        if tech.get("kd_signal"): tech_parts.append(f"KD {_esc(tech['kd_signal'])}")
        if tech.get("macd_signal"): tech_parts.append(f"MACD {_esc(tech['macd_signal'])}")
        if tech.get("vol_ratio"): tech_parts.append(f"量比{_esc(tech['vol_ratio'])}x")
        if tech_parts:
            lines.append("  技術: " + " · ".join(tech_parts))

        # 籌碼 — 沒值不顯示
        chip_parts = []
        fi5 = chip.get("fi_5d", 0) or 0
        if fi5:
            try:
                chip_parts.append(f"外資5日 {int(fi5):+,}張")
            except (TypeError, ValueError):
                pass
        it5 = chip.get("it_5d", 0) or 0
        if it5:
            try:
                chip_parts.append(f"投信5日 {int(it5):+,}張")
            except (TypeError, ValueError):
                pass
        m30 = _to_f(chip.get("margin_30d_pct", 0))
        if m30 and abs(m30) > 5:
            chip_parts.append(f"融資30日 {m30:+.1f}%")
        if chip_parts:
            lines.append("  籌碼: " + " · ".join(chip_parts))

        # 建議 (核心)
        action = adv.get("action", "持有")
        conf = adv.get("confidence", 0)
        ts = adv.get("target_short")
        tm = adv.get("target_mid")
        sl = adv.get("stop_loss")
        prob = adv.get("next_day_up_prob", 50)

        action_line = f"  建議: <b>{_esc(action)}</b> (信心 {_esc(conf)}%) · 隔日漲機率 {_esc(prob)}%"
        lines.append(action_line)
        price_parts = []
        if ts: price_parts.append(f"短期目標 {_esc(ts)}")
        if tm: price_parts.append(f"中期目標 {_esc(tm)}")
        if sl: price_parts.append(f"停損 {_esc(sl)}")
        if price_parts:
            lines.append("  " + " · ".join(price_parts))

        reason = adv.get("reason", "")
        if reason:
            lines.append(f"  理由: {_esc(reason)}")
        risks = adv.get("risks", "")
        if risks and risks != "—":
            lines.append(f"  風險: {_esc(risks)}")

        # 最近新聞 (最多 2 條)
        if news:
            for n in news[:2]:
                lines.append(f"  - {_esc(n.get('title',''))}")

        lines.append("")

    # G7 fix: byte-length truncation (取代舊的 char-based len() 檢查)
    return _truncate_tg_msg("\n".join(lines))



def fmt_us_open_picks(data: dict, ai_text: str = "") -> str:
    """美股開盤後 30 分推播."""
    if data.get("error"):
        return f"美股開盤分析：{_esc(data['error'])}"
    lines = [f"<b>美股開盤後 30 分鐘 · 資金流向</b>"]
    # Regime banner
    regime = data.get("regime") or {}
    if regime:
        try:
            import regime_detector
            banner = regime_detector.fmt_regime_banner(regime)
            if banner:
                lines.append("")
                lines.append(banner)
        except Exception:
            pass
    # 大盤預測
    lines.extend(_fmt_prediction_block(data.get("prediction"), data.get("accuracy")))
    # 國際訊號
    lines.extend(_fmt_external_signals_block())
    # 美股版萌芽 sector (RS vs SPY 突破)
    us_emerging = data.get("emerging") or []
    if us_emerging:
        try:
            import emerging_themes
            lines.extend(emerging_themes.fmt_emerging_themes_block(us_emerging))
        except Exception:
            pass
    sectors = data.get("sectors")
    if sectors is not None and not sectors.empty:
        lines.append("")
        for _, row in sectors.iterrows():
            sym = row.get("symbol")
            sname = row.get("sector", "")
            # 防 None — fetch_sector_rotation 資料不足時 1d_% 會是 None
            lines.append(f"<b>{_esc(sym)} {_esc(sname)}</b>  1d {_safe_pct(row.get('1d_%'))}")

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
            lines.append(f"\n<b>[{_esc(sec)}]</b>")
            for _, s in stocks.iterrows():
                sym = s.get("symbol", "")
                today = s.get("今日%")
                ratio = s.get("量比")
                twenty = s.get("20日%")
                lines.append(
                    f"  • <b><code>{_esc(sym)}</code></b>  "
                    f"今日 {_esc(today)}% · 量比 {_esc(ratio)}x · 20d {_esc(twenty)}%"
                )
                cat = catalysts.get(str(sym))
                if cat:
                    lines.append(f"    催化劑: {_esc(cat)}")
                ev = events.get(str(sym))
                if ev and ev.get("summary") and ev["summary"] != "—":
                    lines.append(f"    財報: {_esc(ev['summary'])}")

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
                f"  • <b><code>{_esc(sym)}</code></b>  "
                f"今日 {_esc(today)}% · 20d {_esc(twenty)}% · 量比 {_esc(ratio)}x · {_esc(score)}/10"
            )
            cat = catalysts.get(str(sym))
            if cat:
                lines.append(f"    催化劑: {_esc(cat)}")
            ev = events.get(str(sym))
            if ev and ev.get("summary") and ev["summary"] != "—":
                lines.append(f"    財報: {_esc(ev['summary'])}")

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
                lines.append(f"<b>{_esc(s[3:])}</b>")
            else:
                lines.append(_esc(line))

    # G7 fix: byte-length truncation (取代舊的 char-based len() 檢查)
    return _truncate_tg_msg("\n".join(lines))


def fmt_ai_analysis(stock_id: str, name: str, ai_text: str) -> str:
    """AI 個股分析推送格式. Telegram 訊息上限 4096 字，必要時截斷."""
    head = f"<b>🤖 AI 深度分析 — {_esc(stock_id)} {_esc(name)}</b>\n"
    body = ai_text or ""
    out_lines = []
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("## "):
            out_lines.append(f"<b>{_esc(s[3:])}</b>")
        else:
            out_lines.append(_esc(line))
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
        themes = "、".join(str(t) for t in hot_themes_df["題材"].head(3).tolist())
        lines.append(f"<i>熱門題材: {_esc(themes)}</i>")
    lines.append("")
    for i, row in stealth_df.head(15).iterrows():
        sid = row.get("stock_id", "")
        name = row.get("stock_name", "")
        theme = row.get("題材", "")
        today = row.get("今日%", "—")
        five = row.get("5日%", "—")
        ratio = row.get("量比", "—")
        lines.append(f"{i+1}. <b><code>{_esc(sid)}</code></b> {_esc(name)}  [{_esc(theme)}]")
        lines.append(f"   今日 {_esc(today)}% / 5d {_esc(five)}% / 量比 {_esc(ratio)}x")
        cat = row.get("催化劑")
        if cat and str(cat).strip() and str(cat).strip() != "—":
            lines.append(f"   催化劑: {_esc(cat)}")
    return "\n".join(lines)


def fmt_growth_picks(picks_df) -> str:
    if picks_df is None or picks_df.empty:
        return "🌱 成長動能 Top 10：今日無符合條件的標的。"
    lines = ["<b>🌱 成長動能 Top 10 (消息面 + K 線健康度)</b>", ""]
    for i, r in picks_df.iterrows():
        # 改 .get + fallback "—" 避免 KeyError 炸整封推播
        # (上游 news_picks Gemini 失敗時可能缺欄)
        sid = r.get("代號") if hasattr(r, "get") else "—"
        nm = r.get("名稱") if hasattr(r, "get") else "—"
        sc = r.get("score") if hasattr(r, "get") else "—"
        if not sid:
            continue  # 沒代號 skip
        lines.append(
            f"{i+1}. <b><code>{_esc(sid)}</code></b> {_esc(nm)} · "
            f"{_esc(r.get('題材',''))} · {_esc(sc)}/10"
        )
        if r.get("理由"):
            lines.append(f"   {_esc(r.get('理由'))}")
    return "\n".join(lines)



def fmt_watchlist_alert(stock_id: str, name: str, hits: list, latest_date: str,
                         row: dict | None = None) -> str:
    """watchlist 命中通知，含詳細數值。"""
    head = f"<b>🔔 自選股警報 — {_esc(stock_id)} {_esc(name)}</b>\n資料日期: {_esc(latest_date)}"
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
    body.append("命中: " + ", ".join(_esc(h) for h in hits))
    return "\n".join(body)


def fmt_fear_greed_alert(fg: dict, threshold_low: int = 25, threshold_high: int = 75) -> Optional[str]:
    """美股 CNN F&G 極值警報. 中性區間回 None."""
    if not fg or fg.get("score") is None:
        return None
    try:
        s = float(fg["score"])
    except (TypeError, ValueError):
        return None
    rating = _esc(fg.get("rating", ""))
    if s <= threshold_low:
        return (f"⚠️ <b>美股市場極度恐慌</b>\n"
                f"CNN F&amp;G Index: <b>{s:.0f}</b> ({rating})\n"
                "歷史經驗為逢低分批布局訊號，仍須個股控管風險。")
    if s >= threshold_high:
        return (f"⚠️ <b>美股市場極度貪婪</b>\n"
                f"CNN F&amp;G Index: <b>{s:.0f}</b> ({rating})\n"
                "歷史經驗為短期回檔風險偏高訊號，建議分批減碼或停利。")
    return None



def fmt_tw_pulse_alert(pulse: dict, threshold_low: int = 25, threshold_high: int = 75) -> Optional[str]:
    """台股市場情緒指數極值警報. 中性區間回 None."""
    if not pulse or pulse.get("score") is None:
        return None
    s = pulse["score"]
    raw = pulse.get("raw") or {}
    twii = _esc(raw.get("TWII"))
    pct5 = _esc(raw.get("5日%"))
    ma60 = _esc(raw.get("距 MA60 %"))
    rating = _esc(pulse.get("rating_zh"))
    if s <= threshold_low:
        return (f"⚠️ <b>台股市場極度恐慌</b>\n台股情緒指數: {_esc(s)} ({rating})\n"
                f"加權: {twii} · 5日 {pct5}% · 距 MA60 {ma60}%\n"
                "歷史經驗為逢低布局訊號，仍須個股控管風險。")
    if s >= threshold_high:
        return (f"⚠️ <b>台股市場極度貪婪</b>\n台股情緒指數: {_esc(s)} ({rating})\n"
                f"加權: {twii} · 5日 {pct5}% · 距 MA60 {ma60}%\n"
                "歷史經驗為短期回檔風險偏高訊號，建議分批減碼或停利。")
    return None
