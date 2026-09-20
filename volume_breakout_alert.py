
from __future__ import annotations

import datetime as dt
from typing import Dict, List

import watchlist_store


def _us_today_str() -> str:

    now_utc = dt.datetime.utcnow()
    try:
        import index_alerts
        dst = bool(index_alerts._is_us_in_dst(now_utc.date()))
    except Exception:
        dst = 3 <= now_utc.month <= 10
    return (now_utc - dt.timedelta(hours=4 if dst else 5)).strftime("%Y-%m-%d")


def _tpe_today_str() -> str:
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def _market_today_str(market: str) -> str:
    """依標的所屬市場回它自己的「今天」。

    Bug fix (2026-09-20): 這支模組的 universe 同時含台股與美股 (watchlist +
    holdings), 但去重 state 原本整包共用一個「美東日期」(_us_today_str)。
    台股 session 的 monitor cron 落在 UTC 01:08~04:38, 而夏令時 (UTC-4) 下美東要到
    UTC 04:00 才換日 —— 日界正好落在台股盤中 (TPE 12:08):

        tick (UTC)      _us_today_str()   TPE
        01:08 ~ 03:38   昨天              09:08 ~ 11:38
        04:08 / 04:38   今天              12:08 / 12:38   ← 這裡「換日」

    而觸發條件 (今日 +3%、量比 ≥3、突破 60 日高) 是用日線算的, 早上成立整天都成立,
    所以 12:08 那個 tick 會清空 alerted 後把早上推過的每一檔原封不動再推一次,
    同時再燒一輪 push_cap 的每日次要額度。改成各自用自己市場的日界。
    """
    return _us_today_str() if str(market).upper() == "US" else _tpe_today_str()


def _bucket(vb: dict, market: str) -> dict:
    """取得該市場的當日去重 bucket (跨日自動重置)。

    state shape: {"TW": {"date": ..., "alerted": [...]}, "US": {...}}
    舊版是單層 {"date":..., "alerted":[...]} —— 這裡遇到會自動汰換掉, 最多只損失
    「切換當天」的一次去重, 不會誤推歷史資料。
    """
    mk = "US" if str(market).upper() == "US" else "TW"
    b = vb.get(mk)
    if not isinstance(b, dict):
        b = {}
    today = _market_today_str(mk)
    if b.get("date") != today:
        b = {"date": today, "alerted": []}
    vb[mk] = b
    vb.pop("date", None)        # 舊版單層殘留
    vb.pop("alerted", None)
    return b


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
    # 去重 bucket 依市場分開 (見 _market_today_str 的說明)
    alerted_by_mkt = {mk: set(_bucket(vb_state, mk).get("alerted") or [])
                      for mk in ("TW", "US")}

    universe = []
    try:
        # load_watchlist() 回的是 dict 陣列, 不是代號字串 → 一律走 load_watchlist_ids()
        for s in watchlist_store.load_watchlist_ids():
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
            if not r:
                continue
            mk = "US" if str(r.get("market", "TW")).upper() == "US" else "TW"
            if r["symbol"] in alerted_by_mkt[mk]:
                continue
            alerts.append(r)

    return alerts


def mark_alerts_sent(alerts: List[Dict]) -> None:
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        vb = state.setdefault("volume_breakout_alert", {})
        # Bug fix (2026-09-07): 讀寫兩端必須用同一個日界 (原本一端 UTC 一端美東,
        #   每個 tick 都判定換日 → 同一檔每 30 分鐘重推)。
        # Bug fix (2026-09-20): 再進一步 — 同一個日界對「台股 + 美股混合」的 universe
        #   仍然是錯的, 美東日界會落在台股盤中。改成每個市場各自一個 bucket。
        for a in alerts:
            b = _bucket(vb, a.get("market", "TW"))
            s = set(b.get("alerted") or [])
            s.add(a.get("symbol", ""))
            b["alerted"] = sorted(s)
        state["volume_breakout_alert"] = vb
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[vol_breakout] mark_sent failed: {e}", flush=True)


def unmark_alerts_sent(alerts: List[Dict]) -> None:
    """回滾 mark_alerts_sent — 送出失敗 / 被 daily cap 擋下時呼叫,
    把剛 claim 的 symbol 移除, 讓下個 tick 能重試 (否則會被靜默吞掉永不送出)."""
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        vb = state.get("volume_breakout_alert") or {}
        for a in alerts:
            b = _bucket(vb, a.get("market", "TW"))
            s = set(b.get("alerted") or [])
            s.discard(a.get("symbol", ""))
            b["alerted"] = sorted(s)
        state["volume_breakout_alert"] = vb
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[vol_breakout] unmark_sent failed: {e}", flush=True)
