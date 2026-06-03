"""
asia_leading_alert.py
亞股先行警報 — 日經/KOSPI 盤中急漲跌 → 台股可能跟動.

觸發條件:
  - 日經 (^N225) 或 KOSPI (^KS11) vs today_open 變化 ≥ ±1.0%
  - 台股仍在盤中 (TPE 9:00-13:30)
  - per-(sym, direction) per-day 1 次 (避免反覆推)

API:
  check_asia_leading() -> List[Dict]
  mark_alerts_sent(alerts) -> None
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List

import watchlist_store


# 觸發門檻
LEADING_PCT_THRESHOLD = 1.0   # 日韓盤中 ±1% 才推
TWII_OPEN_HOUR_TPE = 9        # 台股開盤
TWII_CLOSE_HOUR_TPE = 13.5    # 台股收盤


def _is_twii_in_session() -> bool:
    """台股是否盤中 (含週末擋掉)."""
    import pytz
    tw = dt.datetime.now(pytz.timezone("Asia/Taipei"))
    if tw.weekday() >= 5:
        return False
    cur = tw.hour + tw.minute / 60.0
    return TWII_OPEN_HOUR_TPE <= cur < TWII_CLOSE_HOUR_TPE


def _fetch_intraday_pct_vs_open(symbol: str) -> Dict:
    """抓 intraday 5m bars 算 vs today_open (HIGH fix: daily 在盤中會 stale)."""
    try:
        import data_sources as ds
        df = ds.fetch_yf_history(symbol, period="2d", interval="5m")
        if df is None or df.empty:
            df = ds.fetch_yf_history(symbol, period="3d", interval="1d")
            if df is None or df.empty:
                return {}
        c = df["Close"].astype(float)
        cur = float(c.iloc[-1])
        # 今日 open = 今日第一根 bar 的 open
        try:
            if hasattr(df.index, "date"):
                today = df.index[-1].date()
                today_df = df[df.index.date == today]
                if not today_df.empty:
                    op = float(today_df["Open"].iloc[0])
                else:
                    op = float(df["Open"].iloc[-1])
            else:
                op = float(df["Open"].iloc[-1])
        except Exception:
            op = float(df["Open"].iloc[-1])
        if op <= 0:
            return {}
        return {
            "symbol": symbol,
            "current": round(cur, 2),
            "open": round(op, 2),
            "pct_vs_open": round((cur / op - 1) * 100, 2),
        }
    except Exception as e:
        print(f"[asia_leading] {symbol} fetch failed: {e}", flush=True)
        return {}


def check_asia_leading() -> List[Dict]:
    """檢查日經/KOSPI 是否盤中急漲跌 → 推台股 leading alert."""
    # 台股不在盤中就 skip (沒意義)
    if not _is_twii_in_session():
        return []

    state = watchlist_store.load_monitor_state()
    al_state = state.setdefault("asia_leading_alert", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    if al_state.get("date") != today_str:
        al_state.clear()
        al_state.update({"date": today_str, "alerted": []})
    alerted: set = set(al_state.get("alerted") or [])

    alerts: List[Dict] = []
    for sym, name in [("^N225", "日經 225"), ("^KS11", "KOSPI")]:
        snap = _fetch_intraday_pct_vs_open(sym)
        if not snap or snap.get("pct_vs_open") is None:
            continue
        pct = snap["pct_vs_open"]
        if pct >= LEADING_PCT_THRESHOLD:
            direction = "up"
        elif pct <= -LEADING_PCT_THRESHOLD:
            direction = "down"
        else:
            continue
        # per-(sym, direction) dedup
        key = f"{sym}:{direction}"
        if key in alerted:
            continue
        alerts.append({
            "symbol": sym,
            "name": name,
            "current": snap["current"],
            "open": snap["open"],
            "pct_vs_open": pct,
            "direction": direction,
            "tier": 2,
        })

    # 對台股影響 (簡單推論)
    if alerts:
        try:
            twii = _fetch_intraday_pct_vs_open("^TWII")
            for a in alerts:
                a["twii_pct_vs_open"] = twii.get("pct_vs_open") if twii else None
                # 推論: 日韓跌 → 台股可能跟跌; 日韓漲 → 台股可能跟漲 / 已跟漲
                if a["direction"] == "down":
                    a["narrative"] = (
                        "亞股急跌, 留意台股是否跟跌; 持倉減碼觀望, "
                        "強勢族群拉回也避免追多"
                    )
                else:
                    a["narrative"] = (
                        "亞股急漲, 台股可能跟強; 觀察強勢族群龍頭是否帶量續攻"
                    )
        except Exception:
            pass

    return alerts


def mark_alerts_sent(alerts: List[Dict]) -> None:
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        al = state.setdefault("asia_leading_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        if al.get("date") != today_str:
            al.clear()
            al.update({"date": today_str, "alerted": []})
        a_set = set(al.get("alerted") or [])
        for a in alerts:
            a_set.add(f"{a.get('symbol','')}:{a.get('direction','')}")
        al["alerted"] = sorted(a_set)
        state["asia_leading_alert"] = al
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[asia_leading] mark_sent failed: {e}", flush=True)
