"""
system_health.py
系統健康度 / Admin Dashboard.

收集:
  - monitor_state 各 alert module 的 last_fired_at / cooldown / today count
  - FinMind API 額度 (api/v4/user_info)
  - Gemini API 可用性 (是否設 key)
  - 最近 24h 推播紀錄 (從 monitor_state["push_history"], notifier 寫入)
  - GH Actions cron 上次成功時間 (從 monitor_state[*]["last_run_at"] 推斷)

API:
  collect_health() -> Dict
  record_push(push_type, ok, error_msg=None) -> None  # notifier 呼叫
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import data_sources as ds
import watchlist_store


# 推播紀錄上限 (避免 monitor_state 變超大)
# 500 筆夠涵蓋 7 天統計 (高頻日可能 50+/day)
PUSH_HISTORY_MAX = 500


def _now_utc() -> dt.datetime:
    """tz-naive UTC now (Py 3.12 安全)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _parse_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo:
            d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return d
    except Exception:
        return None


def _ago(d: Optional[dt.datetime]) -> str:
    if d is None:
        return "—"
    now = _now_utc()
    delta = (now - d).total_seconds()
    if delta < 0:
        return "未來"
    if delta < 60:
        return f"{int(delta)}s 前"
    if delta < 3600:
        return f"{int(delta/60)}m 前"
    if delta < 86400:
        return f"{delta/3600:.1f}h 前"
    return f"{delta/86400:.1f}d 前"


# === 1. 各 alert module 狀態 ===
def _extract_module_status(monitor_state: Dict) -> List[Dict]:
    """逐個 alert module 抽 state 摘要."""
    results = []

    modules = [
        # (key, label, 內部時間鍵, 內部 count 鍵)
        ("intraday_reversal", "🔁 盤中反轉", "last_alert_at", None),
        ("weak_open", "📉 開盤即弱", "last_alert_at", None),
        ("strong_sector_alert", "🚀 強勢族群", "last_batch_at", "sectors_alerted"),
        ("holdings_intraday_alert", "⚠️ 持倉風險", "last_alert_at", "alerted"),
        ("news_event_alert", "📰 事件新聞", "last_batch_at", "alerted"),
        ("morning_brief", "🌅 晨報", "last_sent_at", None),
        ("market_close_brief", "🌆 收盤摘要", "last_sent_at", None),
        ("tw_mid", "🇹🇼 TW 中盤", "last_sent_at", None),
    ]

    for key, label, time_key, count_key in modules:
        sub = monitor_state.get(key, {}) or {}
        if not sub:
            results.append({
                "module": label, "key": key,
                "last_fired": None, "last_fired_ago": "—",
                "today_count": 0, "status": "🔵 未啟動"
            })
            continue

        # 嘗試找 last fired (可能是 isoformat 或 nested)
        last_at = None
        for tk in [time_key, "last_alert_at", "last_batch_at", "last_sent_at", "last_fired_at"]:
            v = sub.get(tk) if isinstance(sub, dict) else None
            if isinstance(v, str):
                last_at = _parse_iso(v)
                break
            if isinstance(v, dict):  # 巢狀 e.g. {symbol: {last_alert_at}}
                for s_sub in v.values():
                    if isinstance(s_sub, dict) and s_sub.get(tk):
                        d = _parse_iso(s_sub.get(tk))
                        if d and (last_at is None or d > last_at):
                            last_at = d

        # today count
        today_count = 0
        if count_key:
            cv = sub.get(count_key)
            if isinstance(cv, list):
                today_count = len(cv)
            elif isinstance(cv, dict):
                today_count = len(cv)
        # 對 nested per-symbol state 估今日 count
        elif isinstance(sub, dict):
            today_str = dt.date.today().strftime("%Y-%m-%d")
            cnt = 0
            for v in sub.values():
                if isinstance(v, dict) and v.get("date") == today_str:
                    cnt += 1
            today_count = cnt

        # status 判斷
        if last_at:
            ago_sec = (_now_utc() - last_at).total_seconds()
            if ago_sec < 1800:
                status = "🟢 活躍"
            elif ago_sec < 86400:
                status = "🟡 今日有跑"
            else:
                status = "⚪ 閒置"
        else:
            status = "🔵 未啟動"

        results.append({
            "module": label,
            "key": key,
            "last_fired": last_at.isoformat() if last_at else None,
            "last_fired_ago": _ago(last_at),
            "today_count": today_count,
            "status": status,
        })

    return results


# === 2. FinMind 額度 ===
def _check_finmind_quota() -> Dict:
    """call /api/v4/user_info 拿 user_count 與 api_request_limit."""
    out = {"available": False, "user_count": None, "limit": None, "pct_used": None, "err": None}
    token = ds.get_finmind_token()
    if not token:
        out["err"] = "未設 FINMIND_TOKEN"
        return out
    try:
        import requests
        r = requests.get("https://api.finmindtrade.com/api/v4/user_info",
                         params={"token": token}, timeout=10)
        r.raise_for_status()
        j = r.json() or {}
        # 不同版本 schema, 試常見鍵
        data = j.get("data") if isinstance(j.get("data"), dict) else j
        out["available"] = True
        out["user_count"] = data.get("user_count") or data.get("api_request_count")
        out["limit"] = data.get("api_request_limit") or data.get("user_count_limit") or 600
        if out["user_count"] is not None and out["limit"]:
            try:
                out["pct_used"] = round(float(out["user_count"]) / float(out["limit"]) * 100, 1)
            except Exception:
                pass
    except Exception as e:
        out["err"] = f"{type(e).__name__}: {str(e)[:100]}"
    return out


# === 3. Gemini 可用性 ===
def _check_gemini() -> Dict:
    out = {"available": False, "key_set": False, "err": None}
    try:
        import ai_analyzer
        out["available"] = ai_analyzer.gemini_available()
        out["key_set"] = out["available"]
    except Exception as e:
        out["err"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


# === 4. Telegram 可用性 ===
def _check_telegram() -> Dict:
    out = {"available": False, "err": None}
    try:
        import notifier
        out["available"] = notifier.is_configured()
    except Exception as e:
        out["err"] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


# === 5. GH Actions 上次跑時間 (從各 module last_fired 取最近) ===
def _guess_gh_actions_health(module_status: List[Dict]) -> Dict:
    """從各 alert module 的最後執行時間, 推斷 GH Actions monitor cron 是否還在跑."""
    fired_times = []
    for m in module_status:
        if m.get("last_fired"):
            d = _parse_iso(m["last_fired"])
            if d:
                fired_times.append(d)
    if not fired_times:
        return {"latest": None, "latest_ago": "—", "status": "🔴 從未執行"}
    latest = max(fired_times)
    ago_sec = (_now_utc() - latest).total_seconds()
    if ago_sec < 1200:  # < 20 min
        status = "🟢 正常 (monitor cron 在跑)"
    elif ago_sec < 7200:  # < 2 hr
        status = "🟡 警告 (>20min 沒新 alert; 可能是市場時段以外, 也可能 cron 失敗)"
    elif ago_sec < 86400:
        status = "🟠 危險 (今天有跑過, 但 >2hr 沒新訊號)"
    else:
        status = "🔴 故障 (>24hr 沒任何 alert; 檢查 GH Actions)"
    return {
        "latest": latest.isoformat(),
        "latest_ago": _ago(latest),
        "status": status,
    }


# === 6. 推播紀錄 (notifier 寫入) ===
def record_push(push_type: str, ok: bool, error_msg: Optional[str] = None) -> None:
    """notifier.send_message 每次呼叫後紀錄. 寫到 monitor_state["push_history"].
    結構: [{ts, type, ok, err}, ...] 最多 PUSH_HISTORY_MAX 筆.
    """
    try:
        state = watchlist_store.load_monitor_state()
        hist = state.get("push_history") or []
        if not isinstance(hist, list):
            hist = []
        hist.append({
            "ts": _now_utc().isoformat(),
            "type": str(push_type)[:50],
            "ok": bool(ok),
            "err": str(error_msg)[:200] if error_msg else None,
        })
        # 只保留最後 PUSH_HISTORY_MAX 筆
        if len(hist) > PUSH_HISTORY_MAX:
            hist = hist[-PUSH_HISTORY_MAX:]
        state["push_history"] = hist
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[system_health] record_push failed: {e}", flush=True)


def record_dashboard_open() -> None:
    """用戶開啟健康度 tab 自動 call. 紀錄一筆 DASHBOARD_PING 到 push_history.
    用途: 提供「絕對時間錨」, 對比 cron 推播時間判斷 cron 是否活著.
    """
    record_push("DASHBOARD_PING", True)


def diagnose_cron_health(monitor_state: Dict) -> Dict:
    """根據 push_history 計算 cron 健康度.
    回 {status: 🟢/🟡/🔴, label: str, last_cron_ago: str, last_ping_ago: str, lag_sec: int}.
    """
    hist = monitor_state.get("push_history") or []
    if not isinstance(hist, list):
        hist = []
    last_cron_ts = None
    last_ping_ts = None
    for h in hist:
        ts = _parse_iso(h.get("ts"))
        if ts is None:
            continue
        t = h.get("type", "")
        if t == "DASHBOARD_PING":
            if last_ping_ts is None or ts > last_ping_ts:
                last_ping_ts = ts
        else:
            if last_cron_ts is None or ts > last_cron_ts:
                last_cron_ts = ts

    if last_cron_ts is None:
        return {
            "status": "🔴 從未推播",
            "label": "push_history 完全沒有 cron 推播紀錄 — cron 可能從未跑過或剛 reset state.",
            "last_cron_ago": "—",
            "last_ping_ago": _ago(last_ping_ts),
            "lag_sec": None,
        }

    now = _now_utc()
    cron_lag_sec = (now - last_cron_ts).total_seconds()

    # 判斷 (考慮週末/盤外時段)
    weekday = now.weekday()
    is_weekend = weekday >= 5
    cur_hour = now.hour + now.minute / 60.0
    in_session = (1.0 <= cur_hour < 5.5) or (13.0 <= cur_hour < 21.5)
    in_session = (1.0 <= cur_hour < 5.5) or (13.0 <= cur_hour < 21.5)
    # 寬鬆時段 (盤外 / 週末): cron 本來就少跑, 標準放寬
    if is_weekend or not in_session:
        warn_threshold = 6 * 3600
        fail_threshold = 24 * 3600
    else:
        warn_threshold = 30 * 60
        fail_threshold = 2 * 3600

    if cron_lag_sec < warn_threshold:
        status = "🟢 Cron 健康"
        label = f"最近推播 {_ago(last_cron_ts)} (在預期範圍內)"
    elif cron_lag_sec < fail_threshold:
        status = "🟡 Cron lag"
        label = f"最近推播 {_ago(last_cron_ts)} (>{warn_threshold//60}min, 可能延遲)"
    else:
        status = "🔴 Cron 故障"
        label = f"最近推播 {_ago(last_cron_ts)} (>{fail_threshold//3600}hr, 立即檢查 GH Actions!)"

    return {
        "status": status,
        "label": label,
        "last_cron_ago": _ago(last_cron_ts),
        "last_ping_ago": _ago(last_ping_ts) if last_ping_ts else "—",
        "lag_sec": int(cron_lag_sec),
        "in_session": in_session,
        "is_weekend": is_weekend,
    }


def _summarize_push_history(monitor_state: Dict, hours: int = 24) -> Dict:
    """過去 N 小時推播統計 (不含 DASHBOARD_PING)."""
    hist = monitor_state.get("push_history") or []
    if not isinstance(hist, list):
        return {"total": 0, "ok": 0, "fail": 0, "by_type": {}, "recent": []}
    cutoff = _now_utc() - dt.timedelta(hours=hours)
    recent = []
    for h in hist:
        ts = _parse_iso(h.get("ts"))
        if ts and ts >= cutoff and h.get("type") != "DASHBOARD_PING":
            recent.append(h)
    by_type = {}
    ok_n, fail_n = 0, 0
    for r in recent:
        t = r.get("type", "?")
        by_type.setdefault(t, {"ok": 0, "fail": 0})
        if r.get("ok"):
            by_type[t]["ok"] += 1
            ok_n += 1
        else:
            by_type[t]["fail"] += 1
            fail_n += 1
    return {
        "total": len(recent),
        "ok": ok_n,
        "fail": fail_n,
        "by_type": by_type,
        "recent": recent[-15:],
    }


def collect_health() -> Dict:
    """整合所有健康指標."""
    try:
        state = watchlist_store.load_monitor_state() or {}
    except Exception as e:
        state = {}
        print(f"[system_health] load_monitor_state failed: {e}", flush=True)

    module_status = _extract_module_status(state)
    return {
        "ts": _now_utc().isoformat(),
        "cron_health": diagnose_cron_health(state),
        "gh_actions": _guess_gh_actions_health(module_status),
        "modules": module_status,
        "finmind": _check_finmind_quota(),
        "gemini": _check_gemini(),
        "telegram": _check_telegram(),
        "push_24h": _summarize_push_history(state, hours=24),
        "push_7d": _summarize_push_history(state, hours=24 * 7),
    }
