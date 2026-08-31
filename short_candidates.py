"""
short_candidates.py
盤中弱勢個股掃描 — 短空候選 (多空雙向, 對應 strong_stock_alert).

觸發條件 (個股):
  - 今日跌幅 ≤ -3%
  - 量比 ≥ 1.5x (放量下殺)
  - 跌破 5/10/20 MA (技術面確認弱)

掃描範圍:
  大型權值 + 熱門題材 + watchlist + holdings (跟強勢股 universe 對稱)

排序: score = abs(today_pct) × 2 + vol_ratio × 1

API:
  - scan_intraday_weak_stocks(top_n=5) -> List[Dict]
  - _fmt_intraday_weak_msg(picks) -> str
  - check_and_push_intraday_weak() -> Optional[Dict]  # 給 monitor flow 呼叫
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import data_sources as ds


WEAK_COOLDOWN_MIN = 90
WEAK_DAILY_CAP = 3
WEAK_MIN_DROP_PCT = -3.0   # 跌幅 ≤ -3% 才算弱
WEAK_MIN_VR = 1.5          # 量比 ≥ 1.5x (放量下殺)
WEAK_TOP_N = 5


def _stock_weak_metrics(stock_id: str) -> Optional[Dict]:
    """單檔: 今日%, 量比, 跌破 MA?."""
    try:
        for suffix in [".TW", ".TWO"]:
            sym = f"{stock_id}{suffix}"
            df = ds.fetch_yf_history(sym, period="60d", interval="1d")
            if df is not None and not df.empty and len(df) >= 20:
                break
        else:
            return None
        # 抓 intraday 5m 算當下價
        df_i = None
        for suffix in [".TW", ".TWO"]:
            sym = f"{stock_id}{suffix}"
            df_i = ds.fetch_yf_history(sym, period="5d", interval="5m")
            if df_i is not None and not df_i.empty:
                break
        if df_i is None or df_i.empty:
            return None
        import pandas as pd

        date_col = "Datetime" if "Datetime" in df_i.columns else df_i.columns[0]
        df_i = df_i.copy()
        df_i["_dt"] = pd.to_datetime(df_i[date_col])
        df_i["_d"] = df_i["_dt"].dt.date
        today = df_i["_d"].max()
        today_bars = df_i[df_i["_d"] == today].sort_values("_dt")
        prev_bars = df_i[df_i["_d"] < today]
        if today_bars.empty:
            return None
        current = float(today_bars["Close"].iloc[-1])
        today_vol = float(today_bars["Volume"].sum())
        prev_close = float(prev_bars["Close"].iloc[-1]) if not prev_bars.empty else current
        today_pct = (current / prev_close - 1) * 100 if prev_close > 0 else 0

        # 量比
        if not prev_bars.empty:
            prev_daily = prev_bars.groupby("_d")["Volume"].sum()
            avg_daily_vol = float(prev_daily.tail(5).mean()) if len(prev_daily) >= 1 else 0
        else:
            avg_daily_vol = 0
        vol_ratio = today_vol / avg_daily_vol if avg_daily_vol > 0 else 0

        # MA 跌破檢查
        closes = df["Close"].astype(float)
        ma5 = closes.tail(5).mean()
        ma10 = closes.tail(10).mean()
        ma20 = closes.tail(20).mean()
        broken_ma5 = current < ma5
        broken_ma10 = current < ma10
        broken_ma20 = current < ma20

        return {
            "stock_id": stock_id,
            "current": round(current, 2),
            "today_pct": round(today_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "broken_ma5": broken_ma5,
            "broken_ma10": broken_ma10,
            "broken_ma20": broken_ma20,
            "ma20": round(float(ma20), 2),
        }
    except Exception:
        return None


def scan_intraday_weak_stocks(top_n: int = WEAK_TOP_N,
                                max_drop: float = WEAK_MIN_DROP_PCT,
                                min_vr: float = WEAK_MIN_VR,
                                max_workers: int = 8) -> List[Dict]:
    """常態掃弱勢個股."""
    universe = [
        # 大型權值 (跟強勢股對稱)
        "2330", "2317", "2454", "2412", "2308", "2382", "2891", "2882", "2881",
        # AI / 半導體
        "3231", "2376", "6669", "3017", "3661", "2379", "3711", "8046",
        # 重電 / 核電
        "1513", "1519", "1503", "1504", "1514",
        # 航運 / 汽車
        "2603", "2609", "2618", "2207", "1536",
        # 太空 / 衛星 / 機器人
        "3491", "6285", "3178", "4585",
        # ABF / 載板
        "3037",
        # 其他熱門
        "8069", "6531", "1216", "2912",
        # 量子 / SMR / 光通訊
        "3450", "2331", "6271",
        # 生技 / 醫材
        "4138", "1707",
    ]
    try:
        import watchlist_store
        # 原本 universe + wl 會 TypeError (wl 是 dict 陣列) 並被下面的 except 吞掉
        wl = watchlist_store.load_watchlist_ids()
        universe = list(dict.fromkeys(universe + wl))
    except Exception:
        pass
    try:
        import holdings_store
        hd = [str(x.get("stock_id", "")).strip() for x in (holdings_store.load_holdings() or [])]
        universe = list(dict.fromkeys(universe + [h for h in hd if h]))
    except Exception:
        pass

    print(f"[intraday_weak] 掃描 {len(universe)} 檔...", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_stock_weak_metrics, sid): sid for sid in universe}
        for fut in as_completed(futures):
            m = fut.result()
            if m is None:
                continue
            tp = m.get("today_pct", 0)
            vr = m.get("vol_ratio", 0)
            if tp > max_drop or vr < min_vr:
                continue
            # score 越大越弱
            m["score"] = round(abs(tp) * 2 + vr * 1, 2)
            results.append(m)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]


def _fmt_intraday_weak_msg(picks: List[Dict]) -> str:
    """格式化弱勢股 TG (短空候選)."""
    import html as _html

    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)

    if not picks:
        return ""

    now_tpe = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%H:%M")
    lines = [
        f"📉 <b>盤中弱勢股 Top {len(picks)} (短空候選)</b> · {now_tpe} TPE",
        "<i>(跌幅 ≤ -3% + 量比 ≥ 1.5x, 放量下殺)</i>",
        "",
    ]
    name_map = {}
    try:
        info = ds.get_taiwan_stock_info()
        if info is not None and not info.empty:
            name_map = info.set_index("stock_id")["stock_name"].to_dict()
    except Exception:
        pass

    for i, p in enumerate(picks, 1):
        sid = str(p.get("stock_id", ""))
        name = _esc(name_map.get(sid, ""))
        cur = p.get("current")
        cur_s = f"{cur:.2f}" if isinstance(cur, (int, float)) else "—"  # Bug fix: 缺值套 :.2f 會崩
        tp = p.get("today_pct", 0)
        vr = p.get("vol_ratio", 0)
        # 跌破 MA 標記
        bma_tags = []
        if p.get("broken_ma5"): bma_tags.append("5MA")
        if p.get("broken_ma10"): bma_tags.append("10MA")
        if p.get("broken_ma20"): bma_tags.append("20MA")
        bma = f" · 跌破 {'/'.join(bma_tags)}" if bma_tags else ""

        lines.append(
            f"{i}. <code>{_esc(sid)}</code> {name} · "
            f"{cur_s} <b>{tp:.2f}%</b> · 量比 {vr:.2f}x{bma}"
        )
        # 短空操作建議
        try:
            cur_f = float(cur)
            short_entry = round(cur_f * 1.01, 2)  # 反彈 +1% 才空 (避免追殺)
            short_stop = round(cur_f * 1.03, 2)   # +3% 停損
            short_target = round(cur_f * 0.95, 2) # -5% 目標
            lines.append(
                f"   🔻 空單參考 {short_entry:.2f} · 停損 {short_stop:.2f} · 目標 {short_target:.2f}"
            )
        except (TypeError, ValueError):
            pass

    lines.append("")
    lines.append("<i>※ 短空候選 (3R 比). 台股需可借券標的; 美股可直接放空. 持倉避開.</i>")
    return "\n".join(lines)


def check_and_push_intraday_weak() -> Optional[Dict]:
    """常態 intraday 弱勢股推播 (短空候選).

    Cooldown 90min, 一天最多 3 次, 台股 session 內.
    """
    now_utc = dt.datetime.now(dt.timezone.utc)
    if now_utc.hour < 1 or now_utc.hour > 5:
        return None
    try:
        import holiday_check
        if holiday_check.is_market_closed_today("TW"):
            return None
    except Exception:
        pass

    state = None
    today_data = None
    try:
        import watchlist_store
        state = watchlist_store.load_monitor_state()
        iwa = state.setdefault("intraday_weak_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        today_data = iwa.setdefault(today_str, {"count": 0, "last_ts": None})
        if today_data.get("count", 0) >= WEAK_DAILY_CAP:
            return {"triggered": False, "reason": "daily_cap_reached"}
        last_ts = today_data.get("last_ts")
        if last_ts:
            try:
                last_dt = dt.datetime.fromisoformat(last_ts)
                if (now_utc - last_dt).total_seconds() < WEAK_COOLDOWN_MIN * 60:
                    return {"triggered": False, "reason": "cooldown"}
            except Exception:
                pass
    except Exception:
        pass

    picks = scan_intraday_weak_stocks()
    if not picks:
        return {"triggered": False, "reason": "no_picks"}

    # 跨類去重 — 同股 30 min 內已推同方向 (空) 不再推
    try:
        import alert_priority as _ap
        original_n = len(picks)
        picks = _ap.filter_dedup_picks(picks, "intraday_short_candidate", "down")
        if not picks:
            return {"triggered": False, "reason": "all_recently_pushed",
                    "filtered_n": original_n}
    except Exception:
        pass

    msg = _fmt_intraday_weak_msg(picks)
    # 末段加歷史績效 (空單訊號)
    try:
        import signal_tracker as _st
        perf = _st.fmt_compact_perf("intraday_weak_short", lookback_days=30)
        if perf:
            msg = msg + "\n\n" + perf
    except Exception:
        pass

    try:
        import notifier
        ok, _ = notifier.send_message(msg, disable_preview=True)
        if ok and today_data is not None:
            today_data["count"] = today_data.get("count", 0) + 1
            today_data["last_ts"] = now_utc.isoformat()
            try:
                import watchlist_store
                watchlist_store.save_monitor_state(state)
            except Exception:
                pass
            try:
                import signal_tracker as _st2
                _st2.record_batch("intraday_weak_short", picks,
                                   evaluate_after_days=5, expected_direction="down")
            except Exception as _re:
                print(f"[intraday_weak] record_batch failed: {_re}", flush=True)
            try:
                import alert_priority as _ap
                _ap.mark_picks_pushed(picks, "intraday_short_candidate", "down")
            except Exception:
                pass
        return {"triggered": True, "n_picks": len(picks), "sent": ok}
    except Exception as e:
        print(f"[intraday_weak] notifier 失敗: {e}", flush=True)
        return {"triggered": True, "n_picks": len(picks), "sent": False}
