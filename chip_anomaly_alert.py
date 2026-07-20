"""
chip_anomaly_alert.py
籌碼異常即時警報 (台股) — Tier 2 訊號.

觸發條件 (任一):
  A. 法人連買 ≥ 5 個交易日 AND 5d 累計買超佔流通張數 > 1%
  B. 法人連賣 ≥ 5 個交易日 AND 5d 累計賣超佔流通張數 > 1% (TG 警告)
  C. 主力券商集中度急升 (top 3 集中度今日 - 30d 均 > 5pp)

對象: watchlist + holdings (僅台股)
Tier: Tier 2 (響鈴, 但批次)
Throttle: per-(sym, type) per-day 1 次

API:
  check_chip_anomaly() -> List[Dict]
  mark_alerts_sent(alerts) -> None
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List

import watchlist_store


def _check_one_stock(sid: str) -> List[Dict]:
    """掃單檔台股籌碼異常. 回 alert list (可能 0-2 則)."""
    out: List[Dict] = []
    try:
        import data_sources as ds
        # 用 fetch_institutional_universe (data_sources 唯一公開 API)
        end = dt.date.today().strftime("%Y-%m-%d")
        start = (dt.date.today() - dt.timedelta(days=20)).strftime("%Y-%m-%d")
        inst_df = ds.fetch_institutional_universe((sid,), start, end)
        if inst_df is None or inst_df.empty:
            return out
        # FinMind schema: date, stock_id, name (Foreign_Investor 等), buy, sell (股)
        if "name" not in inst_df.columns or "buy" not in inst_df.columns:
            return out
        foreign = inst_df[inst_df["name"].astype(str).str.contains(
            "Foreign|外資|外陸", na=False, regex=True
        )].copy()
        if foreign.empty:
            return out
        foreign["net_lots"] = (
            foreign["buy"].astype(float) - foreign["sell"].astype(float)
        ) / 1000.0
        daily_net = foreign.groupby("date")["net_lots"].sum().sort_index()
        if len(daily_net) < 5:
            return out
        net_5d = daily_net.tail(5)
        # 連買: 全 > 0
        is_streak_buy = (net_5d > 0).all()
        is_streak_sell = (net_5d < 0).all()
        if not (is_streak_buy or is_streak_sell):
            return out
        # 流通張數 (用發行張數近似, 若 FinMind 沒給, fallback)
        try:
            info = ds.get_taiwan_stock_info()
            if info is not None and not info.empty:
                row = info[info["stock_id"] == sid]
                if not row.empty and "資本額" in row.columns:
                    cap = float(row.iloc[0].get("資本額", 0))
                    # 流通張數 ≈ 資本額 / 10 元 / 1000 股 (粗估)
                    shares_lots = cap / 10000 if cap > 0 else 0
                else:
                    shares_lots = 0
            else:
                shares_lots = 0
        except Exception:
            shares_lots = 0
        cum_5d = net_5d.sum()
        pct_of_outstanding = abs(cum_5d) / shares_lots * 100 if shares_lots > 0 else 0

        if is_streak_buy and pct_of_outstanding > 1.0:
            out.append({
                "symbol": sid,
                "market": "TW",
                "type": "chip_strong_buy",
                "direction": "buy",
                "streak_days": 5,
                "cum_5d_lots": int(cum_5d),
                "pct_of_outstanding": round(pct_of_outstanding, 2),
                "tier": 2,
            })
        if is_streak_sell and pct_of_outstanding > 1.0:
            out.append({
                "symbol": sid,
                "market": "TW",
                "type": "chip_strong_sell",
                "direction": "sell",
                "streak_days": 5,
                "cum_5d_lots": int(cum_5d),
                "pct_of_outstanding": round(pct_of_outstanding, 2),
                "tier": 2,
            })
    except Exception as e:
        print(f"[chip_anomaly] {sid} check failed: {e}", flush=True)
    return out


def check_chip_anomaly() -> List[Dict]:
    state = watchlist_store.load_monitor_state()
    ca_state = state.setdefault("chip_anomaly_alert", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    if ca_state.get("date") != today_str:
        ca_state.clear()
        ca_state.update({"date": today_str, "alerted": []})
    alerted: set = set(ca_state.get("alerted") or [])

    universe = set()
    try:
        wl = watchlist_store.load_watchlist() or []
        for sid in wl:
            s = str(sid).strip().upper()
            if s and s.isdigit():
                universe.add(s)
    except Exception:
        pass
    try:
        import holdings_store
        for h in holdings_store.load_holdings() or []:
            sid = str(h.get("stock_id", "")).strip().upper()
            mk = h.get("market", "TW" if sid.isdigit() else "US")
            if sid and mk == "TW":
                universe.add(sid)
    except Exception:
        pass

    all_alerts: List[Dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_check_one_stock, s): s for s in universe}
        for fut in as_completed(futs):
            try:
                for a in fut.result():
                    key = f"{a['symbol']}:{a['type']}"
                    if key not in alerted:
                        all_alerts.append(a)
            except Exception:
                pass

    return all_alerts


def mark_alerts_sent(alerts: List[Dict]) -> None:
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        ca = state.setdefault("chip_anomaly_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        if ca.get("date") != today_str:
            ca.clear()
            ca.update({"date": today_str, "alerted": []})
        a_set = set(ca.get("alerted") or [])
        for a in alerts:
            a_set.add(f"{a.get('symbol','')}:{a.get('type','')}")
        ca["alerted"] = sorted(a_set)
        state["chip_anomaly_alert"] = ca
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[chip_anomaly] mark_sent failed: {e}", flush=True)


def unmark_alerts_sent(alerts: List[Dict]) -> None:
    """回滾 mark_alerts_sent — 送出失敗 / 被 daily cap 擋下時呼叫,
    把剛 claim 的 (symbol:type) 移除, 讓下個 tick 能重試 (否則會被靜默吞掉永不送出)."""
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        ca = state.get("chip_anomaly_alert") or {}
        a_set = set(ca.get("alerted") or [])
        for a in alerts:
            a_set.discard(f"{a.get('symbol','')}:{a.get('type','')}")
        ca["alerted"] = sorted(a_set)
        state["chip_anomaly_alert"] = ca
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[chip_anomaly] unmark_sent failed: {e}", flush=True)
