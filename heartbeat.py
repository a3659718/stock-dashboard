"""
heartbeat.py
每日系統健康狀態檢查 — 偵測沉默失敗.

為什麼: 系統有多個 silent-failure path (yfinance rate-limited / FinMind token 過期 /
        Gemini quota 用完 / TG bot 死掉). 沒推播時用戶不知道是「真的沒事」還是「系統壞了」.
        Daily heartbeat 探外部 API + 印 ETF 監控資料新鮮度, 確保系統還活著.

執行頻率: 每天一次 (建議 UTC 06:30 = TPE 14:30, 台股剛收盤後, 流量低)
觸發: scripts/market_open_alert.py 的 `market=heartbeat` mode
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 外部 API 探針
# ---------------------------------------------------------------------------
def _probe_yfinance() -> Tuple[bool, str]:
    """打 ^GSPC (S&P 500, 永遠存在) 看 yfinance 是否活著."""
    try:
        import data_sources as ds
        df = ds.fetch_yf_history("^GSPC", period="5d", interval="1d")
        if df is None or df.empty:
            return False, "yfinance 回空 (rate-limited?)"
        return True, f"OK ({len(df)} bars)"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _probe_finmind() -> Tuple[bool, str]:
    """打 TaiwanStockInfo (輕量) 看 FinMind token 是否還有效."""
    try:
        import requests
        token = os.environ.get("FINMIND_TOKEN", "").strip()
        if not token:
            return False, "FINMIND_TOKEN 未設定"
        r = requests.get(
            "https://api.finmindtrade.com/api/v4/data",
            params={"dataset": "TaiwanStockInfo", "token": token},
            timeout=15,
        )
        if r.status_code == 200:
            return True, "OK"
        text = r.text[:120].replace("\n", " ")
        if r.status_code == 400 and "illegal" in r.text.lower():
            return False, f"Token 被拒 (illegal). 到 finmindtrade.com 重新生成"
        return False, f"HTTP {r.status_code}: {text}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _probe_gemini() -> Tuple[bool, str]:
    """跑一個 trivial Gemini call 確認 API key 有效."""
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return False, "Gemini 未設定 / 套件未安裝"
        import google.generativeai as genai
        genai.configure(api_key=ai_analyzer.get_gemini_key())
        model = genai.GenerativeModel("gemini-2.5-flash")
        resp = model.generate_content(
            "Reply with the single word OK.",
            generation_config={"max_output_tokens": 10, "temperature": 0},
            safety_settings=ai_analyzer.get_safety_settings(),
        )
        text = (getattr(resp, "text", "") or "").strip()
        if text:
            return True, f"OK ({text[:30]!r})"
        cands = getattr(resp, "candidates", []) or []
        if cands:
            fr = getattr(cands[0], "finish_reason", None)
            return True, f"OK (no text, finish_reason={fr})"
        return False, "Gemini 無回應 (quota / safety filter?)"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def _probe_telegram_config() -> Tuple[bool, str]:
    """確認 TG bot token + chat_id 都有設定 (不實打 sendMessage, 因為 heartbeat 本身就會 send)."""
    try:
        import notifier
        if notifier.is_configured():
            return True, "OK (token + chat_id 都有設定)"
        return False, "TG bot token 或 chat_id 缺失"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


# ---------------------------------------------------------------------------
# State 內部資料新鮮度
# ---------------------------------------------------------------------------
def _check_etf_freshness() -> List[Dict]:
    """讀 monitor_state.active_etf_holdings, 看每個 ETF 的 last_data_date 距今幾天.

    回 list of {etf_code, name, data_date, age_days, status}
    """
    try:
        import watchlist_store
        import active_etf_monitor
    except Exception:
        return []
    try:
        state = watchlist_store.load_monitor_state()
        etf_state = state.get("active_etf_holdings", {})
    except Exception:
        return []

    today = dt.date.today()
    results = []
    for code, cfg in active_etf_monitor.ETF_CONFIG.items():
        entry = etf_state.get(code, {})
        data_date = entry.get("last_data_date")
        if not data_date:
            results.append({
                "etf_code": code,
                "name": cfg.get("name", code),
                "data_date": "尚未抓取",
                "age_days": None,
                "status": "❓",
            })
            continue
        try:
            # data_date 格式 "2026/05/12"
            y, m, d = data_date.split("/")
            d_date = dt.date(int(y), int(m), int(d))
            age = (today - d_date).days
        except Exception:
            results.append({
                "etf_code": code,
                "name": cfg.get("name", code),
                "data_date": data_date,
                "age_days": None,
                "status": "❓",
            })
            continue
        if age <= 3:
            status = "✅"
        elif age <= 7:
            status = "🟡"
        else:
            status = "❌"
        results.append({
            "etf_code": code,
            "name": cfg.get("name", code),
            "data_date": data_date,
            "age_days": age,
            "status": status,
        })
    return results


def _check_state_persistence() -> Tuple[bool, str]:
    """判斷 monitor_state 是否真的有跨 cron run persist."""
    try:
        import watchlist_store
        # 看 Google Sheets 是否設定
        sheet = watchlist_store._get_sheet("monitor_state")
        if sheet is not None:
            return True, "Google Sheets ✓"
        # 否則只能靠本地檔案 (在 GH Actions 不會 persist)
        return False, "⚠️ 只有本地檔案, GH Actions ephemeral runner 跨 run 不會 persist!"
    except Exception as e:
        return False, f"檢查失敗: {e}"


# ---------------------------------------------------------------------------
# 主要 entry
# ---------------------------------------------------------------------------
def build_heartbeat_message() -> Tuple[str, bool]:
    """跑全部 health check, 組成 TG 訊息 (HTML).

    回 (msg, all_healthy)。

    BUG FIX (使用者要求): 原本這支每天固定推一封「系統健康日報」, 不管有沒有異常都送,
    等於每天一封「一切正常」的背景推播, 沒有資訊價值。改成回傳 all_healthy 這個布林值,
    讓呼叫端 (scripts/market_open_alert.py) 決定: 全部健康就只印 console log 不推播,
    有異常 (🟡/🔴) 才真的推 — 跟 daily_selfcheck.py 已經在用的「安靜時不推、異常才響鈴」
    是同一套設計哲學, 這裡補齊一致。
    """
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)  # 顯示 TPE 時間
    timestamp = now.strftime("%Y-%m-%d %H:%M TPE")

    # 跑全部 probe
    yf_ok,  yf_info  = _probe_yfinance()
    fm_ok,  fm_info  = _probe_finmind()
    gm_ok,  gm_info  = _probe_gemini()
    tg_ok,  tg_info  = _probe_telegram_config()
    st_ok,  st_info  = _check_state_persistence()
    etf_status       = _check_etf_freshness()

    all_critical_ok = yf_ok and fm_ok and tg_ok  # Gemini / state 是 nice-to-have
    overall_icon = "🟢" if all_critical_ok and st_ok else ("🟡" if all_critical_ok else "🔴")

    lines = [
        f"<b>{overall_icon} 系統健康日報</b>",
        f"<i>{timestamp}</i>",
        "",
        "<b>外部 API 狀態</b>",
        f"  {('✅' if yf_ok else '❌')} yfinance — {yf_info}",
        f"  {('✅' if fm_ok else '❌')} FinMind — {fm_info}",
        f"  {('✅' if gm_ok else '❌')} Gemini — {gm_info}",
        f"  {('✅' if tg_ok else '❌')} Telegram — {tg_info}",
        "",
        "<b>State 持久化</b>",
        f"  {('✅' if st_ok else '⚠️')} {st_info}",
    ]

    if etf_status:
        lines.append("")
        lines.append("<b>主動式 ETF 資料新鮮度</b>")
        for e in etf_status:
            age_str = f"{e['age_days']} 天前" if e["age_days"] is not None else "—"
            lines.append(
                f"  {e['status']} <code>{e['etf_code']}</code> {e['name']}: "
                f"{e['data_date']} ({age_str})"
            )

    # 加診斷提示
    if not all_critical_ok:
        lines.append("")
        lines.append("<b>⚠️ 行動建議</b>")
        if not yf_ok:
            lines.append("  • yfinance fail: 可能 IP 被 rate-limit, 等 1-2 hr 自動恢復")
        if not fm_ok:
            lines.append("  • FinMind fail: 到 finmindtrade.com 看 token 是否過期")
        if not gm_ok:
            lines.append("  • Gemini fail: 可能 quota 用完, 看 Google AI Studio dashboard")
        if not tg_ok:
            lines.append("  • TG config fail: 檢查 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secret")

    if not st_ok:
        lines.append("")
        lines.append(
            "<i>⚠️ State 沒持久化警告: "
            "ratchet / cooldown / daily-cap 全部會在每次 cron run 後失效, "
            "你會收到大量重複警報. 請設定 Google Sheets credentials.</i>"
        )

    all_healthy = all_critical_ok and st_ok  # 對應 overall_icon 是不是 🟢

    # M3 fix: 走 byte-length truncate, 避免 ETF 監控擴張後超過 TG 4096 byte 上限
    try:
        import notifier as _n
        return _n._truncate_tg_msg("\n".join(lines)), all_healthy
    except Exception:
        # notifier 載入失敗時 fallback 用 char-length truncate (粗略)
        out = "\n".join(lines)
        if len(out.encode("utf-8")) > 3900:
            out = out.encode("utf-8")[:3900].decode("utf-8", errors="ignore") + "\n…(節錄)"
        return out, all_healthy
