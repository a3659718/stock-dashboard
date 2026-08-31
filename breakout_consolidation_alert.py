"""
breakout_consolidation_alert.py
盤整突破 alert (美股) — 找「盤整很久 + 開盤後第一根突破 + 熱門題材」.

觸發條件 (3 同時):
  1. 過去 20 日波動率窄: ATR/Price ≤ 2.5% (相對窄幅)
  2. 過去 20 日 high/low range 窄: (max-min)/min ≤ 12%
  3. 今日 today_pct ≥ +2% (跳出盤整)
  4. 收盤 > 20d high (突破)
  5. (可選) vol_ratio ≥ 2.0 (有量)
  6. theme hot score 高 (用 theme_analyzer 過濾)

挑 top 5 (按 breakout_pct × theme_score 排序).

API:
  check_breakout_consolidation() -> List[Dict]
  mark_alerts_sent(alerts) -> None
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import watchlist_store


def _us_today_str() -> str:
    """美東 (ET) 的今天日期字串 — 這支掃美股, 「一天」要用美東的一天算.

    見 check_breakout_consolidation() 內的 bug 說明: 用 UTC 日期會在台北早上
    (亞股 monitor tick) 把當日去重表清空, 造成前一晚推過的美股突破隔天再推一次。
    """
    now_utc = dt.datetime.utcnow()
    try:
        import index_alerts
        dst = bool(index_alerts._is_us_in_dst(now_utc.date()))
    except Exception:
        dst = 3 <= now_utc.month <= 10
    return (now_utc - dt.timedelta(hours=4 if dst else 5)).strftime("%Y-%m-%d")


# 美股 universe (大型權值 + AI/熱門題材)
DEFAULT_US_UNIVERSE_BREAKOUT = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
    "AVGO", "AMD", "PLTR", "DELL", "ORCL", "CRM", "LLY", "REGN",
    "RKLB", "ASTS", "BABA", "TSM", "ARM", "SMCI",
    "SOFI", "MARA", "RIOT", "CELH", "DUOL", "NET", "DDOG",
]


def _calc_consolidation_metrics(df) -> Dict:
    """算 20d 盤整指標 + 今日突破判斷."""
    try:
        import pandas as pd
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
        # range tight: (max-min)/min ≤ 12%
        range_pct = (max_20 - min_20) / min_20 * 100 if min_20 > 0 else 999
        # ATR/Price (TR avg / current)
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
    """掃單檔. 回 alert dict 或 None."""
    try:
        import data_sources as ds
        df = ds.fetch_yf_history(sid, period="90d", interval="1d")
        if df is None or df.empty or len(df) < 22:
            return None
        m = _calc_consolidation_metrics(df)
        if not m:
            return None
        # 觸發條件 (relaxed: ATR 2.5% / range 12% / 漲 2% / 突破)
        if m["atr_pct_20d"] > 2.5: return None
        if m["range_pct_20d"] > 12: return None
        if m["today_pct"] < 2.0: return None
        if m["breakout_pct"] < 0.5: return None  # 至少突破 0.5% 才算
        # 量增 (鬆綁: ≥ 1.5x)
        if m["vol_ratio"] < 1.5: return None

        # 熱門題材 score (用 theme_analyzer.theme_score 實際 API)
        theme_score_val = 0
        theme_tag = ""
        try:
            import theme_analyzer
            tr = theme_analyzer.theme_score(sid)
            if isinstance(tr, dict):
                # Bug fix: theme_score() 回的是 narrative_tags / total_score, 不是 narratives / score
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
    """掃 universe + watchlist, 找盤整突破 top N."""
    state = watchlist_store.load_monitor_state()
    bo_state = state.setdefault("breakout_consolidation_alert", {})
    # Bug fix (2026-08): 原本用 dt.date.today() = GitHub Actions 的 UTC 日期。
    #   這支掃的是【美股】日線 (period="90d", interval="1d"), 但 UTC 日界落在
    #   台北 08:00 —— 也就是「亞股時段的 monitor tick」(TPE 09:08~13:08) 已經算
    #   新的一天了。於是:
    #     週二 TPE 22:15 推出 NVDA 突破 → alerted 記在 UTC 週二
    #     週三 TPE 09:08 → UTC 日期翻頁 → bo_state.clear() → alerted 清空
    #     此時美股已收盤, 日線最後一根還是週二那根, 篩選條件原封不動全部成立
    #     → 同一批標的隔天早上被【原封不動重推一次】(alert_priority 的 30 分鐘窗早過期)
    #   改用美東日期: 美股一個交易日內不會翻頁, 亞股時段也不會誤觸重置。
    today_str = _us_today_str()
    if bo_state.get("date") != today_str:
        bo_state.clear()
        bo_state.update({"date": today_str, "alerted": []})
    alerted: set = set(bo_state.get("alerted") or [])

    universe = set(DEFAULT_US_UNIVERSE_BREAKOUT)
    try:
        # load_watchlist() 回 dict 陣列 → 用 load_watchlist_ids("US") 只取美股代號
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
    """回滾 mark_alerts_sent — 送出失敗 / 被 daily cap 擋時呼叫, 把剛 claim 的 symbol 移除,
    讓下個 tick 能重試 (否則被靜默吞掉永不送出)."""
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
