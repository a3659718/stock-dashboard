"""
watchlist_triggers.py
自選股「條件觸發」 — 跟 watchlist_alerts.py 的 ±5/10% 門檻互補.

支援的觸發條件:
  1. price_above / price_below: 現價穿越某指定價位
  2. ma_cross_up / ma_cross_down: 站上 / 跌破 N 日均線 (N = 5, 20, 60)
  3. kd_golden_cross / kd_death_cross: KD K 線穿越 D 線
  4. macd_golden_cross / macd_death_cross: MACD hist 由負轉正 / 由正轉負

State 結構 (watchlist_store.monitor_state["watchlist_triggers"]):
{
    "trigger_id_uuid": {
        "stock_id": "2330",
        "name": "台積電",
        "market": "TW",
        "type": "price_below",
        "value": 1000.0,        # 對 price_*: 目標價; 對 ma_*: MA 期數; 對 kd_*: 不用; 對 macd_*: 不用
        "fired_at": "2026-05-09",  # 已觸發日期 (避免重複推)
        "armed": True,
        "created_at": "2026-05-01",
    }
}

對外接口:
    add_trigger(stock_id, type, value, name, market) -> trigger_id
    list_triggers(stock_id?) -> List[Dict]
    remove_trigger(trigger_id) -> bool
    check_triggers() -> List[Dict]  # 給 monitor cron 呼叫, 回觸發的條件 list
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Dict, List, Optional

import data_sources as ds


_STATE_KEY = "watchlist_triggers"


# 支援的觸發類型 (給 dashboard select 用)
TRIGGER_TYPES = {
    "price_above":      "現價站上 X",
    "price_below":      "現價跌破 X",
    "ma_cross_up_5":    "站上 5 日均線",
    "ma_cross_up_20":   "站上 20 日均線 (月線)",
    "ma_cross_up_60":   "站上 60 日均線 (季線)",
    "ma_cross_down_5":  "跌破 5 日均線",
    "ma_cross_down_20": "跌破 20 日均線 (月線)",
    "ma_cross_down_60": "跌破 60 日均線 (季線)",
    "kd_golden_cross":  "KD 黃金交叉",
    "kd_death_cross":   "KD 死亡交叉",
    "macd_golden_cross": "MACD 由負轉正",
    "macd_death_cross":  "MACD 由正轉負",
}


def _load_state() -> Dict:
    try:
        import watchlist_store
        return watchlist_store.load_monitor_state()
    except Exception:
        return {}


def _save_state(state: Dict) -> None:
    try:
        import watchlist_store
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[watchlist_triggers] save failed: {e}", flush=True)


def add_trigger(stock_id: str, trigger_type: str, value: Optional[float] = None,
                  name: str = "", market: str = "TW") -> Optional[str]:
    """新增條件觸發, 回 trigger_id (失敗回 None)."""
    if not stock_id or trigger_type not in TRIGGER_TYPES:
        return None
    if trigger_type.startswith("price_") and value is None:
        return None
    state = _load_state()
    triggers: Dict = state.setdefault(_STATE_KEY, {})

    tid = str(uuid.uuid4())[:10]
    triggers[tid] = {
        "stock_id": str(stock_id),
        "name": str(name or ""),
        "market": str(market or "TW").upper(),
        "type": trigger_type,
        "value": float(value) if value is not None else None,
        "fired_at": None,
        "armed": True,
        "created_at": dt.date.today().strftime("%Y-%m-%d"),
    }
    state[_STATE_KEY] = triggers
    _save_state(state)
    return tid


def list_triggers(stock_id: Optional[str] = None) -> List[Dict]:
    """列出所有 (或單一股票的) 觸發條件."""
    state = _load_state()
    triggers: Dict = state.get(_STATE_KEY, {}) or {}
    out = []
    for tid, t in triggers.items():
        if stock_id and str(t.get("stock_id", "")) != str(stock_id):
            continue
        out.append({**t, "id": tid})
    return out


def remove_trigger(trigger_id: str) -> bool:
    state = _load_state()
    triggers: Dict = state.get(_STATE_KEY, {}) or {}
    if trigger_id in triggers:
        del triggers[trigger_id]
        state[_STATE_KEY] = triggers
        _save_state(state)
        return True
    return False


def _is_market_closed_now(market: str = "TW") -> bool:
    """該市場是否已收盤 (盤中 daily K 是 partial-bar, 不該拿來算 KD/MACD cross)."""
    now_utc = dt.datetime.now(timezone.utc)
    if market == "US":
        try:
            import index_alerts
            dst = index_alerts._is_us_in_dst(now_utc.date())
        except Exception:
            dst = True
        return now_utc.hour >= (21 if dst else 22)
    return now_utc.hour >= 6  # TW 收盤 13:30 TPE = 05:30 UTC, +30 min buffer


def _fetch_kd_macd_state(stock_id: str, market: str = "TW",
                          require_closed: bool = True) -> Optional[Dict]:
    """抓最近 60 日 daily K, 算 MA / KD / MACD, 回最後 2 日狀態以判 cross.

    重要: KD/MACD cross 教科書是收盤計算. 盤中跑會拿到「今日 partial day」當 close.iloc[-1],
    cross 判斷可能誤觸發 (盤中 K > D, 收盤又跌回去).

    require_closed=True (default): 該市場未收盤就回 None.
    """
    if require_closed and not _is_market_closed_now(market):
        return None
    try:
        if market == "US":
            df = ds.fetch_yf_history(stock_id, period="3mo", interval="1d")
        else:
            df = None
            for suffix in [".TW", ".TWO"]:
                df = ds.fetch_yf_history(f"{stock_id}{suffix}", period="3mo", interval="1d")
                if not df.empty:
                    break
        if df is None or df.empty or len(df) < 30:
            return None
        import pandas as pd
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)

        # KD (RSV → K → D)
        n = 9
        rsv = ((close - low.rolling(n).min()) /
               (high.rolling(n).max() - low.rolling(n).min()) * 100)
        rsv = rsv.fillna(50)
        k_series = rsv.ewm(alpha=1/3, adjust=False).mean()
        d_series = k_series.ewm(alpha=1/3, adjust=False).mean()

        # MACD
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        hist = macd_line - signal_line

        # MAs
        ma5 = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean() if len(close) >= 60 else None

        return {
            "close_today": float(close.iloc[-1]),
            "close_yesterday": float(close.iloc[-2]),
            "k_today": float(k_series.iloc[-1]),
            "k_yesterday": float(k_series.iloc[-2]),
            "d_today": float(d_series.iloc[-1]),
            "d_yesterday": float(d_series.iloc[-2]),
            "macd_hist_today": float(hist.iloc[-1]),
            "macd_hist_yesterday": float(hist.iloc[-2]),
            "ma5_today": float(ma5.iloc[-1]) if not ma5.iloc[-1] != ma5.iloc[-1] else None,
            "ma5_yesterday": float(ma5.iloc[-2]) if not ma5.iloc[-2] != ma5.iloc[-2] else None,
            "ma20_today": float(ma20.iloc[-1]) if not ma20.iloc[-1] != ma20.iloc[-1] else None,
            "ma20_yesterday": float(ma20.iloc[-2]) if not ma20.iloc[-2] != ma20.iloc[-2] else None,
            "ma60_today": float(ma60.iloc[-1]) if (ma60 is not None and len(ma60) and ma60.iloc[-1] == ma60.iloc[-1]) else None,
            "ma60_yesterday": float(ma60.iloc[-2]) if (ma60 is not None and len(ma60) and ma60.iloc[-2] == ma60.iloc[-2]) else None,
        }
    except Exception as e:
        print(f"[watchlist_triggers] _fetch_kd_macd_state {stock_id} failed: {e}", flush=True)
        return None


def _evaluate_trigger(trigger: Dict, market_state: Dict) -> Optional[str]:
    """判斷 trigger 是否觸發. 回 None = 不觸發, 回字串 = 觸發訊息."""
    t_type = trigger.get("type", "")
    val = trigger.get("value")
    cur = market_state.get("close_today")
    if cur is None:
        return None

    # 1. price_*
    if t_type == "price_above":
        if val is not None and cur >= val and market_state.get("close_yesterday", cur) < val:
            return f"現價 {cur} 站上目標價 {val}"
    if t_type == "price_below":
        if val is not None and cur <= val and market_state.get("close_yesterday", cur) > val:
            return f"現價 {cur} 跌破目標價 {val}"

    # 2. ma_*  cross_up = 昨天在下、今天在上
    if t_type.startswith("ma_cross_"):
        is_up = "_up_" in t_type
        n = t_type.split("_")[-1]
        ma_today = market_state.get(f"ma{n}_today")
        ma_yest = market_state.get(f"ma{n}_yesterday")
        c_y = market_state.get("close_yesterday")
        if not (ma_today and ma_yest and c_y):
            return None
        if is_up and c_y < ma_yest and cur > ma_today:
            return f"站上 {n} 日均線 ({cur} > MA{n} {ma_today:.1f})"
        if (not is_up) and c_y > ma_yest and cur < ma_today:
            return f"跌破 {n} 日均線 ({cur} < MA{n} {ma_today:.1f})"

    # 3. KD cross
    if t_type == "kd_golden_cross":
        ky, dy = market_state.get("k_yesterday"), market_state.get("d_yesterday")
        kt, dt_ = market_state.get("k_today"), market_state.get("d_today")
        if all(v is not None for v in (ky, dy, kt, dt_)) and ky < dy and kt > dt_:
            return f"KD 黃金交叉 (K={kt:.1f} 上穿 D={dt_:.1f})"
    if t_type == "kd_death_cross":
        ky, dy = market_state.get("k_yesterday"), market_state.get("d_yesterday")
        kt, dt_ = market_state.get("k_today"), market_state.get("d_today")
        if all(v is not None for v in (ky, dy, kt, dt_)) and ky > dy and kt < dt_:
            return f"KD 死亡交叉 (K={kt:.1f} 下穿 D={dt_:.1f})"

    # 4. MACD hist 翻號
    if t_type == "macd_golden_cross":
        hy, ht = market_state.get("macd_hist_yesterday"), market_state.get("macd_hist_today")
        if hy is not None and ht is not None and hy < 0 < ht:
            return f"MACD 由負轉正 (hist {hy:.3f}→{ht:.3f})"
    if t_type == "macd_death_cross":
        hy, ht = market_state.get("macd_hist_yesterday"), market_state.get("macd_hist_today")
        if hy is not None and ht is not None and hy > 0 > ht:
            return f"MACD 由正轉負 (hist {hy:.3f}→{ht:.3f})"

    return None


def check_triggers() -> List[Dict]:
    """掃所有 armed 的 trigger, 回觸發的清單. 同時把已觸發的 mark fired_at + disarm."""
    state = _load_state()
    triggers: Dict = state.get(_STATE_KEY, {}) or {}
    if not triggers:
        return []
    today = dt.date.today().strftime("%Y-%m-%d")

    # group by stock_id 一次抓資料
    by_stock: Dict[str, List] = {}
    for tid, t in triggers.items():
        if not t.get("armed"):
            continue
        # 同一日已觸發過 → skip
        if t.get("fired_at") == today:
            continue
        sid = str(t.get("stock_id", ""))
        by_stock.setdefault(sid, []).append((tid, t))

    fired: List[Dict] = []
    for sid, entries in by_stock.items():
        # market 取第一個 trigger 的 market (同 stock 應該都一樣)
        market = entries[0][1].get("market", "TW")
        # price_above / price_below 不需要等收盤 (盤中即時觸發是合理的)
        # KD/MACD/MA cross 必須等收盤 (盤中 partial-bar 會誤觸)
        types_in_group = [t.get("type", "") for _, t in entries]
        only_price_triggers = all(tp.startswith("price_") for tp in types_in_group)
        m_state = _fetch_kd_macd_state(sid, market,
                                          require_closed=not only_price_triggers)
        if not m_state:
            continue
        for tid, t in entries:
            t_type = t.get("type", "")
            # 對 cross 類, 若是 require_closed=False 拿到的盤中資料, 跳過 cross 判斷
            is_cross = (t_type.startswith("kd_") or t_type.startswith("macd_")
                        or t_type.startswith("ma_cross"))
            if is_cross and only_price_triggers:
                continue  # 防呆 (不會發生)
            msg = _evaluate_trigger(t, m_state)
            if msg:
                t["fired_at"] = today
                t["armed"] = False
                fired.append({
                    "id": tid,
                    "stock_id": sid,
                    "name": t.get("name", ""),
                    "type": t.get("type", ""),
                    "type_label": TRIGGER_TYPES.get(t.get("type", ""), t.get("type", "")),
                    "msg": msg,
                    "current": m_state.get("close_today"),
                })

    if fired:
        state[_STATE_KEY] = triggers
        _save_state(state)
    return fired


def fmt_trigger_alerts(fired: List[Dict]) -> str:
    """格式化觸發訊息 (給推播用)."""
    if not fired:
        return ""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)
    lines = ["<b>📌 條件觸發警報</b>", ""]
    for f in fired:
        sid = _esc(f.get("stock_id", ""))
        nm = _esc(f.get("name", ""))
        cur = _esc(f.get("current", ""))
        type_label = _esc(f.get("type_label", ""))
        msg = _esc(f.get("msg", ""))
        lines.append(f"<b><code>{sid}</code></b> {nm} {cur} — <b>{type_label}</b>")
        lines.append(f"  {msg}")
    return "\n".join(lines)
