"""
tx_futures_alert.py — 台指期 / 微台 / 小台專屬 alert

3 種訊號 (任一觸發即推):
  1. 升貼水異常 (basis) — 超過 ±60 點 (=正常波動 +1 σ)
     - 大幅逆價差 → 多方退場 / 看空
     - 大幅正價差 → 多方搶買 / 看多
  2. 法人多空淨額異動 — 三大法人台指期淨額連 2 日轉向
  3. 散戶反指標 — 小台散戶多單多 ≥ 70% (極端反指標) → 空訊號
                  小台散戶空單多 ≥ 70% → 多訊號

操作建議導向:
  - 給「微台多 / 微台空 / 觀望」明確結論
  - 給「進場價 / 停損點 / 預期區間」
  - 不需深入指標細節

API:
  check_tx_futures_alerts() -> List[Dict]  # 0-N 條 alerts
  fmt_tx_futures_alerts(alerts) -> str     # TG HTML
  check_and_push() -> Dict                  # cron 用 (有 cooldown)
"""
from __future__ import annotations

import datetime as dt
from datetime import timezone
from typing import Dict, List

# 門檻 (可調)
BASIS_ANOMALY = 60.0        # 升貼水 ±60 點 = 異常
RETAIL_EXTREME_PCT = 70.0   # (保留, 目前未使用 — FinMind 沒有給散戶多空「比例」)
RETAIL_EXTREME_NET = 8000   # 小台散戶淨留倉 ±8000 口 = 極端反指標 (與 producer 的 signal 門檻一致)
INST_FLIP_DAYS = 2          # 法人連 N 日轉向

COOLDOWN_MIN = 90            # 90 min 內不重複推同種
DAILY_CAP = 4                # 一天最多 4 則


def _classify_basis(basis: float, twii: float) -> Dict:
    """分類升貼水."""
    out = {"signal": "neutral", "action": "觀望", "reason": ""}
    if basis is None or twii is None or twii <= 0:
        return out
    bp_pct = abs(basis) / twii * 1000  # 百分位 (basis points / 千分比)
    if basis >= BASIS_ANOMALY:
        out.update(
            signal="bullish_strong",
            action="微台多 (但留意短線過熱)",
            reason=f"正價差 {basis:+.0f} 點 (>{BASIS_ANOMALY}), 多方搶買, 過 30 分鐘可能拉回",
        )
    elif basis <= -BASIS_ANOMALY:
        out.update(
            signal="bearish_strong",
            action="微台空 (留意急殺後反彈)",
            reason=f"逆價差 {basis:+.0f} 點 (<-{BASIS_ANOMALY}), 多方退場, 空方有效",
        )
    elif basis >= BASIS_ANOMALY * 0.5:
        out.update(signal="bullish_mild", action="微台偏多",
                    reason=f"正價差 {basis:+.0f} 點, 多方略強")
    elif basis <= -BASIS_ANOMALY * 0.5:
        out.update(signal="bearish_mild", action="微台偏空",
                    reason=f"逆價差 {basis:+.0f} 點, 空方略強")
    return out


def _check_basis_alert(snap: Dict) -> Dict:
    """檢查升貼水異常."""
    # Bug fix (2026-09): 原本讀 "twii_close" / "spot_price", 但
    # institutional_positioning._fetch_futures_basis() 回的只有 basis / basis_pct / signal,
    # 從來沒有這兩個 key → twii 恆為 None → _classify_basis 直接回 neutral → 永不觸發。
    # producer 端已補上 "spot", 這裡改讀它 (舊 key 保留當 fallback)。
    basis_info = snap.get("basis") or {}
    basis = basis_info.get("basis")
    twii = (basis_info.get("spot") or basis_info.get("twii_close")
            or basis_info.get("spot_price"))
    if basis is None:
        return {}
    cls = _classify_basis(basis, twii)
    if cls["signal"] in ("bullish_strong", "bearish_strong"):
        return {
            "type": "basis_anomaly",
            "tier": 1,  # 高優先
            "signal": cls["signal"],
            "action": cls["action"],
            "title": "升貼水異常",
            "reason": cls["reason"],
            "current_twii": twii,
            "basis": basis,
        }
    return {}


def _check_inst_flip_alert(snap: Dict) -> Dict:
    """檢查法人多空淨額連續轉向."""
    # Bug fix (2026-09): 兩個 key 都錯 —
    #   1. snapshot 裡的 key 是 "futures" 不是 "inst_futures" → fut 恆為 {}
    #   2. producer 從來沒有算過 "foreign_net_change_5d"
    # 它有的是 fut["data"] (最近 5 個交易日的 foreign_oi) 與 fut["foreign_net_oi"] (最新一日),
    # 所以 5 日變化在這裡自己從 data 頭尾算。
    fut = snap.get("futures") or snap.get("inst_futures") or {}
    foreign_net = fut.get("foreign_net_oi")
    if foreign_net is None:
        return {}
    series = [d.get("foreign_oi") for d in (fut.get("data") or [])
              if d.get("foreign_oi") is not None]
    if len(series) < 2:
        return {}
    foreign_change = float(series[-1]) - float(series[0])
    if foreign_change >= 5000:
        return {
            "type": "inst_flip_bullish",
            "tier": 1,
            "signal": "bullish",
            "action": "微台多 / 台指多單",
            "title": "外資台指轉多",
            "reason": f"外資 5 日台指淨多單 +{foreign_change:,.0f} 口, 大舉建多",
        }
    if foreign_change <= -5000:
        return {
            "type": "inst_flip_bearish",
            "tier": 1,
            "signal": "bearish",
            "action": "微台空 / 台指空單",
            "title": "外資台指轉空",
            "reason": f"外資 5 日台指淨空單 {foreign_change:,.0f} 口, 大舉建空",
        }
    return {}


def _check_retail_extreme_alert(snap: Dict) -> Dict:
    """檢查散戶反指標."""
    # Bug fix (2026-09): 原本讀 "long_pct" / "short_pct", 但
    # institutional_positioning._fetch_retail_mtx() 回的是 {"retail_net": 口數, "signal": str},
    # 根本沒有百分比欄位 → 兩個值恆為 None → 直接 return {} → 這條訊號永不觸發。
    # 改用它真的有的「散戶淨留倉口數」, 門檻沿用 producer 自己在 signal 裡用的 ±8000 口。
    retail = snap.get("retail_mtx") or {}
    retail_net = retail.get("retail_net")
    if retail_net is None:
        return {}
    if retail_net >= RETAIL_EXTREME_NET:
        return {
            "type": "retail_long_extreme",
            "tier": 2,
            "signal": "bearish_contra",
            "action": "微台偏空 (散戶反指標)",
            "title": "小台散戶過度看多",
            "reason": f"散戶淨留多 {retail_net:+,.0f} 口 (≥{RETAIL_EXTREME_NET:,}), 反指標見頂",
        }
    if retail_net <= -RETAIL_EXTREME_NET:
        return {
            "type": "retail_short_extreme",
            "tier": 2,
            "signal": "bullish_contra",
            "action": "微台偏多 (散戶反指標)",
            "title": "小台散戶過度看空",
            "reason": f"散戶淨留空 {retail_net:+,.0f} 口 (≤-{RETAIL_EXTREME_NET:,}), 反指標見底",
        }
    return {}


def _entry_advice(alert: Dict, twii: float = None) -> Dict:
    """根據 alert signal 算進場/停損/目標 (微台 1 點 = TWD 50)."""
    signal = alert.get("signal", "")
    out = {}
    if not twii or twii <= 0:
        return out
    if "bullish" in signal:
        entry = round(twii * 0.999, 0)  # 略低於現價
        stop = round(twii * 0.99, 0)    # -1%
        target = round(twii * 1.01, 0)  # +1%
        out["entry"] = entry
        out["stop"] = stop
        out["target"] = target
        out["rr"] = round((target - entry) / max(1, entry - stop), 2)
    elif "bearish" in signal:
        entry = round(twii * 1.001, 0)
        stop = round(twii * 1.01, 0)
        target = round(twii * 0.99, 0)
        out["entry"] = entry
        out["stop"] = stop
        out["target"] = target
        out["rr"] = round((entry - target) / max(1, stop - entry), 2)
    return out


def check_tx_futures_alerts() -> List[Dict]:
    """跑所有檢查, 回 alert list (按 tier 排序)."""
    alerts = []
    try:
        import institutional_positioning as ip
        snap = ip.fetch_institutional_snapshot()
    except Exception as e:
        print(f"[tx_futures] snapshot fail: {e}", flush=True)
        return []
    # Bug fix (2026-09-18): institutional_positioning._fetch_futures_basis() 回的 key
    # 是 "spot" (見該檔 :227/:259), 沒有 "twii_close" → twii 恆為 None →
    # _entry_advice() 直接 return {} → 進場價/停損/目標/R:R 永遠不會出現在推播上。
    # 同檔 _check_basis_alert() 早就改讀 spot 了, 這行漏改。
    _basis_info = snap.get("basis") or {}
    twii = (_basis_info.get("spot")
            or _basis_info.get("twii_close")
            or _basis_info.get("spot_price"))
    # 1. 升貼水
    a1 = _check_basis_alert(snap)
    if a1: alerts.append(a1)
    # 2. 法人轉向
    a2 = _check_inst_flip_alert(snap)
    if a2: alerts.append(a2)
    # 3. 散戶反指標
    a3 = _check_retail_extreme_alert(snap)
    if a3: alerts.append(a3)

    # 加上進出場價
    for a in alerts:
        adv = _entry_advice(a, twii)
        a.update(adv)

    return sorted(alerts, key=lambda x: x.get("tier", 3))


def fmt_tx_futures_alerts(alerts: List[Dict]) -> str:
    """組 TG HTML 訊息. 失敗回空字串."""
    if not alerts:
        return ""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)
    now_tpe = (dt.datetime.now(timezone.utc) + dt.timedelta(hours=8)).strftime("%H:%M")
    lines = [f"📐 <b>台指期/微台訊號</b> · {now_tpe} TPE"]
    for a in alerts:
        title = _esc(a.get("title", ""))
        action = _esc(a.get("action", ""))
        reason = _esc(a.get("reason", ""))
        tier_emoji = {1: "🔴", 2: "🟡", 3: "⚪"}.get(a.get("tier", 3), "⚪")
        lines.append("")
        lines.append(f"{tier_emoji} <b>{title}</b> → {action}")
        lines.append(f"  <i>{reason}</i>")
        if "entry" in a:
            lines.append(
                f"  📍 進場 {a['entry']:.0f} · "
                f"停損 {a['stop']:.0f} · "
                f"目標 {a['target']:.0f} · "
                f"R:R {a.get('rr', 0):.2f}"
            )
    lines.append("")
    lines.append("<i>※ 微台 1 點 = TWD 50. 嚴守停損, 不留倉抗.</i>")
    return "\n".join(lines)


def check_and_push() -> Dict:
    """cron 用. 有 cooldown 跟 daily cap."""
    try:
        import holiday_check
        if holiday_check.is_market_closed_today("TW"):
            return {"triggered": False, "reason": "holiday"}
    except Exception:
        pass

    try:
        import watchlist_store
        state = watchlist_store.load_monitor_state()
    except Exception:
        state = {}

    now_utc = dt.datetime.now(timezone.utc)
    tx_state = state.setdefault("tx_futures_alert", {})
    today_key = now_utc.strftime("%Y-%m-%d")
    today_data = tx_state.setdefault(today_key, {"count": 0, "last_ts": None, "types": []})

    # daily cap
    if today_data["count"] >= DAILY_CAP:
        return {"triggered": False, "reason": "daily_cap_hit"}

    # cooldown
    if today_data.get("last_ts"):
        try:
            last_ts = dt.datetime.fromisoformat(today_data["last_ts"].replace("Z", "+00:00"))
            mins_since = (now_utc - last_ts).total_seconds() / 60
            if mins_since < COOLDOWN_MIN:
                return {"triggered": False, "reason": "cooldown"}
        except Exception:
            pass

    alerts = check_tx_futures_alerts()
    if not alerts:
        return {"triggered": False, "reason": "no_alerts"}

    # 跨類去重: 已推過的 type 不再推
    new_alerts = [a for a in alerts if a.get("type") not in today_data.get("types", [])]
    if not new_alerts:
        return {"triggered": False, "reason": "all_already_sent"}

    msg = fmt_tx_futures_alerts(new_alerts)
    if not msg:
        return {"triggered": False, "reason": "empty_msg"}

    try:
        import notifier
        ok, info = notifier.send_message(msg, disable_preview=True)
        if ok:
            today_data["count"] = today_data.get("count", 0) + 1
            today_data["last_ts"] = now_utc.isoformat()
            today_data.setdefault("types", []).extend([a["type"] for a in new_alerts])
            try:
                watchlist_store.save_monitor_state(state)
            except Exception:
                pass
        return {"triggered": True, "n_alerts": len(new_alerts), "sent": ok}
    except Exception as e:
        return {"triggered": False, "err": str(e)}
