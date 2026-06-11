"""
holdings_tracker.py
持倉相關的追蹤功能:

1) save_predictions(holdings_results)
   每次 Gemini 跑完, 紀錄每檔的 action / next_day_up_prob / stop_loss / 當時價
   用於隔日驗證 + 停損監控

2) evaluate_pending_predictions()
   檢查昨天 (或更早) 的預測, 對照今天收盤, 算準確率

3) accuracy_summary()
   過去 30 天的準確率 (整體 + 分 action / 分機率區間)

4) update_stop_loss_state(holdings_results)
   每次 Gemini 跑完同步更新停損價到 state, 給 check_stop_loss_breaches 用

5) check_stop_loss_breaches()
   當前價跟停損價比對, 跌破就回傳 alert list (給 tw_open / tw_mid / monitor 用)

State key: monitor_state["holdings_tracker"]
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import data_sources as ds
import watchlist_store


# ---------------------------------------------------------------------------
# 1) 紀錄預測
# ---------------------------------------------------------------------------
def save_predictions(holdings_results: List[Dict]) -> int:
    """把 Gemini 預測存進 state, 給隔日驗證用. 回傳存了幾筆."""
    if not holdings_results:
        return 0
    state = watchlist_store.load_monitor_state()
    tracker = state.setdefault("holdings_tracker", {})
    preds = tracker.setdefault("predictions", {})

    today_str = dt.date.today().strftime("%Y-%m-%d")
    today_preds = preds.setdefault(today_str, {})

    saved = 0
    for h in holdings_results:
        sid = str(h.get("stock_id", ""))
        if not sid:
            continue
        tech = h.get("tech", {}) or {}
        adv = h.get("advice", {}) or {}
        today_preds[sid] = {
            "action": adv.get("action"),
            "next_day_up_prob": adv.get("next_day_up_prob", 50),
            "target_short": adv.get("target_short"),
            "target_mid": adv.get("target_mid"),
            "stop_loss": adv.get("stop_loss"),
            "confidence": adv.get("confidence"),
            "current_when_predicted": tech.get("current"),
            "predicted_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "validated": False,
        }
        saved += 1

    # 只保留最近 60 天 (避免無限長)
    cutoff = (dt.date.today() - dt.timedelta(days=60)).strftime("%Y-%m-%d")
    for d in list(preds.keys()):
        if d < cutoff:
            del preds[d]

    tracker["predictions"] = preds
    state["holdings_tracker"] = tracker
    watchlist_store.save_monitor_state(state)
    return saved


# ---------------------------------------------------------------------------
# 2) 驗證預測 (隔日)
# ---------------------------------------------------------------------------
def _fetch_close(stock_id: str, date_str: str) -> Optional[float]:
    """抓某天的收盤. 假日會 fallback 到最近交易日."""
    for suffix in [".TW", ".TWO"]:
        df = ds.fetch_yf_history(f"{stock_id}{suffix}", period="1mo", interval="1d")
        if df is None or df.empty:
            continue
        try:
            import pandas as pd
            df = df.copy()
            date_col = "Date" if "Date" in df.columns else df.columns[0]
            df["_d"] = pd.to_datetime(df[date_col]).dt.date.astype(str)
            row = df[df["_d"] == date_str]
            if not row.empty:
                return float(row["Close"].astype(float).iloc[-1])
            # 該日無資料 (假日), 找前一個 close
            df = df[df["_d"] <= date_str]
            if not df.empty:
                return float(df["Close"].astype(float).iloc[-1])
        except Exception:
            continue
    return None


def evaluate_pending_predictions() -> int:
    """檢查歷史預測, 對照實際走勢算對錯. 回傳新驗證的筆數."""
    state = watchlist_store.load_monitor_state()
    tracker = state.setdefault("holdings_tracker", {})
    preds = tracker.setdefault("predictions", {})

    today = dt.date.today()
    today_str = today.strftime("%Y-%m-%d")
    validated = 0

    for date_str, day_preds in list(preds.items()):
        try:
            pred_date = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        # 預測日 + 1 天才能驗證 (要等隔日收盤)
        if (today - pred_date).days < 1:
            continue
        # 隔日的下個交易日 (簡單版: 預測日 +1 ~ +5 找最近的 close)
        for delta in range(1, 6):
            next_date = pred_date + dt.timedelta(days=delta)
            if next_date.weekday() >= 5:
                continue
            next_date_str = next_date.strftime("%Y-%m-%d")
            if next_date_str > today_str:
                break
            # 對每檔還沒驗證的, 抓 next_date close 比對
            for sid, p in day_preds.items():
                if p.get("validated"):
                    continue
                cur_when_pred = p.get("current_when_predicted")
                if not cur_when_pred:
                    continue
                next_close = _fetch_close(sid, next_date_str)
                if next_close is None:
                    continue
                actual_pct = (next_close / cur_when_pred - 1) * 100 if cur_when_pred > 0 else 0
                actual_up = actual_pct > 0
                predicted_up = (p.get("next_day_up_prob", 50) > 50)
                p["actual_close"] = round(next_close, 2)
                p["actual_pct"] = round(actual_pct, 2)
                p["actual_up"] = actual_up
                p["correct_direction"] = (actual_up == predicted_up)
                p["validated"] = True
                p["validated_date"] = next_date_str
                validated += 1
            break  # 只跑第一個交易日

    tracker["predictions"] = preds
    state["holdings_tracker"] = tracker
    watchlist_store.save_monitor_state(state)
    return validated


# ---------------------------------------------------------------------------
# 3) 準確率摘要
# ---------------------------------------------------------------------------
def accuracy_summary(lookback_days: int = 30) -> Dict:
    """過去 N 天的方向預測準確率."""
    state = watchlist_store.load_monitor_state()
    tracker = state.setdefault("holdings_tracker", {})
    preds = tracker.get("predictions", {})

    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    total = 0
    correct = 0
    by_action: Dict[str, Dict[str, int]] = {}
    by_prob: Dict[str, Dict[str, int]] = {
        ">=70%": {"total": 0, "correct": 0},
        "50-70%": {"total": 0, "correct": 0},
        "<50%": {"total": 0, "correct": 0},
    }

    for date_str, day_preds in preds.items():
        if date_str < cutoff:
            continue
        for sid, p in day_preds.items():
            if not p.get("validated"):
                continue
            total += 1
            ok = bool(p.get("correct_direction"))
            if ok:
                correct += 1
            act = p.get("action", "—")
            by_action.setdefault(act, {"total": 0, "correct": 0})
            by_action[act]["total"] += 1
            if ok:
                by_action[act]["correct"] += 1
            prob = p.get("next_day_up_prob", 50)
            if prob >= 70:
                bucket = ">=70%"
            elif prob >= 50:
                bucket = "50-70%"
            else:
                bucket = "<50%"
            by_prob[bucket]["total"] += 1
            if ok:
                by_prob[bucket]["correct"] += 1

    return {
        "total": total,
        "correct": correct,
        "accuracy_pct": round(correct / total * 100, 1) if total else 0,
        "by_action": by_action,
        "by_prob_range": by_prob,
        "lookback_days": lookback_days,
    }


# ---------------------------------------------------------------------------
# 4) 同步停損價
# ---------------------------------------------------------------------------
def update_stop_loss_state(holdings_results: List[Dict]) -> int:
    """從 Gemini 結果同步每檔的停損價到 state, 給 check_stop_loss_breaches 用."""
    if not holdings_results:
        return 0
    state = watchlist_store.load_monitor_state()
    tracker = state.setdefault("holdings_tracker", {})
    sl_state = tracker.setdefault("stop_loss", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    n = 0
    for h in holdings_results:
        sid = str(h.get("stock_id", ""))
        if not sid:
            continue
        adv = h.get("advice", {}) or {}
        sl = adv.get("stop_loss")
        if sl and float(sl) > 0:
            existing = sl_state.get(sid, {})
            sl_state[sid] = {
                "stop_loss": float(sl),
                "name": h.get("name", ""),
                "set_date": today_str,
                # 保留 fired 紀錄, 避免 Gemini 重設停損後同日再推一次
                "fired_date": existing.get("fired_date", ""),
                "near_stop_fired_date": existing.get("near_stop_fired_date", ""),
            }
            n += 1
    tracker["stop_loss"] = sl_state
    state["holdings_tracker"] = tracker
    watchlist_store.save_monitor_state(state)
    return n


# ---------------------------------------------------------------------------
# 5) 檢查停損是否跌破
# ---------------------------------------------------------------------------
def check_stop_loss_breaches() -> List[Dict]:
    """掃所有持倉, 抓即時價, 跌破停損就回傳 alert.
    一天最多 1 次 per 股票 (用 fired_date 去重).

    回傳 alert dict 多帶 "alert_type":
      - "breach" — 已跌破停損
      - "near_stop" — 距停損 ≤ 2% (預警, 還沒破)
    """
    try:
        import holdings_store
    except ImportError:
        return []
    holdings = holdings_store.load_holdings()
    if not holdings:
        return []

    state = watchlist_store.load_monitor_state()
    tracker = state.setdefault("holdings_tracker", {})
    sl_state = tracker.setdefault("stop_loss", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")

    alerts: List[Dict] = []
    for h in holdings:
        sid = str(h.get("stock_id", ""))
        if not sid:
            continue
        sl_info = sl_state.get(sid)
        if not sl_info or not sl_info.get("stop_loss"):
            continue  # 還沒設過停損 (Gemini 沒跑過)
        stop_loss = float(sl_info["stop_loss"])
        # fired_date 對 breach 是「真的破」, near_stop_fired_date 是「預警」
        fired_breach = sl_info.get("fired_date") == today_str
        fired_near = sl_info.get("near_stop_fired_date") == today_str

        # 抓即時價 (5m 線最後一根, fallback daily)
        cur = None
        for suffix in [".TW", ".TWO"]:
            df = ds.fetch_yf_history(f"{sid}{suffix}", period="2d", interval="5m")
            if df is not None and not df.empty:
                try:
                    cur = float(df["Close"].astype(float).iloc[-1])
                    break
                except Exception:
                    continue
        if cur is None:
            for suffix in [".TW", ".TWO"]:
                df = ds.fetch_yf_history(f"{sid}{suffix}", period="2d", interval="1d")
                if df is not None and not df.empty:
                    try:
                        cur = float(df["Close"].astype(float).iloc[-1])
                        break
                    except Exception:
                        continue
        if cur is None:
            continue

        if cur <= stop_loss:
            if fired_breach:
                continue
            breach_pct = (cur / stop_loss - 1) * 100 if stop_loss > 0 else 0
            alerts.append({
                "stock_id": sid,
                "name": h.get("name", "") or sl_info.get("name", ""),
                "current": round(cur, 2),
                "stop_loss": round(stop_loss, 2),
                "breach_pct": round(breach_pct, 2),
                "set_date": sl_info.get("set_date", ""),
                "alert_type": "breach",
            })
            sl_info["fired_date"] = today_str
        else:
            # 預警: 距停損 ≤ 2% (還沒破, 但很接近)
            distance_pct = (cur / stop_loss - 1) * 100 if stop_loss > 0 else 999
            if 0 < distance_pct <= 2 and not fired_near and not fired_breach:
                alerts.append({
                    "stock_id": sid,
                    "name": h.get("name", "") or sl_info.get("name", ""),
                    "current": round(cur, 2),
                    "stop_loss": round(stop_loss, 2),
                    "distance_pct": round(distance_pct, 2),
                    "breach_pct": 0,  # 還沒破
                    "set_date": sl_info.get("set_date", ""),
                    "alert_type": "near_stop",
                })
                sl_info["near_stop_fired_date"] = today_str

    tracker["stop_loss"] = sl_state
    state["holdings_tracker"] = tracker
    watchlist_store.save_monitor_state(state)
    return alerts
