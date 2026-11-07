

from __future__ import annotations

import datetime as dt
import uuid
from typing import Dict, List, Optional

from radar.data import data_sources as ds


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
        from radar.core import watchlist_store
        return watchlist_store.load_monitor_state()
    except Exception:
        return {}


def _save_state(state: Dict) -> None:
    try:
        from radar.core import watchlist_store
        # immediate: 觸發的 claim / 回滾要馬上寫入, 否則 monitor 的 batch 模式要等跑完才寫,
        # 同時間的 tw_close / us_close 會看到同一個觸發還上膛而重複推播。
        watchlist_store.save_monitor_state(state, immediate=True)
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
    now_utc = dt.datetime.now(dt.timezone.utc)
    if market == "US":
        try:
            from radar.alerts import index_alerts
            dst = index_alerts._is_us_in_dst(now_utc.date())
        except Exception:
            dst = True
        # 16:00 ET 收盤 + 15 分緩衝 (收盤後幾分鐘的日 K 可能還沒定案)
        return (now_utc.hour, now_utc.minute) >= ((20, 15) if dst else (21, 15))
    # TW 收盤 13:30 TPE = 05:30 UTC; 盤後撮合結束 + 5 分鐘緩衝
    # 13:30 收盤; Yahoo 台股報價約延遲 20 分 → 13:55 前拿到的日 K 收盤其實是 13:3x 前的價,
    # 收盤集合競價翻轉的交叉會誤觸發 (一次性觸發無法事後更正)。13:55 TPE 起才判斷。
    return (now_utc.hour, now_utc.minute) >= (5, 55)


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
            # 6mo: 3mo 只有 ~58-64 根台股 K 棒, MA60 的「昨日值」需要 61 根 → 60 日均線觸發條件
            # 在農曆年前後 / 新上市股會靜默永遠不成立。
            df = ds.fetch_yf_history(stock_id, period="6mo", interval="1d")
        else:
            df = None
            for suffix in [".TW", ".TWO"]:
                df = ds.fetch_yf_history(f"{stock_id}{suffix}", period="6mo", interval="1d")
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
    """掃所有 armed 的 trigger, 回觸發的清單.

    Bug fix: 原本這裡會在偵測到觸發的當下就直接把 armed 設 False + fired_at 寫回
    state — 但這些是 one-shot 觸發 (使用者設「跌破 1000 通知我」, 通知過一次就該
    解除), 一旦這裡寫回去, 不管呼叫端後續有沒有真的把訊息送出去 (實測發現送出
    這段目前是靠 scripts/market_open_alert.py 把結果暫存進
    monitor_state["watchlist_triggers_today"], 等 15:00 彙總時才真正組訊息推播),
    這個 trigger 都已經「用完了」— 如果彙總那步失敗 (例如程式改版/暫時性錯誤),
    使用者的警報就會不聲不響永遠消失, 且無法重跑補救。
    改成 check_triggers() 只負責「找出符合條件的」, 不動 armed/fired_at; 呼叫端
    確認已經把結果妥善保存 (至少寫進待彙總的 state) 之後, 再呼叫
    mark_triggers_fired() 才真正解除警戒, 避免半途出錯就把使用者的警報吃掉。
    """
    state = _load_state()
    triggers: Dict = state.get(_STATE_KEY, {}) or {}
    if not triggers:
        return []
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date().strftime("%Y-%m-%d")

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
        # Bug fix: 同股同時有 price_* 與 cross 觸發時, 原本整組用 require_closed=True →
        #          price_* 也被迫等收盤才判, 盤中收不到即時價格警報. 改成 price 用盤中資料、
        #          cross 用收盤資料各取所需, 兩類各自在正確時機觸發.
        types_in_group = [t.get("type", "") for _, t in entries]
        has_price = any(tp.startswith("price_") for tp in types_in_group)
        has_cross = any(not tp.startswith("price_") for tp in types_in_group)
        intraday_state = _fetch_kd_macd_state(sid, market, require_closed=False) if has_price else None
        closed_state = _fetch_kd_macd_state(sid, market, require_closed=True) if has_cross else None
        for tid, t in entries:
            t_type = t.get("type", "")
            is_cross = (t_type.startswith("kd_") or t_type.startswith("macd_")
                        or t_type.startswith("ma_cross"))
            m_state = closed_state if is_cross else intraday_state
            if not m_state:
                continue
            msg = _evaluate_trigger(t, m_state)
            if msg:
                fired.append({
                    "id": tid,
                    "stock_id": sid,
                    "name": t.get("name", ""),
                    "type": t.get("type", ""),
                    "type_label": TRIGGER_TYPES.get(t.get("type", ""), t.get("type", "")),
                    "msg": msg,
                    "current": m_state.get("close_today"),
                    # Bug fix: 原本沒帶原始門檻值出去, 下游 (scripts/market_open_alert.py
                    # 累積進 watchlist_triggers_today, tw_post_market_summary.py 讀出來
                    # 顯示) 因此永遠顯示「條件 None」— 這裡補上, 讓 15:00 彙總能顯示
                    # 完整的「現價 X / 條件 Y」。
                    "value": t.get("value"),
                    "fired_at": today,
                })

    return fired


def mark_triggers_fired(fired: List[Dict]) -> Dict[str, Dict]:
    """把 check_triggers() 回傳、且已經確認妥善保存 (至少寫進待彙總 state) 的
    trigger 標記 fired_at + armed=False (解除警戒, one-shot 用掉)。

    呼叫端只有在確定這批 fired 已經被安全記下來 (寫進 monitor_state 給後續彙總
    推播用, 或已經直接送出成功) 之後才該呼叫這個函式 — 若中途保存/推播失敗,
    不呼叫這個函式即可, 下次 check_triggers() 還會再抓到同一批, 不會漏推。
    """
    prev: Dict[str, Dict] = {}
    if not fired:
        return prev
    try:
        state = _load_state()
        triggers: Dict = state.get(_STATE_KEY, {}) or {}
        changed = False
        for f in fired:
            tid = f.get("id")
            t = triggers.get(tid)
            if not t:
                continue
            prev[tid] = {"armed": t.get("armed"), "fired_at": t.get("fired_at")}
            t["fired_at"] = f.get("fired_at") or (dt.datetime.utcnow() + dt.timedelta(hours=8)).date().strftime("%Y-%m-%d")
            t["armed"] = False
            changed = True
        if changed:
            state[_STATE_KEY] = triggers
            _save_state(state)
    except Exception as e:
        print(f"[watchlist_triggers] mark_triggers_fired failed (non-fatal, will retry next check): {e}", flush=True)
    return prev


def unmark_triggers_fired(fired: List[Dict], prev: Optional[Dict[str, Dict]] = None) -> None:
    """送出失敗時回滾 mark_triggers_fired: 恢復 armed / fired_at, 下次 check 還會再觸發。"""
    if not fired:
        return
    try:
        state = _load_state()
        triggers: Dict = state.get(_STATE_KEY, {}) or {}
        for f in fired:
            tid = f.get("id")
            t = triggers.get(tid)
            if not t:
                continue
            old = (prev or {}).get(tid) or {"armed": True, "fired_at": None}
            t["armed"] = old.get("armed", True)
            t["fired_at"] = old.get("fired_at")
        state[_STATE_KEY] = triggers
        _save_state(state)
    except Exception as e:
        print(f"[watchlist_triggers] unmark_triggers_fired failed: {e}", flush=True)


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
