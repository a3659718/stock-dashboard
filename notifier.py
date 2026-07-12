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


def _esc_attr(s) -> str:
    """escape 給 HTML 屬性值用 (e.g. <a href="...">) — 連 " ' & < > 都 escape.

    新聞網址常含 `&` (?a=x&b=y), 直接塞進 href 會被 Telegram 判成壞 entity → 400,
    連結消失。屬性 context 必須連引號一起 escape。
    """
    if s is None:
        return ""
    try:
        return _html.escape(str(s), quote=True)
    except Exception:
        return ""


def _strip_caret(sym: str) -> str:
    """剝 ^TWII → TWII (yfinance ticker 顯示給用戶看的清理)."""
    if not sym:
        return ""
    s = str(sym)
    return s[1:] if s.startswith("^") else s


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


def _truncate_tg_msg(out: str, max_chars: int = 4000) -> str:
    """Telegram 限 4096 個「字元」(UTF-16 code units), 不是 bytes.

    Bug fix: 舊版註解誤以為限 4096 BYTES, 用 byte 切 (3900 bytes). 但中文 1 字 = 1 個
    TG 字元卻佔 3 bytes → 中文訊息只能放 ~1300 字就被砍, 而 TG 其實能放 ~4096 字。
    這正是「急報等中文推播文字被截斷」的主因。改用 UTF-16 長度判斷後, 中文可用篇幅
    約是舊版的 3 倍, 大多數訊息不再需要截斷。
    (emoji 等非 BMP 字元 = 2 個 UTF-16 units, 已被正確計入; max_chars=4000 留 margin。)

    第二參數沿用位置呼叫 (ipo_calendar_alert 傳 3900) — 現在語意是「字元數」, 仍安全。
    """
    if not out:
        return ""
    u16 = out.encode("utf-16-le")
    if len(u16) // 2 <= max_chars:  # TG 長度 = UTF-16 code units
        return out
    # 砍到 (max_chars - 8) 個 unit; errors='ignore' 會丟掉切到一半的 surrogate pair
    truncated = u16[: (max_chars - 8) * 2].decode("utf-16-le", errors="ignore")
    # 砍掉結尾半截 HTML tag (避免 parse_mode=HTML 回 400)
    last_lt = truncated.rfind("<")
    last_gt = truncated.rfind(">")
    if last_lt > last_gt:  # 最後一個 '<' 之後沒有 '>' → 結尾是半截 tag
        truncated = truncated[:last_lt]
    truncated += "\n…(節錄)"
    return _balance_html_tags(truncated)


def _balance_html_tags(s: str) -> str:
    """補齊未閉合的常見 Telegram HTML tag (b/i/code/pre/u/s).
    Telegram HTML 模式要求 tag 成對, 截斷後可能少了 </b> 之類 → 補在尾端.
    """
    import re as _re
    for tag in ("b", "i", "code", "pre", "u", "s"):
        opens = len(_re.findall(rf"<{tag}>", s))
        closes = len(_re.findall(rf"</{tag}>", s))
        if opens > closes:
            s += f"</{tag}>" * (opens - closes)
    return s


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

    callback_data 短碼定義 (scripts/tg_callback_listener.py 負責配對處理):
      wl:<mkt>:<sid>   加入自選
      ai:<mkt>:<sid>   AI 深入分析
      sl:<mkt>:<sid>   設停損 (依現價算 -8% 建議停損)
      tv:<sid>         開 TradingView (url button, 不需 handler)

    callback button 由 tg_callback_listener long-poll getUpdates 處理;
    listener 沒跑時 url button (TradingView) 仍可直接跳轉, callback button 會
    顯示轉圈 (Telegram 端逾時), 但不會崩, 也不影響主訊息.

    註: callback_data 帶 market, 讓 handler 知道用台股還是美股資料源.
        舊格式 wl:<sid> (無 market) listener 仍向後相容 (由 sid 是否純數字推斷).
    """
    mkt = "US" if str(market).upper() == "US" else "TW"
    if mkt == "US":
        tv_url = f"https://www.tradingview.com/symbols/{stock_id}/"
    else:
        tv_url = f"https://www.tradingview.com/symbols/TWSE-{stock_id}/"
    # callback_data 上限 64 bytes; "wl:US:" 6 + sid → 留 32 給 sid 綽綽有餘
    sid_short = (stock_id or "")[:32]
    return {
        "inline_keyboard": [
            [
                {"text": "➕ 加自選", "callback_data": f"wl:{mkt}:{sid_short}"},
                {"text": "🤖 AI 分析", "callback_data": f"ai:{mkt}:{sid_short}"},
            ],
            [
                {"text": "🛡️ 設停損", "callback_data": f"sl:{mkt}:{sid_short}"},
                {"text": "📊 看圖", "url": tv_url},
            ],
        ]
    }


def _record_push_safe(text: str, ok: bool, info: str) -> None:
    """安全記錄推播 (system_health), 任何例外都吞掉 — 不能影響 send_message 主流程."""
    try:
        import system_health
        # 從第一行抓 type (剝 emoji / 取前 50 字)
        ptype = (text or "").split("\n", 1)[0][:50] if text else "?"
        system_health.record_push(ptype, ok, error_msg=None if ok else info[:200])
    except Exception:
        pass


def send_message(text: str, disable_preview: bool = True,
                  reply_markup: Optional[dict] = None,
                  disable_notification: bool = False,
                  category: Optional[str] = None,
                  **_kwargs) -> tuple[bool, str]:
    """直接呼叫 Bot API。回傳 (成功, 訊息).

    強化點:
      1. token / chat_id 自動 strip 前後空白
      2. HTML parse 失敗 → 自動 retry 一次純文字 (避免單一字元擋住整則推播)
      3. 失敗時把更多診斷資訊 (chat_id 長度、message 前 80 字) 包進 info
      4. reply_markup: optional inline keyboard (來自 build_stock_action_keyboard)
      5. 對 None / 空 text early return, 避免 text[:80] 在診斷字串炸 TypeError
      6. 自動寫入 system_health.push_history (供 admin dashboard 顯示)
      7. disable_notification=True → silent push (不響鈴, 用於普通新聞分流)
      8. category: 若有傳 (e.g. "volume_breakout"), 算 daily cap; 沒傳 = 主推不算 cap
    """
    # === 深夜靜音 (TPE 02:00-06:00) — 用戶要求: 推但不響鈴, 不中斷睡眠 ===
    try:
        import datetime as _dt_q
        _hr_tpe = (_dt_q.datetime.utcnow() + _dt_q.timedelta(hours=8)).hour
        if 2 <= _hr_tpe < 6 and not disable_notification:
            disable_notification = True
            print(f"[notifier] quiet hours (TPE {_hr_tpe}:xx), force silent push", flush=True)
    except Exception:
        pass

    # === 次要 alert daily cap (用戶要求: cap 6 封/日) ===
    # 只有傳 category 的 caller 算 cap; 主推 (us_open / 反轉 / pre_market 等) 不傳 → 不算
    if category:
        try:
            import push_cap as _pc
            if not _pc.check_and_consume(category):
                print(f"[notifier] daily cap reached, skip {category}", flush=True)
                return False, f"daily_cap_skip:{category}"
        except Exception as _ce:
            print(f"[notifier] push_cap check fail (continue anyway): {_ce}", flush=True)

    # Bug fix: 加 3 次 retry — 網路抖動 / TG API 暫時 503 不會漏推
    import time as _time
    last_info = ""
    for attempt in range(3):
        ok, info = _send_message_inner(text, disable_preview, reply_markup, disable_notification)
        last_info = info
        if ok:
            if attempt > 0:
                print(f"[notifier] send_message OK on retry #{attempt+1}", flush=True)
            _record_push_safe(text, ok, info)
            return ok, info
        # 失敗 — 短暫 backoff 後重試 (except 永久性錯誤: chat_id 沒設 / token 錯)
        if any(k in info for k in ("尚未設定", "chat not found", "Forbidden", "Unauthorized")):
            break  # 永久錯不重試
        if attempt < 2:
            _time.sleep(2 ** attempt)  # 1s, 2s exponential backoff
    _record_push_safe(text, False, last_info)
    return False, last_info


def _send_message_inner(text: str, disable_preview: bool = True,
                          reply_markup: Optional[dict] = None,
                          disable_notification: bool = False) -> tuple[bool, str]:
    """實際送出 — 拆出避免 record_push hook 影響原邏輯."""
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
        "disable_notification": bool(disable_notification),
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
            pass  # 量比已移除
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

    return _truncate_tg_msg("\n".join(lines))


def fmt_emerging_breakout(data: dict, top_n: int = 10) -> str:
    """美股『新興突破』掃描推播 (us_screener.run_emerging_breakout 的輸出)."""
    df = data.get("top_picks") if isinstance(data, dict) else None
    if df is None or getattr(df, "empty", True):
        return "🚀 <b>美股新興突破掃描</b>\n\n今日無符合條件的突破標的 (流動性 / 過熱過濾後)。"
    n = min(top_n, len(df))
    lines = [
        f"🚀 <b>美股新興突破 Top {n}</b> · 盤後掃描",
        f"<i>掃 {data.get('scanned','?')}/{data.get('universe_size','?')} 檔 · 🆕=精選池外新標的 · ⚠️=過熱降評</i>",
        "",
    ]
    for i, (_, r) in enumerate(df.head(top_n).iterrows(), 1):
        sym = _esc(str(r.get("symbol", "")))
        new_tag = "🆕 " if r.get("池外") else ""
        lines.append(f"{i}. {new_tag}<b><code>{sym}</code></b>  分數 {_fmt_num(r.get('score'))}")
        seg = []
        if r.get("last") is not None:
            seg.append(f"${_fmt_num(r.get('last'))}")
        if r.get("daily_%") is not None:
            seg.append(f"日 {_safe_pct(r.get('daily_%'))}")
        if r.get("20d_%") is not None:
            seg.append(f"20d {_safe_pct(r.get('20d_%'))}")
        if r.get("RS_20d") is not None:
            seg.append(f"RS {_fmt_num(r.get('RS_20d'))}")
        if r.get("量比"):
            seg.append(f"量比 {_fmt_num(r.get('量比'))}x")
        if seg:
            lines.append("   " + " · ".join(seg))
        if r.get("題材"):
            lines.append(f"   題材: {_esc(str(r.get('題材')))}")
        if r.get("專家"):
            lines.append(f"   👑 大戶: {_esc(str(r.get('專家')))}")
        if r.get("過熱警示"):
            lines.append(f"   ⚠️ {_esc(str(r.get('過熱警示')))}")
        lines.append("")
    lines.append("<i>※ 技術動能 + 內部人/分析師背書; 非投資建議, 池外標的風險較高, 嚴守風控.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_us_top_picks(df, fg: dict, top_n: int = 10) -> str:
    if df is None or df.empty:
        return f"美股 Top {top_n} 推薦：今日無符合篩選條件的標的。"
    score = fg.get("score") if fg else None
    rating = fg.get("rating") if fg else None
    try:
        fg_line = f"恐慌指數 {round(float(score),1)} ({_esc(rating)})" if score is not None else "恐慌指數 N/A"
    except (TypeError, ValueError):
        fg_line = "恐慌指數 N/A"
    lines = [f"<b>美股 Top {top_n} 推薦</b> · {fg_line}", ""]
    for i, row in df.head(top_n).iterrows():
        # pandas Series .get(key, default) 缺 key 時回 default; 但 None 值仍會回 None
        # → 用 `or "—"` 同時擋 None 跟空字串
        sym = row.get("symbol") or "—"
        sc = row.get("score") or "—"
        theme_v = row.get("題材")
        expert_v = row.get("專家")
        overheat_v = row.get("過熱警示")
        lines.append(
            f"{i+1}. <b><code>{_esc(sym)}</code></b>  "
            f"日 {_esc(row.get('daily_%'))}% / 20d {_esc(row.get('20d_%'))}% · 分數 {_esc(sc)}"
            + (f"\n   題材: {_esc(theme_v)}" if theme_v else "")
            + (f"\n   👑 大戶: {_esc(expert_v)}" if expert_v else "")
            + (f"\n   ⚠️ {_esc(overheat_v)}" if overheat_v else "")
        )
    return _truncate_tg_msg("\n".join(lines))


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
                                pass  # 量比已移除
                            except Exception:
                                pass
                        details = " · ".join(parts) if parts else ""
                        lines.append(f"  <code>{_esc(sid)}</code> {_esc(nm)}  {details}")
                        # 催化劑 (若 DataFrame 有此欄)
                        cat = sr.get("催化劑") if hasattr(sr, "get") else None
                        if cat and str(cat).strip() and str(cat).strip() != "—":
                            pass  # 催化劑已移除
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
                                pass  # 量比已移除
                            except Exception:
                                pass
                        details = " · ".join(parts) if parts else ""
                        lines.append(f"  <code>{_esc(sid)}</code> {_esc(nm)}  {details}")
                        # 催化劑 (compute_hot_themes 已塞 "催化劑" 欄)
                        cat = sr.get("催化劑") if hasattr(sr, "get") else None
                        if cat and str(cat).strip() and str(cat).strip() != "—":
                            pass  # 催化劑已移除
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
    # Bug fix: 移除「量比」功能時留下孤兒 `raw_pass`(未定義變數的裸語句), 只要預測有
    #          vol_ratio 就 NameError → 炸掉整封 us_open/us_mid 推播. 整段死碼移除。
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

            out.append(f"  <b><code>{_esc(sid)}</code></b> {_esc(nm)}  今日 {_esc(tp)}% ")
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
        out.append(f"<b>WTI 油價: ${_fmt_num(oil.get('price'))} ({_safe_pct(oil.get('pct_5d'), '+.1f', suffix='% 5d')})</b>")
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
                    f"今日 {_esc(today)}% · 5d {_esc(five)}%"
                )
                cat = catalysts.get(str(sid))
                if cat:
                    pass  # 催化劑已移除
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
            # 明確欄位名 (watchlist_alerts 已改): pct_vs_open = vs 開盤, pct_vs_prior = vs 昨收
            pct_vs_open = a.get("pct_vs_open")
            pct_vs_prior = a.get("pct_vs_prior")

            sign = "+" if primary_pct > 0 else ""
            # 主行 (取較極端那個錨點)
            lines.append(
                f"<b><code>{_esc(sid)}</code></b> {_esc(name)} {_fmt_num(cur)} <b>{_esc(d)}{thr}%</b> "
                f"{sign}{primary_pct:.2f}% vs {_esc(anchor_label)} {_fmt_num(anchor_price)}"
            )
            # 次行 — 顯示另一個錨點對照 (如果兩個都有)
            other_parts = []
            if pct_vs_open is not None and a.get("primary_anchor") != "open":
                s2 = "+" if pct_vs_open > 0 else ""
                other_parts.append(f"開盤 {s2}{pct_vs_open:.2f}%")
            if pct_vs_prior is not None and a.get("primary_anchor") != "close":
                s2 = "+" if pct_vs_prior > 0 else ""
                other_parts.append(f"昨收 {s2}{pct_vs_prior:.2f}%")
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

    # crypto_alerts 已停用 (用戶要求取消加密貨幣)
    # if crypto_alerts: skip


def fmt_weekend_recap(data: dict) -> str:
    """週末重點摘要 (Sat/Sun 22:00 TPE).

    複用 fmt_holiday_news 的主要區塊 + 加入 weekend-specific 內容:
    - 7d 全球指數表現
    - Crypto 7d
    - ETF 持股 snapshot
    - Gemini 下週展望
    """
    if not data:
        return "週末摘要: 資料不足"

    # 先拿 fmt_holiday_news 的基底 (但改標題)
    base = fmt_holiday_news(data)
    # 把第一行從「台股休市日 · 全球重大消息整理」改成「週末重點摘要」
    base = base.replace("台股休市日 · 全球重大消息整理", "週末重點摘要 · 全球市場與下週展望", 1)

    extras = ["", "------ 一週表現 (5d) ------"]
    week_perf = data.get("week_perf") or {}
    if week_perf:
        for sym, info in week_perf.items():
            try:
                name = _esc(info.get("name", sym))
                last = float(info.get("last", 0) or 0)
                pct = float(info.get("pct_5d", 0) or 0)
            except (TypeError, ValueError):
                continue
            arrow = "📈" if pct >= 0 else "📉"
            extras.append(f"{arrow} {name}: {last:,.2f} ({pct:+.2f}%)")

    etf_snapshot = data.get("etf_snapshot") or []
    if etf_snapshot:
        extras.append("")
        extras.append("------ 主動式 ETF top 5 持股 ------")
        for e in etf_snapshot:
            code = _esc(e.get("etf_code", ""))
            name = _esc(e.get("etf_name", ""))
            dd = _esc(e.get("data_date", "—"))
            extras.append(f"<b>{code}</b> {name} ({dd})")
            for s in e.get("top5", [])[:5]:
                try:
                    sid = _esc(s.get("sid", ""))
                    sn = _esc(s.get("name", ""))
                    pct = float(s.get("pct", 0) or 0)
                    extras.append(f"  <code>{sid}</code> {sn}: {pct:.2f}%")
                except (TypeError, ValueError):
                    continue

    outlook = data.get("next_week_outlook") or ""
    if outlook:
        extras.append("")
        extras.append("------ 🔭 下週展望 (Gemini) ------")
        extras.append(_md_to_tg_html(outlook))

    # === 整合: 下週美股 IPO 預告 (週末用) ===
    # 週末給美股 IPO 列表 (台股 IPO 週五晚已單獨推)
    try:
        import ipo_calendar_alert as _ipo
        ipo_msg = _ipo.build_us_ipo_preview_msg()
        if ipo_msg:
            extras.append("")
            extras.append("------ 🇺🇸 下週美股 IPO ------")
            ipo_clean = "\n".join(ipo_msg.split("\n")[1:])
            extras.append(ipo_clean)
    except Exception as _ie:
        print(f"[weekend_recap] us ipo preview fail: {_ie}", flush=True)

    return _truncate_tg_msg(base + "\n" + "\n".join(extras))


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
        lines.append(f"🛢 WTI 油價: ${_fmt_num(oil.get('price'))} ({_safe_pct(oil.get('pct_5d'), '+.1f', suffix='% 5d')})")
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
        return f"[●  區間  ]  低於下緣 {dist_pct:.2f}% (可進場)"
    if cur > eh:
        dist_pct = (cur - eh) / eh * 100
        return f"[  區間  ●]  高於上緣 {dist_pct:.2f}% (追高警告)"
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
        out.append("(本日無 R:R ≥ {:.2f} 的標的)".format(rr_threshold))
        return out

    for i, (p, rr) in enumerate(enriched, 1):
        sid = _esc(p.get("stock_id", ""))
        nm = _esc(p.get("name", ""))
        theme = _esc(p.get("theme", ""))
        cur_raw = p.get("current", 0)
        cur = _fmt_num(cur_raw)          # 價格一律 2 位小數
        el_raw = p.get("entry_low", 0)
        eh_raw = p.get("entry_high", 0)
        e_low = _fmt_num(el_raw)
        e_high = _fmt_num(eh_raw)
        target = _fmt_num(p.get("target_price", 0))
        target_pct = _safe_int_or_dash(p.get("target_pct", 0))
        stop = _fmt_num(p.get("stop_loss", 0))
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

    # === C: 美股持倉動作建議 (RSI 背離 + 組合風險) ===
    try:
        import rsi_divergence as _rd
        divs = _rd.scan_holdings_for_divergence()
        us_divs = [d for d in divs if d.get("market") == "US"]
        if us_divs:
            bear = [d for d in us_divs if d.get("type") == "bearish"]
            bull = [d for d in us_divs if d.get("type") == "bullish"]
            if bear or bull:
                lines.append("")
                lines.append("━━━━━━━ 🚨 美股持倉動作建議 ━━━━━━━")
                for d in bear:
                    sid = _esc(d.get("symbol", ""))
                    strength = d.get("strength", 1)
                    if strength >= 2:
                        lines.append(f"  ⚠️ <code>{sid}</code> <b>建議減碼</b> (動能衰竭)")
                    else:
                        lines.append(f"  🟡 <code>{sid}</code> <b>留意減碼</b>")
                for d in bull:
                    sid = _esc(d.get("symbol", ""))
                    strength = d.get("strength", 1)
                    if strength >= 2:
                        lines.append(f"  ✅ <code>{sid}</code> <b>反彈在即</b> (可加碼)")
                    else:
                        lines.append(f"  🔺 <code>{sid}</code> <b>留意反彈</b>")
    except Exception as _ce:
        print(f"[us_close] rsi_div fail: {_ce}", flush=True)

    # === C: 組合風險 (只看 US 持倉) ===
    try:
        import portfolio_risk as _pr
        risk = _pr.analyze_portfolio_risk()
        if risk and risk.get("holdings_n", 0) > 0:
            warns = risk.get("warnings", [])
            real_warns = [w for w in warns if "🔴" in w or "🟡" in w]
            if real_warns:
                lines.append("")
                lines.append("━━━━━━━ ⚠️ 組合風險 ━━━━━━━")
                for w in real_warns[:3]:
                    lines.append(f"  {w}")
    except Exception as _pe:
        print(f"[us_close] portfolio_risk fail: {_pe}", flush=True)

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
            lines.append(f"     上漲機率 <b>{_esc(up_prob)}%</b> · 預期 +{target:.2f}%")
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


def fmt_combined_intraday_alerts(
    crash_data: Optional[dict],
    reversal_alerts: Optional[list],
    bucket_alerts: Optional[list],
    crash_ai_text: str = "",
) -> str:
    """合併 3 種大盤類警報為一封 TG (per cron tick).

    減少訊息頻率: 同 cron tick 即使 crash + reversal + bucket 全觸發, 只推 1 封.
    Same symbol 的多個觸發類型 group 在一起顯示.

    Args:
      crash_data: index_alerts.check_systemic_crash() return (None 表示沒觸發)
      reversal_alerts: index_alerts.check_intraday_reversal() return (list, 空表示沒)
      bucket_alerts: index_alerts.check_index_alerts() return (list, 空表示沒)
      crash_ai_text: Gemini 對 crash 的分析 (有 crash 才放, 不重複到 reversal 等)
    """
    has_crash = bool(crash_data and crash_data.get("triggers"))
    has_reversal = bool(reversal_alerts)
    has_bucket = bool(bucket_alerts)

    if not (has_crash or has_reversal or has_bucket):
        return ""

    # 集合所有 symbol — index by symbol, 把各種警報塞進去
    per_sym: Dict[str, Dict] = {}

    if has_crash:
        for t in crash_data.get("triggers", []) or []:
            sym = t.get("symbol", "")
            if not sym:
                continue
            per_sym.setdefault(sym, {"name": t.get("name", sym), "country": t.get("country", "")})
            per_sym[sym]["crash"] = t

    if has_reversal:
        for a in reversal_alerts or []:
            sym = a.get("symbol", "")
            if not sym:
                continue
            per_sym.setdefault(sym, {"name": a.get("name", sym), "country": a.get("country", "")})
            per_sym[sym].setdefault("reversals", []).append(a)

    if has_bucket:
        for a in bucket_alerts or []:
            sym = a.get("symbol", "")
            if not sym:
                continue
            per_sym.setdefault(sym, {"name": a.get("name", sym), "country": a.get("country", "")})
            per_sym[sym]["bucket"] = a

    if not per_sym:
        return ""

    # ===== 訊息標題 — 用最高優先級的圖示 =====
    if has_crash:
        _crash_dirs = {(info.get("crash") or {}).get("direction", "down")
                       for info in per_sym.values() if info.get("crash")}
        if _crash_dirs == {"up"}:
            title = "🚀 盤中重要事件 (含系統性大漲)"
        elif _crash_dirs == {"down"}:
            title = "🚨 盤中重要事件 (含系統性大跌)"
        else:
            title = "🚨 盤中重要事件 (系統性大漲/大跌)"
    elif has_reversal:
        title = "🔄 盤中反轉警報"
    else:
        title = "📈 盤中警報"

    lines = [f"<b>{title}</b>", ""]

    for sym, info in per_sym.items():
        sym_esc = _esc(sym)
        name_esc = _esc(info.get("name", sym))
        country = _esc(info.get("country", ""))
        country_tag = f"[{country}] " if country else ""

        # 收集所有 anchor 價格 (從任一觸發源取得; reversal 通常最完整)
        anchors: dict = {}
        srcs = [info.get("crash"), info.get("bucket")]
        srcs.extend(info.get("reversals", []) or [])
        for src in srcs:
            if not isinstance(src, dict):
                continue
            for k in ("today_open", "today_high", "today_low", "current", "prior_close"):
                if k not in anchors and src.get(k) not in (None, ""):
                    try:
                        anchors[k] = float(src[k])
                    except (TypeError, ValueError):
                        pass
        today_open = anchors.get("today_open")
        today_high = anchors.get("today_high")
        today_low = anchors.get("today_low")
        current = anchors.get("current")
        prior_close = anchors.get("prior_close")

        # === Symbol 主行: 加 ▼▲ 點數 + % vs 開盤 + 當日% vs 昨收 ===
        cur_str = f"{current:,.2f}" if current is not None else "—"
        header_extra = ""
        if today_open is not None and current is not None and today_open > 0:
            diff_open = current - today_open
            pct_open = (current / today_open - 1) * 100
            arrow_h = "▲" if diff_open > 0 else ("▼" if diff_open < 0 else "—")
            sign_p = "+" if pct_open > 0 else ""
            header_extra = f"  {arrow_h}{abs(diff_open):,.0f} 點 ({sign_p}{pct_open:.2f}% vs 開)"
        # 加「當日 vs 昨收」= 看盤軟體顯示的當日漲跌幅, 讓百分比對得上實際盤面
        # (反轉-only 訊息原本只有 vs 開盤 / 從高低點, 沒有 vs 昨收 → 數字對不上會覺得怪)
        day_extra = ""
        if prior_close is not None and current is not None and prior_close > 0:
            pct_pc = (current / prior_close - 1) * 100
            sign_d = "+" if pct_pc > 0 else ""
            day_extra = f"  · 當日 {sign_d}{pct_pc:.2f}% (vs 昨收)"
        lines.append(f"{country_tag}<b>{name_esc}</b> <code>{sym_esc}</code> {cur_str}{header_extra}{day_extra}")

        # === 今日 K 線走勢 line (4 個 key 價位, 視覺一目了然) ===
        if today_open is not None and current is not None:
            path_parts = [f"開 {today_open:,.0f}"]
            if today_high is not None:
                path_parts.append(f"高 {today_high:,.0f}")
            if today_low is not None:
                path_parts.append(f"低 {today_low:,.0f}")
            path_parts.append(f"現 {current:,.0f}")
            lines.append(f"  📊 今日 K 線: " + " / ".join(path_parts))

        # === Crash 觸發 (最高優先, 訊息最豐富) ===
        crash_t = info.get("crash")
        if crash_t:
            try:
                tval = float(crash_t.get("trigger_value", 0) or 0)
                pct_o = float(crash_t.get("pct_vs_open", 0) or 0)
                pct_p = float(crash_t.get("pct_vs_prior", 0) or 0)
                today_open_c = float(crash_t.get("today_open", today_open or 0) or 0)
                prior_close_c = float(crash_t.get("prior_close", prior_close or 0) or 0)
                cur_c = float(crash_t.get("current", current or 0) or 0)
            except (TypeError, ValueError):
                tval = pct_o = pct_p = today_open_c = prior_close_c = cur_c = 0.0
            ttype = crash_t.get("trigger_type", "intraday")
            ttype_zh = "盤中" if ttype == "intraday" else "連2日累計"
            _is_up = (crash_t.get("direction", "down") == "up")
            _move_emoji = "🚀" if _is_up else "🚨"
            _move_zh = "系統性大漲" if _is_up else "系統性大跌"
            diff_o = cur_c - today_open_c
            diff_p = cur_c - prior_close_c
            _arr_o = "▲" if diff_o > 0 else ("▼" if diff_o < 0 else "—")
            _arr_p = "▲" if diff_p > 0 else ("▼" if diff_p < 0 else "—")
            lines.append(f"  {_move_emoji} <b>{_move_zh}觸發</b> ({ttype_zh} {tval:+.2f}%)")
            lines.append(
                f"     vs 開盤 {today_open_c:,.0f}: {_arr_o}{abs(diff_o):,.0f} 點 ({pct_o:+.2f}%)"
            )
            lines.append(
                f"     vs 昨收 {prior_close_c:,.0f}: {_arr_p}{abs(diff_p):,.0f} 點 ({pct_p:+.2f}%)"
            )

        # === Reversal 觸發 — 直觀化: 從高/低 → 現價 顯示點數差 ===
        for rev in info.get("reversals", []):
            try:
                rtype = rev.get("type", "")
                high = float(rev.get("today_high", today_high or 0) or 0)
                low = float(rev.get("today_low", today_low or 0) or 0)
                cur_r = float(rev.get("current", current or 0) or 0)
                dd = float(rev.get("drawdown_pct", 0) or 0)
                rb = float(rev.get("rebound_pct", 0) or 0)
            except (TypeError, ValueError):
                continue
            if rtype == "drawdown":
                pts_drop = cur_r - high  # 負值
                lines.append(
                    f"  🔻 <b>反轉警報 (從高點回吐)</b>"
                )
                lines.append(
                    f"     高 {high:,.0f} ↘ 現 {cur_r:,.0f} = "
                    f"<b>▼{abs(pts_drop):,.0f} 點 ({dd:+.2f}%)</b>"
                )
            elif rtype == "rebound":
                pts_up = cur_r - low  # 正值
                lines.append(
                    f"  🔺 <b>反彈警報 (從低點彈升)</b>"
                )
                lines.append(
                    f"     低 {low:,.0f} ↗ 現 {cur_r:,.0f} = "
                    f"<b>▲{pts_up:,.0f} 點 (+{rb:.2f}%)</b>"
                )

        # === Bucket 觸發 (原有 index_alerts) ===
        bucket_t = info.get("bucket")
        if bucket_t:
            try:
                diff = float(bucket_t.get("diff", 0) or 0)
                leg = float(bucket_t.get("leg_pts", 0) or 0)
                direction = _esc(bucket_t.get("direction", ""))
                consecutive = int(bucket_t.get("consecutive", 1) or 1)
            except (TypeError, ValueError):
                diff = leg = 0.0
                direction = ""
                consecutive = 1
            arrow_b = "▲" if diff > 0 else "▼"
            extra = f" · 連{consecutive}次同方向{direction}" if consecutive >= 2 else ""
            # 點數警報跟主行的「vs 開盤」其實同樣資料, 但 bucket 強調「跨越門檻」
            lines.append(
                f"  📊 點數警報: {arrow_b}{abs(int(diff)):,} 點跨越門檻{extra}"
            )
            # 移除舊版重複的 leg 顯示 (太重複)
            _ = leg  # silence unused warning; 若需要 leg 邏輯之後可加回

        lines.append("")

    # === Gemini 分析 — 只在 crash 觸發時放, 放在最後 ===
    if has_crash and crash_ai_text:
        lines.append("<b>🤖 Gemini 動作建議</b>")
        lines.append(_md_to_tg_html(crash_ai_text))
        lines.append("")

    lines.append("⚠️ 僅供參考, 不構成投資建議")
    return _truncate_tg_msg("\n".join(lines))


# --- M2 Reversal alert enrichment helpers ---
def _rev_severity_badge(sev: str) -> str:
    return {
        "mild": "🟡 輕微",
        "medium": "🟠 中度",
        "severe": "🔴 嚴重",
    }.get(sev, "")


def _rev_drawdown_state_desc(state: str) -> str:
    return {
        "still_green": "仍在紅 K (高開回吐)",
        "turned_black": "已翻黑 (壓回平盤下)",
        "accelerating_down": "加速殺低 (vs 開盤 ≤ -1%) ⚠️",
    }.get(state, "")


def _rev_rebound_state_desc(state: str) -> str:
    return {
        "recovered": "已收復 (翻紅或平)",
        "near_recover": "接近收復 (vs 開盤 > -1%)",
        "still_red": "仍在黑 K (反彈但未收復)",
    }.get(state, "")


def _rev_speed_desc(speed: str, mins) -> str:
    if speed == "fast":
        try:
            m_int = int(mins or 0)
        except (TypeError, ValueError):
            m_int = 0
        return f"急殺/急彈 ({m_int} 分鐘內) 🚨"
    if speed == "slow":
        try:
            m_int = int(mins or 0)
        except (TypeError, ValueError):
            m_int = 0
        return f"緩跌/緩彈 ({m_int} 分鐘磨)"
    return ""


def _rev_volume_desc(vstate: str, vr) -> str:
    try:
        vrf = float(vr) if vr is not None else None
    except (TypeError, ValueError):
        vrf = None
    if vrf is None:
        return ""
    if vstate == "heavy":
        return f"量增放出 ({vrf:.2f}× 同期) ⚠️"
    if vstate == "light":
        return f"量縮觀望 ({vrf:.2f}× 同期)"
    if vstate == "normal":
        return f"量能正常 ({vrf:.2f}× 同期)"
    return ""


def _rev_action_drawdown(severity: str, market_state: str) -> str:
    # L2 fix: severity × market_state 完整矩陣 (含 mild 也依盤勢區分)
    if severity == "severe":
        if market_state == "accelerating_down":
            return "💡 持倉建議: 加速殺低, 持股減碼 1/3 並設停損"
        return "💡 持倉建議: 跌幅已大, 持股減碼 1/3, 留意停損點"
    if severity == "medium":
        if market_state == "accelerating_down":
            return "💡 持倉建議: 減碼 20%, 觀察是否止穩"
        if market_state == "turned_black":
            return "💡 持倉建議: 暫不加碼, 留意支撐"
        return "💡 持倉建議: 高檔回吐, 觀察是否續弱, 暫不動作"
    # mild
    if market_state == "accelerating_down":
        return "💡 持倉建議: 雖幅度小但已翻黑加速, 留意是否擴大"
    if market_state == "turned_black":
        return "💡 持倉建議: 小幅翻黑, 觀察支撐能否守住"
    return "💡 持倉建議: 短線雜訊, 觀察為主"


def _rev_action_rebound(severity: str, market_state: str) -> str:
    # L2 fix: 補齊 severity × market_state 組合, 避免 mild+recovered 矛盾
    if severity == "severe":
        if market_state == "recovered":
            return "💡 持倉建議: 強勁反彈且已收復, 可分批布局強勢股"
        if market_state == "near_recover":
            return "💡 持倉建議: 反彈強勁接近收復, 留意能否翻紅"
        return "💡 持倉建議: 低檔強彈但仍黑, 先觀察止跌確認"
    if severity == "medium":
        if market_state in ("recovered", "near_recover"):
            return "💡 持倉建議: 初步止穩, 觀察續攻力道再決定"
        return "💡 持倉建議: 反彈中但未收復, 不宜追高"
    # mild
    if market_state == "recovered":
        return "💡 持倉建議: 小幅反彈已翻紅, 觀察延續性"
    return "💡 持倉建議: 短線小反彈, 還沒翻紅前不追"


# M4: 指數 symbol → 短縮寫 (跨市場對照用, 避免多個 US 顯示成 "US / US" 搞混)
_INDEX_SHORT = {
    "^TWII": "台股",
    "^N225": "日經",
    "^KS11": "韓股",
    "^SOX": "費半",
    "^IXIC": "那指",
}


def _index_short(sym: str, country: str = "") -> str:
    """回傳指數縮寫. 找不到就 fallback country code 或 symbol 去掉 ^."""
    if sym in _INDEX_SHORT:
        return _INDEX_SHORT[sym]
    if country:
        return country
    return sym.lstrip("^")


def _fmt_cross_market_short(cross_market: list) -> str:
    """M4: 跨市場對照行 — 用指數縮寫而非 country code."""
    return " / ".join(
        f"{_index_short(c.get('symbol', ''), c.get('country', ''))} "
        f"{c.get('pct_vs_open', 0):+.2f}%"
        for c in cross_market
    )


def _rev_cross_market_ctx(self_pct: float, cross_market: list) -> str:
    if not cross_market:
        return ""
    same_dir = 0
    opp_dir = 0
    for c in cross_market:
        try:
            op = float(c.get("pct_vs_open", 0) or 0)
        except (TypeError, ValueError):
            op = 0.0
        if self_pct < 0:
            if op < -0.3:
                same_dir += 1
            elif op > 0.3:
                opp_dir += 1
        else:
            if op > 0.3:
                same_dir += 1
            elif op < -0.3:
                opp_dir += 1
    if same_dir >= 2:
        return "(系統性走勢)"
    if opp_dir >= 2:
        return "⚠️ 獨自走弱" if self_pct < 0 else "✨ 獨自走強"
    return ""


def fmt_combined_intraday_super(
    crash_data=None,
    reversal_alerts=None,
    bucket_alerts=None,
    weak_open_alerts=None,
    strong_sector_alerts=None,
    holdings_intraday_alerts=None,
    crash_ai_text: str = "",
) -> list:
    """合 monitor 推播 — 反轉 / 開盤即弱 / 強勢族群 / 持倉風險 / 大跌 / bucket.

    H1 fix: 改回傳 list[str], 每元素是 1 封 TG. caller 用迴圈 send.
    這樣即使各段加總超 TG 4096 byte 上限, 也不會 silent 砍掉後面段.
    一般情況 (4 段共 < 3900 byte) 合 1 封; 超量自動拆多封 (各段獨立).
    """
    parts = []
    try:
        m1 = fmt_combined_intraday_alerts(
            crash_data=crash_data,
            reversal_alerts=reversal_alerts or [],
            bucket_alerts=bucket_alerts or [],
            crash_ai_text=crash_ai_text,
        )
        if m1:
            parts.append(m1)
    except Exception as _e:
        print(f"[combined super] crash/reversal/bucket 段失敗: {_e}", flush=True)

    try:
        m2 = fmt_weak_open_alerts(weak_open_alerts or [])
        if m2:
            parts.append(m2)
    except Exception as _e:
        print(f"[combined super] weak_open 段失敗: {_e}", flush=True)

    try:
        m3 = fmt_strong_sector_alerts(strong_sector_alerts or [])
        if m3:
            parts.append(m3)
    except Exception as _e:
        print(f"[combined super] strong_sector 段失敗: {_e}", flush=True)

    # 4 (MED-D1): 持倉 intraday 風險也合進來
    try:
        m4 = fmt_holdings_intraday_alerts(holdings_intraday_alerts or [])
        if m4:
            parts.append(m4)
    except Exception as _e:
        print(f"[combined super] holdings_intraday 段失敗: {_e}", flush=True)

    if not parts:
        return []

    # 嘗試合 1 封 (用分隔線). 若合起來超 TG 上限就拆多封, 避免後面段被截斷.
    sep = "\n\n━━━━━━━━━━━━━━━━━\n\n"
    full = sep.join(parts)
    if len(full.encode("utf-8")) <= 3900:
        return [full]
    # 超量 → 拆多封 (各段獨立, 不損失內容)
    print(f"[combined super] 合計 {len(full.encode('utf-8'))} bytes 超量, 改拆 {len(parts)} 封", flush=True)
    return parts


def fmt_intraday_reversal_alerts(alerts: list) -> str:
    """盤中反轉警報訊息 — 從高點回吐 / 從低點反彈 (M2 加強版).

    每個 alert 含 (見 index_alerts.check_intraday_reversal):
      symbol, name, country, type, current, today_open, today_high, today_low,
      drawdown_pct, rebound_pct, pct_vs_open, alerts_today,
      severity, market_state, speed, mins_since_extreme,
      volume_state, vol_ratio, cross_market, companion_stocks.
    """
    if not alerts:
        return ""

    drawdowns = [a for a in alerts if a.get("type") == "drawdown"]
    rebounds = [a for a in alerts if a.get("type") == "rebound"]
    shrinks = [a for a in alerts if a.get("type") == "shrink"]
    recovers = [a for a in alerts if a.get("type") == "recover"]

    # 取最大強度當訊息標題
    all_sev = [a.get("severity", "mild") for a in alerts]
    order = {"severe": 3, "medium": 2, "mild": 1}
    top_sev = max(all_sev, key=lambda s: order.get(s, 0)) if all_sev else "mild"
    title_badge = _rev_severity_badge(top_sev)
    title_suffix = f" — {title_badge}" if title_badge else ""
    lines = [f"<b>🔄 盤中反轉警報{title_suffix}</b>", ""]

    if drawdowns:
        lines.append("<b>📉 從高點回吐</b>")
        for a in drawdowns:
            name = _esc(a.get("name", ""))
            sym = _esc(_strip_caret(a.get("symbol", "")))
            try:
                dd_pct = float(a.get("drawdown_pct", 0) or 0)
                vs_open = float(a.get("pct_vs_open", 0) or 0)
            except (TypeError, ValueError):
                dd_pct = vs_open = 0.0
            # 新增: vs 昨收 (讓「今日總漲跌」一目了然)
            vs_prior = a.get("pct_vs_prior")
            sev = a.get("severity", "mild")
            badge = _rev_severity_badge(sev)
            badge_p = f" {badge}" if badge else ""
            sign_o = "+" if vs_open > 0 else ""
            advice = _rev_action_drawdown(sev, a.get("market_state", "")) or "💡 觀望"
            # 精簡: 標題 1 行 (含回吐 + vs 開盤 + vs 昨收) + 建議 + 弱股 top 3
            vs_prior_str = ""
            if vs_prior is not None:
                try:
                    vp = float(vs_prior)
                    sign_p = "+" if vp > 0 else ""
                    vs_prior_str = f" · vs 昨收 {sign_p}{vp:.2f}%"
                except (TypeError, ValueError):
                    pass
            lines.append(f"{name} <code>{sym}</code> 回吐 <b>{dd_pct:+.2f}%</b> "
                          f"(vs 開盤 {sign_o}{vs_open:.2f}%{vs_prior_str}){badge_p}")
            lines.append(f"  {advice}")
            comp = a.get("companion_stocks") or []
            if comp:
                wk_line = " · ".join(
                    f"{_esc(c.get('stock_id',''))} "
                    f"{c.get('entry_emoji','')}"
                    f"{float(c.get('today_pct',0) or 0):+.2f}%"
                    for c in comp[:3]
                )
                lines.append(f"  同步弱 (當日%): {wk_line}")
                # 顯示「該減碼」(entry_label=AVOID 或 SELL) 的優先
                avoid_list = [c for c in comp if c.get("entry_label") in ("AVOID", "SELL")][:3]
                if avoid_list:
                    av_line = " · ".join(
                        f"{_esc(c.get('stock_id',''))} {float(c.get('today_pct',0) or 0):+.2f}%"
                        for c in avoid_list
                    )
                    lines.append(f"  ⚠️ <b>建議減碼</b>: {av_line}")
            lines.append("")

    # 漲幅萎縮 (瘦身: 1 行重點 + 同步弱股 top 3)
    if shrinks:
        lines.append("<b>📉 漲幅萎縮</b>")
        for a in shrinks:
            sym = _esc(a.get("symbol", ""))
            name = _esc(a.get("name", ""))
            try:
                max_pct = float(a.get("max_pct_vs_open", 0) or 0)
                vs_open = float(a.get("pct_vs_open", 0) or 0)
                shrink_pp = float(a.get("shrink_pp", 0) or 0)
            except (TypeError, ValueError):
                max_pct = vs_open = shrink_pp = 0.0
            sign_o = "+" if vs_open > 0 else ""
            vs_prior_s = a.get("pct_vs_prior")
            vs_prior_str_s = ""
            if vs_prior_s is not None:
                try:
                    vp = float(vs_prior_s)
                    sign_p = "+" if vp > 0 else ""
                    vs_prior_str_s = f" · vs 昨收 {sign_p}{vp:.2f}%"
                except (TypeError, ValueError):
                    pass
            lines.append(
                f"{name} <code>{sym}</code> 高 +{max_pct:.2f}% → {sign_o}{vs_open:.2f}%"
                f"{vs_prior_str_s} "
                f"(<b>-{shrink_pp:.2f}pp</b>) 💡 分批停利"
            )
            companion = a.get("companion_stocks") or []
            if companion:
                wk_line = " · ".join(
                    f"{_esc(cs.get('stock_id',''))} "
                    f"{cs.get('entry_emoji','')}"
                    f"{float(cs.get('today_pct',0) or 0):+.2f}%"
                    for cs in companion[:3]
                )
                lines.append(f"  同步弱 (當日%): {wk_line}")
            lines.append("")

    # #1 新增: recover (從跌轉漲) + #2 同步轉強權值 + #3 強勢族群
    if recovers:
        lines.append("<b>📈 從跌轉漲 (低點反彈)</b>")
        for a in recovers:
            sym = _esc(a.get("symbol", ""))
            name = _esc(a.get("name", ""))
            try:
                min_pct = float(a.get("min_pct_vs_open", 0) or 0)
                vs_open = float(a.get("pct_vs_open", 0) or 0)
                recovery_pp = float(a.get("recovery_pp", 0) or 0)
            except (TypeError, ValueError):
                min_pct = vs_open = recovery_pp = 0.0
            sign_o = "+" if vs_open > 0 else ""
            vs_prior_rv = a.get("pct_vs_prior")
            vs_prior_str_rv = ""
            if vs_prior_rv is not None:
                try:
                    vp = float(vs_prior_rv)
                    sign_p = "+" if vp > 0 else ""
                    vs_prior_str_rv = f" · vs 昨收 {sign_p}{vp:.2f}%"
                except (TypeError, ValueError):
                    pass
            lines.append(
                f"{name} <code>{sym}</code> 低 {min_pct:.2f}% → {sign_o}{vs_open:.2f}%"
                f"{vs_prior_str_rv} "
                f"(<b>+{recovery_pp:.2f}pp</b>) 💡 留意轉強跟進"
            )
            up_stocks = a.get("companion_stocks_up") or []
            if up_stocks:
                # H: 假反彈過濾 — 漲 ≥3% 但量比 < 0.8x 加警示
                try:
                    import fake_rally_detector as _frd
                    up_stocks = _frd.flag_fake_rally(up_stocks)
                except Exception:
                    pass
                # 加 entry_label emoji (🟢BUY / 🟡WAIT / 🔴AVOID) + 假反彈警示
                def _stock_render(cs):
                    sid = _esc(cs.get('stock_id',''))
                    emoji = cs.get('entry_emoji','')
                    pct = float(cs.get('today_pct',0) or 0)
                    # 假反彈 → 加 ⚠
                    warn = ""
                    if cs.get("rally_quality") == "fake_rally":
                        warn = "⚠"
                    elif cs.get("rally_quality") == "weak":
                        warn = "🟡"
                    return f"{sid} {emoji}{pct:+.2f}%{warn}"
                st_line = " · ".join(_stock_render(cs) for cs in up_stocks[:3])
                lines.append(f"  同步強 (當日%): {st_line}")
                # B: 明確「現在能買」區塊
                # 主路徑: entry_label="BUY"; fallback (Gemini fail 時): entry_score >= 70
                def _is_buy(cs):
                    if cs.get("entry_label") == "BUY":
                        return True
                    score = cs.get("entry_score")
                    try:
                        return score is not None and float(score) >= 70
                    except (TypeError, ValueError):
                        return False
                buy_list = [cs for cs in up_stocks if _is_buy(cs)][:3]
                if buy_list:
                    buy_line = " · ".join(
                        f"{_esc(cs.get('stock_id',''))} "
                        f"{float(cs.get('today_pct',0) or 0):+.2f}% "
                        f"(score {cs.get('entry_score','—')})"
                        for cs in buy_list
                    )
                    lines.append(f"  📊 <b>現在能買</b>: {buy_line}")
            sl = a.get("sector_leaders") or []
            if sl:
                sec_line = " · ".join(
                    f"{_esc(s.get('sector',''))} +{float(s.get('avg_change',0) or 0):.2f}%"
                    for s in sl[:3]
                )
                lines.append(f"  強勢族群: {sec_line}")
            lines.append("")

    if rebounds:
        lines.append("<b>📈 從低點反彈</b>")
        for a in rebounds:
            name = _esc(a.get("name", ""))
            sym = _esc(_strip_caret(a.get("symbol", "")))
            try:
                rb_pct = float(a.get("rebound_pct", 0) or 0)
                vs_open = float(a.get("pct_vs_open", 0) or 0)
            except (TypeError, ValueError):
                rb_pct = vs_open = 0.0
            vs_prior_r = a.get("pct_vs_prior")
            sev = a.get("severity", "mild")
            badge = _rev_severity_badge(sev)
            badge_p = f" {badge}" if badge else ""
            sign_o = "+" if vs_open > 0 else ""
            advice = _rev_action_rebound(sev, a.get("market_state", "")) or "💡 觀察跟進"
            vs_prior_str_r = ""
            if vs_prior_r is not None:
                try:
                    vp = float(vs_prior_r)
                    sign_p = "+" if vp > 0 else ""
                    vs_prior_str_r = f" · vs 昨收 {sign_p}{vp:.2f}%"
                except (TypeError, ValueError):
                    pass
            lines.append(f"{name} <code>{sym}</code> 反彈 <b>+{rb_pct:.2f}%</b> "
                          f"(vs 開盤 {sign_o}{vs_open:.2f}%{vs_prior_str_r}){badge_p}")
            lines.append(f"  {advice}")
            lines.append("")

    lines.append("<i>※ 警報為動能訊號, 非進出建議. 請自行控管風險.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_weak_open_alerts(alerts: list) -> str:
    """開盤即弱 / 開盤即強警報訊息 (M3 新).

    alerts: index_alerts.check_weak_open_alerts() 回傳.
    每個 dict 含 symbol, name, country, type ("weak_open"/"strong_open"),
              current, today_open, today_high, today_low, pct_vs_open,
              high_from_open_pct, low_from_open_pct, severity, vol_ratio,
              volume_state, cross_market.
    """
    if not alerts:
        return ""

    weak = [a for a in alerts if a.get("type") == "weak_open"]
    strong = [a for a in alerts if a.get("type") == "strong_open"]

    all_sev = [a.get("severity", "mild") for a in alerts]
    order = {"severe": 3, "medium": 2, "mild": 1}
    top_sev = max(all_sev, key=lambda s: order.get(s, 0)) if all_sev else "mild"
    title_badge = _rev_severity_badge(top_sev)
    title_suffix = f" — {title_badge}" if title_badge else ""
    lines = [f"<b>⚡ 開盤型態警報{title_suffix}</b>", ""]

    if weak:
        lines.append("<b>📉 開盤即弱 (沒給過反彈機會)</b>")
        for a in weak:
            country = _esc(a.get("country", ""))
            name = _esc(a.get("name", ""))
            sym = _esc(a.get("symbol", ""))
            try:
                cur = float(a.get("current", 0) or 0)
                op = float(a.get("today_open", 0) or 0)
                vs_open = float(a.get("pct_vs_open", 0) or 0)
                hi_from_op = float(a.get("high_from_open_pct", 0) or 0)
            except (TypeError, ValueError):
                cur = op = vs_open = hi_from_op = 0.0
            sev = a.get("severity", "mild")
            badge = _rev_severity_badge(sev)
            badge_part = f"  {badge}" if badge else ""
            lines.append(f"[{country}] {name} <code>{sym}</code> {cur:,.2f}{badge_part}")
            lines.append(
                f"  開盤 {op:,.2f} → 現價 <b>{vs_open:+.2f}%</b> "
                f"(盤中最高僅 +{hi_from_op:.2f}%)"
            )
            vol_desc = _rev_volume_desc(a.get("volume_state", "unknown"), a.get("vol_ratio"))
            # 量能行已隱藏 (用戶選 簡化推播)
            _ = vol_desc
            # 跨市場
            cm = a.get("cross_market") or []
            if cm:
                ctx = _rev_cross_market_ctx(vs_open, cm)
                cm_short = _fmt_cross_market_short(cm)
                ctx_part = f"  {ctx}" if ctx else ""
                lines.append(f"  📊 對照: {cm_short}{ctx_part}")
            # 操作建議
            if sev == "severe":
                lines.append("  💡 持倉建議: 弱開且未反彈, 持股減碼觀望")
            elif sev == "medium":
                lines.append("  💡 持倉建議: 短線轉弱訊號, 暫不加碼")
            else:
                lines.append("  💡 持倉建議: 開盤偏弱, 觀察是否止穩")
            # SOX/IXIC 特別提醒對台股影響
            if sym in ("^SOX", "^IXIC"):
                lines.append("  ⚠️ 此為台股 leading indicator, 留意明日台股開盤")
            lines.append("")

    if strong:
        lines.append("<b>📈 開盤即強 (沒給過回測機會)</b>")
        for a in strong:
            country = _esc(a.get("country", ""))
            name = _esc(a.get("name", ""))
            sym = _esc(a.get("symbol", ""))
            try:
                cur = float(a.get("current", 0) or 0)
                op = float(a.get("today_open", 0) or 0)
                vs_open = float(a.get("pct_vs_open", 0) or 0)
                lo_from_op = float(a.get("low_from_open_pct", 0) or 0)
            except (TypeError, ValueError):
                cur = op = vs_open = lo_from_op = 0.0
            sev = a.get("severity", "mild")
            badge = _rev_severity_badge(sev)
            badge_part = f"  {badge}" if badge else ""
            lines.append(f"[{country}] {name} <code>{sym}</code> {cur:,.2f}{badge_part}")
            lines.append(
                f"  開盤 {op:,.2f} → 現價 <b>+{vs_open:.2f}%</b> "
                f"(盤中最低僅 {lo_from_op:.2f}%)"
            )
            vol_desc = _rev_volume_desc(a.get("volume_state", "unknown"), a.get("vol_ratio"))
            # 量能行已隱藏 (用戶選 簡化推播)
            _ = vol_desc
            cm = a.get("cross_market") or []
            if cm:
                ctx = _rev_cross_market_ctx(vs_open, cm)
                cm_short = _fmt_cross_market_short(cm)
                ctx_part = f"  {ctx}" if ctx else ""
                lines.append(f"  📊 對照: {cm_short}{ctx_part}")
            if sev == "severe":
                lines.append("  💡 持倉建議: 強勢開盤未回測, 可順勢留意強勢族群")
            elif sev == "medium":
                lines.append("  💡 持倉建議: 多頭續攻訊號, 留意拉回是否買點")
            else:
                lines.append("  💡 持倉建議: 開盤偏強, 觀察延續性")
            if sym in ("^SOX", "^IXIC"):
                lines.append("  ✨ 此為台股 leading indicator, 明日台股可期")
            lines.append("")

    lines.append("<i>※ 警報為動能訊號, 非進出建議. 請自行控管風險.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_news_event_alerts(alerts: list, impact_analysis: str = "") -> str:
    """事件型新聞推播 (news_event_alert.check_news_events 用).

    alerts: list of {symbol, market, tag, title, link, publisher, keywords_hit, urgency}
    impact_analysis: 可選, news_impact_analyzer.analyze_news_impact() 回傳的 HTML 區塊
                     (對 HIGH urgency 事件的 Gemini 分析). 直接接在事件列表後.
    """
    if not alerts:
        return ""
    # 按 urgency 拆 3 組 (HIGH 急報 / MED 注意 / LOW 普通)
    high_alerts = [a for a in alerts if a.get("urgency") == "HIGH"]
    med_alerts  = [a for a in alerts if a.get("urgency") == "MED"]
    low_alerts  = [a for a in alerts if a.get("urgency") not in ("HIGH", "MED")]

    # 訊息 header 依最高 urgency 決定
    if high_alerts:
        header = f"🚨🚨 <b>急報 — {len(alerts)} 則重大事件</b>"
    elif med_alerts:
        header = f"⚠️ <b>注意 — {len(alerts)} 則新聞事件</b>"
    else:
        header = f"📰 <b>新聞事件 ({len(alerts)} 則)</b>"

    lines = [header, ""]
    tag_emoji = {"hold": "💼", "watch": "👀", "main": "📌"}
    tag_text = {"hold": "持倉", "watch": "觀察", "main": "主流"}
    source_label = {
        "8-K": "⚡SEC 8-K", "8-K/A": "⚡SEC 8-K/A",
        "10-Q": "📊SEC 10-Q", "10-K": "📚SEC 10-K", "S-1": "🆕SEC S-1",
        "PR": "📢公司公告", "TW_NEWS": "🇹🇼重大訊息", "YAHOO": "📰Yahoo",
    }

    # 按 urgency 區塊輸出 (急報/注意/普通)
    section_alerts = [
        ("🚨 急報", high_alerts),
        ("⚠️ 注意", med_alerts),
        ("📰 一般", low_alerts),
    ]
    for sect_title, sect_list in section_alerts:
        if not sect_list:
            continue
        lines.append(f"<b>{sect_title}</b>")
        for a in sect_list:
            sym = _esc(a.get("symbol", ""))
            market = _esc(a.get("market", ""))
            tag = a.get("tag", "main")
            t_emoji = tag_emoji.get(tag, "📌")
            t_text = tag_text.get(tag, "")
            src = a.get("source_type", "YAHOO")
            src_text = source_label.get(src, src)
            title = _esc(a.get("title", ""))
            link = a.get("link", "") or ""
            publisher = _esc(a.get("publisher", ""))
            hits = a.get("keywords_hit") or []
            # 8K-AUTO 不顯示為「關鍵字」, 顯示為「自動觸發」
            if hits == ["8K-AUTO"]:
                hits_display = "<b>SEC 重大事件自動推</b>"
            else:
                hits_display = "關鍵字: <b>" + ", ".join(_esc(k) for k in hits[:5]) + "</b>"
            lines.append(
                f"{t_emoji} [{market}] <code>{sym}</code>  <i>{t_text}</i>  "
                f"<i>{src_text}</i>  {hits_display}"
            )
            if link:
                lines.append(f'  <a href="{_esc_attr(link)}">{_esc(title)}</a>')
            else:
                lines.append(f"  {_esc(title)}")
            lines.append("")
    # 接 Gemini 影響分析 (若有)
    if impact_analysis:
        lines.append(impact_analysis)
    lines.append("<i>※ 新聞訊號, 請自行確認真偽.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_holdings_intraday_alerts(alerts: list) -> str:
    """持倉 intraday 風險警報訊息 (4 新).

    alerts: holdings_intraday_alert.check_holdings_intraday_risk() 回傳.
    每個 dict 含: stock_id, name, market, current, today_pct, today_high,
                  drawdown_from_high_pct, stop_price, triggers, severity.
    """
    if not alerts:
        return ""
    # 整體 severity 取最強
    top_sev = "medium"
    if any(a.get("severity") == "severe" for a in alerts):
        top_sev = "severe"
    badge = "🔴 嚴重" if top_sev == "severe" else "🟠 中度"

    lines = [f"<b>⚠️ 持倉 intraday 風險警報 — {badge}</b>", ""]
    for a in alerts:
        sid = _esc(a.get("stock_id", ""))
        name = _esc(a.get("name", ""))
        market = _esc(a.get("market", ""))
        try:
            cur = float(a.get("current", 0) or 0)
            tp = float(a.get("today_pct", 0) or 0)
            dd = float(a.get("drawdown_from_high_pct", 0) or 0)
        except (TypeError, ValueError):
            cur = tp = dd = 0.0
        sev_emoji = "🔴" if a.get("severity") == "severe" else "🟠"
        # 入場標籤一起顯示
        el_emoji = a.get("entry_emoji", "")
        el_label = a.get("entry_label", "")
        el_tag = f"  {el_emoji} {_esc(el_label)}" if el_label and el_label != "—" else ""
        market_tag = f"[{market}] " if market else ""   # Bug fix: 空 market 不顯示 []
        lines.append(
            f"{sev_emoji} {market_tag}<code>{sid}</code> {name} {cur:,.2f}  "
            f"<b>{tp:+.2f}%</b>{el_tag}"
        )
        # 觸發理由 (來自 check 模組)
        for t in (a.get("triggers") or [])[:3]:
            lines.append(f"  • {_esc(t)}")
        # 操作建議 (優先用 entry_action, fallback 用 severity 預設)
        entry_action = a.get("entry_action")
        if entry_action and entry_action not in ("—", "持平觀望"):
            lines.append(
                f"  💡 持倉建議: {_esc(entry_action)} "
                f"(系統評分 {a.get('entry_score', '—')}/100)"
            )
        elif a.get("severity") == "severe":
            lines.append("  💡 持倉建議: 嚴重訊號, 建議立即減碼 1/2 或設緊停損")
        else:
            lines.append("  💡 持倉建議: 留意是否止穩, 考慮減碼 1/3")
        lines.append("")

    lines.append("<i>※ 持倉警報, 請依個人風控規劃調整部位.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_strong_sector_alerts(alerts: list) -> str:
    """盤中強勢族群推播 (strong_sector_alert.check_strong_sectors_intraday 用).

    alerts: list of {sector_type, sector_name, avg_pct, up_ratio, n_stocks, leaders, severity}
    """
    if not alerts:
        return ""

    # 整體 severity 取最強
    top_sev = "medium"
    if any(a.get("severity") == "strong" for a in alerts):
        top_sev = "strong"
    badge = "🔴 強勢" if top_sev == "strong" else "🟠 中度"

    lines = [f"<b>🚀 盤中強勢族群 — {badge}</b>", ""]

    # 分組顯示: 證交所產業 / 熱門題材
    by_type = {"industry": [], "theme": []}
    for a in alerts:
        by_type.setdefault(a.get("sector_type", "industry"), []).append(a)

    section_titles = {
        "industry": "📊 證交所產業 (資金流入)",
        "theme": "🔥 熱門題材 (動能領先)",
    }

    for sec_type, title in section_titles.items():
        items = by_type.get(sec_type) or []
        if not items:
            continue
        lines.append(f"<b>{title}</b>")
        for a in items:
            name = _esc(a.get("sector_name", ""))
            try:
                avg = float(a.get("avg_pct", 0) or 0)
                up_ratio = float(a.get("up_ratio", 0) or 0)
                n = int(a.get("n_stocks", 0) or 0)
            except (TypeError, ValueError):
                avg = up_ratio = 0.0
                n = 0
            # MED fix: up_ratio 預期 0-1 scale. 若 caller 誤傳 0-100, 自動 normalize.
            if up_ratio > 1.0:
                up_ratio = up_ratio / 100.0
            sev_badge = "🔴" if a.get("severity") == "strong" else "🟠"
            # 主行
            lines.append(
                f"{sev_badge} <b>{name}</b>  "
                f"均漲 <b>+{avg:.2f}%</b>  "
                f"上漲 {up_ratio * 100:.0f}% ({int(up_ratio * n)}/{n})"
            )
            # 龍頭 3 檔 — #3 升級: 加龍頭/跟風/候補分類
            leaders = a.get("leaders") or []
            if leaders:
                # 分類
                try:
                    import sector_role_classifier as _src
                    leaders = _src.classify_stocks_in_sector(leaders, avg)
                except Exception:
                    pass
                # 分組顯示
                leader_list = [ld for ld in leaders if ld.get("sector_role") == "leader"][:2]
                laggard_list = [ld for ld in leaders if ld.get("sector_role") == "laggard"][:2]
                other_list = [ld for ld in leaders if ld.get("sector_role") not in ("leader", "laggard")][:2]

                def _fmt_one(ld):
                    sid = _esc(ld.get("stock_id", ""))
                    lname = _esc(ld.get("name", ""))
                    try:
                        tp = float(ld.get("today_pct", 0) or 0)
                        vr = float(ld.get("vol_ratio", 0) or 0)
                    except (TypeError, ValueError):
                        tp = vr = 0.0
                    el_emoji = ld.get("entry_emoji", "")
                    el_label = ld.get("entry_label", "")
                    el_tag = f" {el_emoji}{_esc(el_label)}" if el_label and el_label != "—" else ""
                    return f"<code>{sid}</code> {lname} <b>{tp:+.2f}%</b>{el_tag}"

                if leader_list:
                    lines.append("  🏆 龍頭 (該追): " + " / ".join(_fmt_one(ld) for ld in leader_list))
                if laggard_list:
                    lines.append("  🕵️ 候補 (吸籌中, 明日可能補漲): " + " / ".join(_fmt_one(ld) for ld in laggard_list))
                if other_list and not leader_list and not laggard_list:
                    # 沒分類出龍頭 / 候補時, 顯示一般股
                    parts = [_fmt_one(ld) for ld in other_list]
                    lines.append("  族群股: " + " / ".join(parts))
            # 操作建議
            if a.get("severity") == "strong":
                lines.append("  💡 持倉建議: 多檔齊漲且量增, 留意龍頭股拉回買點")
            else:
                lines.append("  💡 持倉建議: 族群轉強, 觀察龍頭續攻力道")
        lines.append("")

    lines.append("<i>※ 資金流向動態訊號, 非進出建議. 請自行控管風險.</i>")
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
            cur = float(t.get("current", 0) or 0)
            today_open = float(t.get("today_open", 0) or 0)
            prior_close = float(t.get("prior_close", 0) or 0)
            pct_vs_open = float(t.get("pct_vs_open", 0) or 0)
            pct_vs_prior = float(t.get("pct_vs_prior", 0) or 0)
        except (TypeError, ValueError):
            tval = cur = today_open = prior_close = pct_vs_open = pct_vs_prior = 0.0
        # SOX fix: 同時顯示 vs 開盤 + vs 昨收 兩個 %, 避免跟新聞報的數字對不上
        ttype_zh = "盤中" if ttype == "intraday" else "連2日累計"
        lines.append(
            f"• {name} <code>{sym}</code> {cur:,.2f} "
            f"({ttype_zh}觸發 <b>{tval:+.2f}%</b>)"
        )
        lines.append(
            f"  vs 開盤 {today_open:,.2f}: <b>{pct_vs_open:+.2f}%</b> · "
            f"vs 昨收 {prior_close:,.2f}: <b>{pct_vs_prior:+.2f}%</b>"
        )
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
            + (f", VIX {vix_f:.2f}" if vix_f is not None else "")
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
        # (量比已移除; 原本殘留的孤兒 `tech_pass` 會 NameError, 已刪)
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
            chip_parts.append(f"融資30日 {m30:+.2f}%")
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
    events = data.get("events", {})
    if sector_picks:
        lines.append("")
        lines.append("<b>各板塊動能潛在股 (3 檔)</b>")
        for sp in sector_picks:
            # 防呆: 用 .get() 避免 schema 變動 KeyError
            sec = sp.get("sector", "Unknown")
            stocks = sp.get("stocks")
            if stocks is None or (hasattr(stocks, "empty") and stocks.empty):
                continue
            lines.append(f"\n<b>[{_esc(sec)}]</b>")
            for _, s in stocks.iterrows():
                sym = s.get("symbol", "")
                today = s.get("今日%")
                twenty = s.get("20日%")
                lines.append(
                    f"  • <b><code>{_esc(sym)}</code></b>  "
                    f"今日 {_esc(today)}% · 20d {_esc(twenty)}%"
                )
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
            score = s.get("growth_score")
            lines.append(
                f"  • <b><code>{_esc(sym)}</code></b>  "
                f"今日 {_esc(today)}% · 20d {_esc(twenty)}% · {_esc(score)}/10"
            )
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
        lines.append(f"   今日 {_esc(today)}% / 5d {_esc(five)}% ")
        cat = row.get("催化劑")
        if cat and str(cat).strip() and str(cat).strip() != "—":
            pass  # 催化劑已移除
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
            pass  # 量比已移除
        inst_parts = []
        if row.get("投信今日(張)") is not None:
            v = row.get("投信今日(張)")
            sign = "+" if isinstance(v, (int, float)) and v > 0 else ""
            inst_parts.append(f"投信今日 {sign}{_fmt_num(v)}張")
        if row.get("投信5日(張)") is not None:
            v = row.get("投信5日(張)")
            sign = "+" if isinstance(v, (int, float)) and v > 0 else ""
            inst_parts.append(f"5日累計 {sign}{_fmt_num(v)}張")
            inst_parts.append(f"投本比 {_fmt_num(row.get('投本比%'), suffix='%')}")
        if inst_parts:
            lines.append("  💰 " + " · ".join(inst_parts))
        lines.append("")
    return "\n".join(lines)


def fmt_us_fg_alert(fg: dict, threshold_low: int = 25, threshold_high: int = 75) -> Optional[str]:
    """美股 F&G 極值警報.

    Bug fix: 加 fmt_fear_greed_alert alias 給 app.py 用 (line 3111 之前的呼叫).
    """
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



# Alias: app.py 用 fmt_fear_greed_alert
fmt_fear_greed_alert = fmt_us_fg_alert

def fmt_tw_pulse_alert(pulse: dict, threshold_low: int = 25, threshold_high: int = 75):
    """台股市場情緒指數極值警報."""
    if not pulse or pulse.get("score") is None:
        return None
    try:
        s = float(pulse["score"])
    except (TypeError, ValueError):
        return None
    rating = _esc(pulse.get("rating", ""))
    if s <= threshold_low:
        return (f"⚠️ <b>台股市場極度恐慌</b>\nTW Pulse: <b>{s:.0f}</b> ({rating})")
    if s >= threshold_high:
        return (f"⚠️ <b>台股市場極度貪婪</b>\nTW Pulse: <b>{s:.0f}</b> ({rating})")
    return None


def fmt_volume_breakout_alerts(alerts: list) -> str:
    """量爆突破推播 (Tier 1 — 立即響鈴).
    alerts: volume_breakout_alert.check_volume_breakout() 回傳.
    """
    if not alerts:
        return ""
    lines = [f"🚨 <b>量爆突破 ({len(alerts)} 則)</b>", ""]
    for a in alerts:
        sym = _esc(a.get("symbol", ""))
        market = _esc(a.get("market", ""))
        cur = float(a.get("current", 0) or 0)
        tp = float(a.get("today_pct", 0) or 0)
        vr = float(a.get("vol_ratio", 0) or 0)
        bp = float(a.get("breakout_pct", 0) or 0)
        h60 = float(a.get("high_60d", 0) or 0)
        market_tag = f"[{market}] " if market else ""   # Bug fix: 空 market 不顯示 []
        lines.append(
            f"⚡ {market_tag}<code>{sym}</code> {cur:,.2f} <b>{tp:+.2f}%</b>"
        )
        lines.append(
            f"  量比 <b>{vr:.2f}x</b> · 突破 60d 高 ({h60:,.2f}) +{bp:.2f}%"
        )
        lines.append("  💡 主力進場確定訊號, 留意拉回支撐 (60d high)")
        lines.append("")
    lines.append("<i>※ Tier 1 — 多空轉強最確定訊號. 持倉加碼 / 觀望者分批進.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_chip_anomaly_alerts(alerts: list) -> str:
    """籌碼異常推播 (Tier 2 — 響鈴批次).
    alerts: chip_anomaly_alert.check_chip_anomaly() 回傳.
    """
    if not alerts:
        return ""
    buys = [a for a in alerts if a.get("direction") == "buy"]
    sells = [a for a in alerts if a.get("direction") == "sell"]
    lines = [f"📊 <b>籌碼異常 ({len(alerts)} 則)</b>", ""]
    if buys:
        lines.append("<b>🟢 法人連買 (進場訊號)</b>")
        for a in buys:
            sym = _esc(a.get("symbol", ""))
            sd = a.get("streak_days", 0)
            pct = float(a.get("pct_of_outstanding", 0) or 0)
            cum = a.get("cum_5d_lots", 0)
            lines.append(
                f"  ⚡ <code>{sym}</code> 連買 {sd} 日 · "
                f"累積 {cum:+,} 張 ({pct:.2f}% of 流通)"
            )
        lines.append("")
    if sells:
        lines.append("<b>🔴 法人連賣 (警示)</b>")
        for a in sells:
            sym = _esc(a.get("symbol", ""))
            pct = float(a.get("pct_of_outstanding", 0) or 0)
            cum = a.get("cum_5d_lots", 0)
            lines.append(
                f"  ⚠ <code>{sym}</code> 連賣 {sd} 日 · "
                f"累積 {cum:+,} 張 ({pct:.2f}% of 流通)"
            )
        lines.append("")
    lines.append("<i>※ Tier 2 — 大戶動向先行指標. 持倉者參考調整.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_breakout_consolidation_alerts(alerts: list) -> str:
    """美股盤整突破推播 (Tier 1 — 立即響鈴).
    alerts: breakout_consolidation_alert.check_breakout_consolidation() 回傳.
    """
    if not alerts:
        return ""
    lines = [f"🚀 <b>盤整突破 — 開盤跳出 ({len(alerts)} 支)</b>", ""]
    for a in alerts:
        sym = _esc(a.get("symbol", ""))
        cur = float(a.get("current", 0) or 0)
        tp = float(a.get("today_pct", 0) or 0)
        vr = float(a.get("vol_ratio", 0) or 0)
        bp = float(a.get("breakout_pct", 0) or 0)
        h20 = float(a.get("high_20d", 0) or 0)
        atr = float(a.get("atr_pct_20d", 0) or 0)
        rg = float(a.get("range_pct_20d", 0) or 0)
        theme = _esc(a.get("theme_tag", ""))
        theme_part = f" 🏷 {theme}" if theme else ""
        lines.append(
            f"⚡ <code>{sym}</code> {cur:,.2f} <b>{tp:+.2f}%</b>{theme_part}"
        )
        lines.append(
            f"  20d 盤整 (range {rg:.2f}% / ATR {atr:.2f}%) → 突破 20d 高 {h20:,.2f} (+{bp:.2f}%)"
        )
        lines.append(f"  量比 <b>{vr:.2f}x</b> · 💡 突破型態, 留意拉回 20d 高支撐")
        lines.append("")
    lines.append("<i>※ Tier 1 — 盤整突破最強訊號. 進場分批, 跌破 20d 高停損.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_asia_leading_alerts(alerts: list) -> str:
    """日韓 leading alert 訊息 (Tier 2)."""
    if not alerts:
        return ""
    has_down = any(a.get("direction") == "down" for a in alerts)
    header_emoji = "📉" if has_down else "📈"
    lines = [f"{header_emoji} <b>亞股先行 — 台股 leading 訊號 ({len(alerts)})</b>", ""]
    for a in alerts:
        name = _esc(a.get("name", ""))
        sym = _esc(a.get("symbol", ""))
        cur = float(a.get("current", 0) or 0)
        pct = float(a.get("pct_vs_open", 0) or 0)
        dir_emoji = "⬇️" if a.get("direction") == "down" else "⬆️"
        lines.append(
            f"{dir_emoji} {name} <code>{sym}</code> {cur:,.2f} "
            f"<b>{pct:+.2f}%</b> (vs 開盤)"
        )
        twii = a.get("twii_pct_vs_open")
        if twii is not None:
            lines.append(f"  🇹🇼 台股加權: {twii:+.2f}% (vs 開盤)")
        narr = a.get("narrative", "")
        if narr:
            lines.append(f"  💡 {narr}")
        lines.append("")
    lines.append("<i>※ Tier 2 — 亞股先行指標, 台股有機會跟動.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_analyst_insider_alerts(alerts: list, gemini_analysis: str = "") -> str:
    """分析師升評 + 內部人買進 推播 (Tier 1)."""
    if not alerts:
        return ""
    upgrades = [a for a in alerts if a.get("type") == "analyst_upgrade"]
    insider_buys = [a for a in alerts if a.get("type") == "insider_buy"]
    ceo_buys = [a for a in insider_buys if a.get("is_ceo_cfo")]
    other_buys = [a for a in insider_buys if not a.get("is_ceo_cfo")]

    lines = [f"🏛 <b>機構/內部人動向 ({len(alerts)} 則)</b>", ""]
    if ceo_buys:
        lines.append("━━━━━━━ 🚨 CEO/CFO 買進 ━━━━━━━")
        for a in ceo_buys[:5]:
            sym = _esc(a.get("symbol", ""))
            who = _esc(a.get("position") or a.get("name", ""))
            val = a.get("value", 0)
            shares = a.get("shares", 0)
            price = float(a.get("price", 0) or 0)
            d = _esc(a.get("filing_date", ""))
            lines.append(
                f"💼 <code>{sym}</code> {who}: "
                f"<b>${val:,}</b> ({shares:,} 股 @ ${price:.2f}) [{d}]"
            )
        lines.append("")
    if other_buys:
        lines.append("━━━━━━━ 💰 內部人大額買進 ━━━━━━━")
        for a in other_buys[:5]:
            sym = _esc(a.get("symbol", ""))
            who = _esc(a.get("position") or a.get("name", ""))
            val = a.get("value", 0)
            lines.append(f"  ⚡ <code>{sym}</code> {who}: <b>${val:,}</b>")
        lines.append("")
    if upgrades:
        lines.append("━━━━━━━ 📈 分析師升評 wave ━━━━━━━")
        for a in upgrades[:5]:
            sym = _esc(a.get("symbol", ""))
            cur = a.get("buy_ratio_cur", 0)
            prev = a.get("buy_ratio_prev", 0)
            ch = a.get("buy_ratio_change_pp", 0)
            lines.append(
                f"  📊 <code>{sym}</code> BUY 占比: {prev}% → <b>{cur}%</b> "
                f"(+{ch:.2f} pp)"
            )
        lines.append("")
    if gemini_analysis:
        lines.append("━━━━━━━ 🤖 Gemini 分析 ━━━━━━━")
        lines.append(_esc(gemini_analysis))
        lines.append("")
    lines.append("<i>※ Tier 1 — 內部人 (CEO/CFO) 買進是 strongest 內幕訊號.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_rate_cycle_advice(cycle_info: dict, advice: dict) -> str:
    """利率週期 + 族群建議推播."""
    if not advice:
        return ""
    emoji = advice.get("emoji", "⚪")
    label = _esc(advice.get("label", ""))
    regime = _esc(advice.get("regime", ""))
    evidence = _esc(cycle_info.get("evidence", ""))
    lines = [
        f"{emoji} <b>Fed Cycle 提示: {label} ({regime})</b>",
        f"<i>{evidence}</i>",
        "",
    ]
    out = advice.get("outperform") or []
    if out:
        lines.append("<b>📈 美股看好 (受惠類)</b>")
        for item in out[:5]:
            if len(item) >= 3:
                tic, name, reason = item[0], item[1], item[2]
                lines.append(f"  ✅ <code>{_esc(tic)}</code> {_esc(name)} — {_esc(reason)}")
            elif len(item) >= 2:
                lines.append(f"  ✅ <code>{_esc(item[0])}</code> {_esc(item[1])}")
        lines.append("")
    av = advice.get("avoid") or []
    if av:
        # 簡化: avoid 用單行帶過, 不展開
        av_names = " · ".join(_esc(item[0]) if len(item) >= 1 else "" for item in av[:3])
        lines.append(f"📉 美股避開: {av_names}")
        lines.append("")
    tw_out = advice.get("tw_outperform") or []
    if tw_out:
        lines.append("<b>🇹🇼 台股看好</b>")
        for item in tw_out[:3]:
            if len(item) >= 2:
                lines.append(f"  ✅ <code>{_esc(item[0])}</code> {_esc(item[1])}")
        lines.append("")
    tw_av = advice.get("tw_avoid") or []
    if tw_av:
        tw_av_names = " · ".join(_esc(item[0]) if len(item) >= 1 else "" for item in tw_av[:3])
        lines.append(f"📉 台股避開: {tw_av_names}")
        lines.append("")
    # Gemini 解讀看好族群 — 由 caller 注入
    gem = advice.get("gemini_analysis", "")
    if gem:
        lines.append("━━━━━━━ 🤖 Gemini 看好族群解讀 ━━━━━━━")
        lines.append(_esc(gem))
        lines.append("")
    lines.append("<i>※ Fed cycle 經典 playbook. 偵測依據 SHY+TLT 30d 走勢, 僅供參考.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())


def fmt_trump_policy_alerts(alerts: list, gemini_analysis="") -> str:
    """川普政策推播 (Tier 1) — 重點凸顯版.

    gemini_analysis 可以是 str (舊版) 或 dict (新版結構化, 含 headline/us_impact/tw_impact/global_impact/trade_action)
    新版優先用 dict 顯示影響重點, 不放一堆新聞連結 (用戶要求: 連結別太多, 重點要清楚).
    """
    if not alerts:
        return ""

    # I: 從 alerts[0] 拔出 filter_stats (川普推播在 check_trump_policy_news 附上的)
    filter_stats = (alerts[0] or {}).get("_filter_stats") if alerts else None

    lines = ["🇺🇸 <b>川普政策動向</b>"]

    if isinstance(gemini_analysis, dict) and gemini_analysis:
        g = gemini_analysis
        headline = _esc(g.get("headline", ""))
        if headline:
            lines.append(f"<i>{headline}</i>")
        lines.append("")

        # 主新聞 1 條 (帶連結) — 用戶要求: 不再列額外新聞題目
        if alerts:
            a0 = alerts[0]
            sym = _esc(a0.get("symbol", ""))
            title = _esc((a0.get("title") or "")[:80])
            link = a0.get("link", "")
            sym_tag = f"[{sym}] " if sym and sym != "GENERAL" else ""
            if link:
                lines.append(f"📰 {sym_tag}<a href=\"{link}\">{title}</a>")
            else:
                lines.append(f"📰 {sym_tag}{title}")
            lines.append("")

        # 影響分析
        us_imp = _esc(g.get("us_impact", ""))
        tw_imp = _esc(g.get("tw_impact", ""))
        if us_imp:
            lines.append(f"🇺🇸 <b>美股影響</b>: {us_imp}")
        if tw_imp:
            lines.append(f"🇹🇼 <b>台股影響</b>: {tw_imp}")
        if us_imp or tw_imp:
            lines.append("")
        # 用戶要求: 刪掉「全球商品影響」block (黃金/原油/美元/美債 4 項)
        # — 訊息瘦身, 只留美股/台股影響 + 操作建議

        # 操作建議 — 三段式
        long_play = _esc(g.get("long_play", ""))
        short_play = _esc(g.get("short_play", ""))
        pos_advice = _esc(g.get("position_advice", ""))
        risk_alert = _esc(g.get("risk_alert", ""))
        ta_fallback = _esc(g.get("trade_action", ""))  # 舊版相容
        if long_play or short_play or pos_advice or risk_alert:
            lines.append("💡 <b>操作建議</b>")
            if long_play:
                lines.append(f"  🟢 多單: {long_play}")
            if short_play:
                lines.append(f"  🔴 空單: {short_play}")
            if pos_advice:
                lines.append(f"  📦 持倉: {pos_advice}")
            if risk_alert:
                lines.append(f"  ⚠️ 風險: {risk_alert}")
            lines.append("")
        elif ta_fallback:
            lines.append(f"💡 <b>操作建議</b>: {ta_fallback}")
            lines.append("")
    else:
        # 舊版 fallback: Gemini 不可用 → 只顯示新聞 + 純文字分析
        for i, a in enumerate(alerts[:3]):
            sym = _esc(a.get("symbol", ""))
            title = _esc((a.get("title") or "")[:80])
            link = a.get("link", "")
            sym_tag = f"[{sym}] " if sym and sym != "GENERAL" else ""
            if link:
                lines.append(f"📰 {sym_tag}<a href=\"{link}\">{title}</a>")
            else:
                lines.append(f"📰 {sym_tag}{title}")
        if isinstance(gemini_analysis, str) and gemini_analysis:
            lines.append("")
            lines.append("<b>分析</b>")
            lines.append(_esc(gemini_analysis))
            lines.append("")
    # I: 顯示過濾統計
    if filter_stats and filter_stats.get("scanned", 0) > 0:
        s = filter_stats
        lines.append(
            f"<i>📊 過濾: 掃 {s['scanned']} 則 → 推 {s['passed']} 則 "
            f"(關鍵字擋 {s['filtered_keyword']} / "
            f"來源不在白名單 {s['filtered_publisher']} / "
            f"舊聞 {s['filtered_age']} / 已推過 {s['filtered_dedup']})</i>"
        )
    lines.append("<i>※ 川普政策推播會抓 Reuters/Bloomberg/WhiteHouse 白名單來源</i>")
    return "\n".join(lines)
