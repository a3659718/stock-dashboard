
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import watchlist_store
import pandas as pd


def _us_today_str() -> str:

    now_utc = dt.datetime.utcnow()
    try:
        import index_alerts
        dst = bool(index_alerts._is_us_in_dst(now_utc.date()))
    except Exception:
        dst = 3 <= now_utc.month <= 10
    return (now_utc - dt.timedelta(hours=4 if dst else 5)).strftime("%Y-%m-%d")


DEFAULT_US_UNIVERSE_BREAKOUT = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
    "AVGO", "AMD", "PLTR", "DELL", "ORCL", "CRM", "LLY", "REGN",
    "RKLB", "ASTS", "BABA", "TSM", "ARM", "SMCI",
    "SOFI", "MARA", "RIOT", "CELH", "DUOL", "NET", "DDOG",
]


def _calc_consolidation_metrics(df) -> Dict:
    """算 20d 盤整指標 + 今日突破判斷."""
    try:
        c = df["Close"].astype(float)
        h = df["High"].astype(float)
        low = df["Low"].astype(float)
        v = df["Volume"].astype(float)
        if len(c) < 22:
            return {}
        cur = float(c.iloc[-1])
        prev = float(c.iloc[-2])
        today_pct = (cur / prev - 1) * 100
        # 20d window (排除今日)
        c20 = c.iloc[-21:-1]
        h20 = h.iloc[-21:-1]
        l20 = low.iloc[-21:-1]
        v20 = v.iloc[-21:-1]
        if c20.empty:
            return {}
        max_20 = float(h20.max())
        min_20 = float(l20.min())
        range_pct = (max_20 - min_20) / min_20 * 100 if min_20 > 0 else 999
        tr = (h20 - l20).abs()
        atr = float(tr.mean())
        atr_pct = atr / cur * 100 if cur > 0 else 999
        # vol ratio
        vol_5d_avg = float(v.iloc[-6:-1].mean()) if v.iloc[-6:-1].mean() > 0 else 0
        vol_ratio = float(v.iloc[-1]) / vol_5d_avg if vol_5d_avg > 0 else 0
        return {
            "current": round(cur, 2),
            "today_pct": round(today_pct, 2),
            "high_20d": round(max_20, 2),
            "low_20d": round(min_20, 2),
            "range_pct_20d": round(range_pct, 2),
            "atr_pct_20d": round(atr_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "breakout_pct": round((cur / max_20 - 1) * 100, 2) if max_20 > 0 else 0,
        }
    except Exception:
        return {}


def _scan_one_stock(sid: str) -> Optional[Dict]:
    try:
        import data_sources as ds
        df = ds.fetch_yf_history(sid, period="90d", interval="1d")
        if df is None or df.empty or len(df) < 22:
            return None
        m = _calc_consolidation_metrics(df)
        if not m:
            return None
        if m["atr_pct_20d"] > 2.5: return None
        if m["range_pct_20d"] > 12: return None
        if m["today_pct"] < 2.0: return None
        if m["breakout_pct"] < 0.5: return None  # 至少突破 0.5% 才算
        # 量增 (鬆綁: ≥ 1.5x)
        if m["vol_ratio"] < 1.5: return None

        theme_score_val = 0
        theme_tag = ""
        try:
            import theme_analyzer
            tr = theme_analyzer.theme_score(sid)
            if isinstance(tr, dict):
                #          → 原本題材 tag 永遠空、score 永遠 0, 題材加權形同失效.
                narratives = tr.get("narrative_tags") or []
                if narratives:
                    theme_tag = " / ".join(narratives[:2])
                    theme_score_val = int(tr.get("total_score", 0) or 0)
        except Exception:
            pass

        return {
            "symbol": sid,
            "market": "US",
            "current": m["current"],
            "today_pct": m["today_pct"],
            "high_20d": m["high_20d"],
            "low_20d": m["low_20d"],
            "atr_pct_20d": m["atr_pct_20d"],
            "range_pct_20d": m["range_pct_20d"],
            "vol_ratio": m["vol_ratio"],
            "breakout_pct": m["breakout_pct"],
            "theme_tag": theme_tag,
            "theme_score": theme_score_val,
            "score": m["breakout_pct"] + theme_score_val * 0.5,  # 排序用
            "tier": 1,  # 高 signal
        }
    except Exception as e:
        print(f"[breakout] {sid} scan failed: {e}", flush=True)
        return None


def check_breakout_consolidation(top_n: int = 5) -> List[Dict]:
    # Bug fix: 這支的 universe 全是美股, 但它在 monitor mode 也會被呼叫 (monitor 是 24x7,
    # 且刻意豁免假日檢查)。去重 state 用的是美東日界 (_us_today_str), 而美東換日落在
    # UTC 04:00 (夏令) / 05:00 (冬令) —— 正好在亞股 session 的 monitor 窗內, 此時美股日線
    # 還是前一交易日的, 觸發條件 (今日 ≥2%、量比 ≥1.5、突破 20 日高) 全部不變:
    #     tick UTC 00:08~03:38 → _us_today_str() 還是 D   (TPE 08:08~11:38, 去重有效)
    #     tick UTC 04:08 起     → 變成 D+1               (TPE 12:08, state 被清空)
    # → 前一晚推過的同一批會在 TPE 12:08 原封不動再推一次, 還多燒一輪 push_cap 次要額度。
    # alert_priority 的跨類去重窗只有 30 分鐘, 擋不住隔十幾小時的這一次。
    # 美股沒開盤時掃它本來也沒有意義 → 直接 gate 在美股 session 內 (us_open 10:02 ET /
    # us_mid 11:32 ET 都在 session 內, 不影響正常推播)。
    try:
        import index_alerts as _ia
        if not _ia._is_market_in_session("US"):
            print("[breakout] 美股未開盤 → 跳過 (避免日界換日造成重複推播)", flush=True)
            return []
    except Exception as _se:
        print(f"[breakout] session 判斷失敗, 照舊執行 (non-fatal): {_se}", flush=True)
    state = watchlist_store.load_monitor_state()
    bo_state = state.setdefault("breakout_consolidation_alert", {})

    today_str = _us_today_str()
    if bo_state.get("date") != today_str:
        bo_state.clear()
        bo_state.update({"date": today_str, "alerted": []})
    alerted: set = set(bo_state.get("alerted") or [])

    universe = set(DEFAULT_US_UNIVERSE_BREAKOUT)
    try:
        for s in watchlist_store.load_watchlist_ids("US"):
            universe.add(s)
    except Exception:
        pass

    alerts: List[Dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_scan_one_stock, s): s for s in universe}
        for fut in as_completed(futs):
            r = fut.result()
            if r and r["symbol"] not in alerted:
                alerts.append(r)

    # 排序 + 取 top
    alerts.sort(key=lambda x: x.get("score", 0), reverse=True)
    return alerts[:top_n]


def mark_alerts_sent(alerts: List[Dict]) -> None:
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        bo = state.setdefault("breakout_consolidation_alert", {})
        today_str = _us_today_str()  # 跟 check_breakout_consolidation 用同一個日界
        if bo.get("date") != today_str:
            bo.clear()
            bo.update({"date": today_str, "alerted": []})
        a_set = set(bo.get("alerted") or [])
        for a in alerts:
            a_set.add(a.get("symbol", ""))
        bo["alerted"] = sorted(a_set)
        state["breakout_consolidation_alert"] = bo
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[breakout] mark_sent fail: {e}", flush=True)


def unmark_alerts_sent(alerts: List[Dict]) -> None:

    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        bo = state.get("breakout_consolidation_alert") or {}
        a_set = set(bo.get("alerted") or [])
        for a in alerts:
            a_set.discard(a.get("symbol", ""))
        bo["alerted"] = sorted(a_set)
        state["breakout_consolidation_alert"] = bo
        watchlist_store.save_monitor_state(state)
    except Exception as _ue:
        print(f"[breakout] unmark_sent fail: {_ue}", flush=True)
