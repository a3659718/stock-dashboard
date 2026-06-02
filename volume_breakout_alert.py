"""
volume_breakout_alert.py
量爆突破即時警報 — 專業推播 Tier 1 訊號.

觸發條件 (3 同時):
  1. vol_ratio > 3.0 (近 5 日均量 3 倍以上)
  2. today_pct > +3% (大漲)
  3. 收盤 / 最新價 > 近 60 日 high (突破前波高)

對象: watchlist + holdings + actionable_picks pool (避免掃整池太慢)
Tier: Tier 1 (立即響鈴推播)
Throttle: per-(sym) per-day 1 次 (型態警報, 確認不重推)

API:
  check_volume_breakout() -> List[Dict]
  mark_alerts_sent(alerts) -> None
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List

import watchlist_store


def _scan_one_stock(sid: str, market: str = "TW") -> Dict | None:
    """掃單一檔判斷是否量爆突破. None = 沒觸發或抓資料失敗."""
    try:
        import data_sources as ds
        if market == "TW":
            df = ds.fetch_yf_history(f"{sid}.TW", period="90d", interval="1d")
            if df is None or df.empty:
                df = ds.fetch_yf_history(f"{sid}.TWO", period="90d", interval="1d")
        else:
            df = ds.fetch_yf_history(sid, period="90d", interval="1d")
        if df is None or df.empty or len(df) < 65:
            return None
        c = df["Close"].astype(float)
        v = df["Volume"].astype(float)
        cur = float(c.iloc[-1])
        prev_close = float(c.iloc[-2])
        today_pct = (cur / prev_close - 1) * 100
        if today_pct < 3.0:
            return None
        # vol_ratio
        vol_5d_avg = float(v.iloc[-6:-1].mean()) if v.iloc[-6:-1].mean() > 0 else 0
        if vol_5d_avg <= 0:
            return None
        vol_ratio = float(v.iloc[-1]) / vol_5d_avg
        if vol_ratio < 3.0:
            return None
        # 60d high (排除今日)
        high_60d = float(c.iloc[-61:-1].max())
        if cur <= high_60d:
            return None
        # 全條件命中
        return {
            "symbol": sid,
            "market": market,
            "current": round(cur, 2),
            "today_pct": round(today_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "high_60d": round(high_60d, 2),
            "breakout_pct": round((cur / high_60d - 1) * 100, 2),
            "tier": 1,  # 高 signal
        }
    except Exception as e:
        print(f"[vol_breakout] {sid} scan failed: {e}", flush=True)
        return None


def check_volume_breakout() -> List[Dict]:
    """掃 watchlist + holdings 找量爆突破. 回 alert list."""
    state = watchlist_store.load_monitor_state()
    vb_state = state.setdefault("volume_breakout_alert", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    if vb_state.get("date") != today_str:
        vb_state.clear()
        vb_state.update({"date": today_str, "alerted": []})
    alerted: set = set(vb_state.get("alerted") or [])

    universe = []
    try:
        wl = watchlist_store.load_watchlist() or []
        for sid in wl:
            s = str(sid).strip().upper()
            if not s: continue
            universe.append((s, "TW" if s.isdigit() else "US"))
    except Exception:
        pass
    try:
        import holdings_store
        for h in holdings_store.load_holdings() or []:
            sid = str(h.get("stock_id", "")).strip().upper()
            if not sid: continue
            mk = h.get("market", "TW" if sid.isdigit() else "US")
            universe.append((sid, mk))
    except Exception:
        pass
    # dedup
    universe = list({(s, m) for s, m in universe})

    alerts: List[Dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_scan_one_stock, s, m): s for s, m in universe}
        for fut in as_completed(futs):
            r = fut.result()
            if r and r["symbol"] not in alerted:
                alerts.append(r)

    return alerts


def mark_alerts_sent(alerts: List[Dict]) -> None:
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        vb = state.setdefault("volume_breakout_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        if vb.get("date") != today_str:
            vb.clear()
            vb.update({"date": today_str, "alerted": []})
        a_set = set(vb.get("alerted") or [])
        for a in alerts:
            a_set.add(a.get("symbol", ""))
        vb["alerted"] = sorted(a_set)
        state["volume_breakout_alert"] = vb
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[vol_breakout] mark_sent failed: {e}", flush=True)
