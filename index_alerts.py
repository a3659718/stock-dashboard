"""
index_alerts.py
大盤點數增量警報 + 加密貨幣劇烈波動警報。

【亞股】當日漲跌每超過閾值就跳通知 (亞股交易時段內監控):
  日經 225 (^N225)    每 ±150 點
  韓國 KOSPI (^KS11)  每 ±50 點
  台股加權 (^TWII)    每 ±100 點

【美股】當日漲跌每超過閾值就跳通知 (美股交易時段內監控):
  費城半導體 (^SOX)   每 ±100 點 (~1.7%, 台股 leading indicator)
  那斯達克 (^IXIC)    每 ±200 點 (~1%)

【加密貨幣】當日 ±2.5% 就跳通知 (24/7):
  BTC-USD
  ETH-USD

連續同方向跳 N 次 → 視為強勢趨勢，加上「持續 X 連跌/連漲」警示文字。

State: monitor_state["index_alerts"], monitor_state["crypto_alerts"]
       每天的開盤價當 base, 收盤後重置.

安全機制 (per-market 交易時段判斷):
  - TW: 09:00-13:30 TPE  = 01:00-05:30 UTC
  - JP: 09:00-15:00 JST  = 00:00-06:00 UTC
  - KR: 09:00-15:30 KST  = 00:00-06:30 UTC
  - US: 09:30-16:00 ET   = 13:30-20:00 UTC (EDT) / 14:30-21:00 UTC (EST)
  非交易時段 → 跳過, 避免抓收盤後 stale data 觸發假警報。
  亞股 + 美股完全不重疊 (06:30-13:30 UTC 為空窗, 此時段只有加密貨幣監控)。
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import data_sources as ds
import watchlist_store


# 配置
INDEX_CONFIG = {
    # 亞股
    "^N225":  {"name": "日經 225",     "threshold": 150.0, "country": "JP"},
    "^KS11":  {"name": "韓國 KOSPI",   "threshold": 50.0,  "country": "KR"},
    "^TWII":  {"name": "台灣加權",     "threshold": 100.0, "country": "TW"},
    # 美股 (台股 leading indicator)
    "^SOX":   {"name": "費城半導體",   "threshold": 100.0, "country": "US"},
    "^IXIC":  {"name": "那斯達克",     "threshold": 200.0, "country": "US"},
}


# ---------------------------------------------------------------------------
# 各市場交易時段判斷 (避免閉市時抓 stale data 觸發假警報)
# ---------------------------------------------------------------------------
def _is_us_in_dst(today: Optional[dt.date] = None) -> bool:
    """美國 DST: 3 月第 2 週日 ~ 11 月第 1 週日."""
    today = today or dt.date.today()
    year = today.year
    # 3 月第 2 週日
    mar1 = dt.date(year, 3, 1)
    days_to_first_sun = (6 - mar1.weekday()) % 7  # weekday: Mon=0..Sun=6
    dst_start = mar1 + dt.timedelta(days=days_to_first_sun + 7)
    # 11 月第 1 週日
    nov1 = dt.date(year, 11, 1)
    days_to_first_sun = (6 - nov1.weekday()) % 7
    dst_end = nov1 + dt.timedelta(days=days_to_first_sun)
    return dst_start <= today < dst_end


def _is_market_in_session(country: str) -> bool:
    """各市場是否在自己交易時段內.
    TW: 09:00-13:30 TPE  = 01:00-05:30 UTC
    JP: 09:00-15:00 JST  = 00:00-06:00 UTC
    KR: 09:00-15:30 KST  = 00:00-06:30 UTC
    US: 09:30-16:00 ET   = 13:30-20:00 UTC (EDT) / 14:30-21:00 UTC (EST)
    週末一律不在 session.
    """
    now_utc = dt.datetime.utcnow()
    cur = now_utc.hour + now_utc.minute / 60.0
    c = country.upper()
    if c == "US":
        # 美股 weekend 邊界要用「美東日期」, 不能用 UTC 日期 —
        # 例: UTC Sat 02:00 = ET Fri 22:00 (理論上盤後), 美股還是「週五」, 不算週末.
        # 但實務上美股 16:00 ET (= 20:00/21:00 UTC) 就收盤, 之後不在 session, 所以
        # 加上 weekday 判斷 (對 ET 日期判) 就足夠安全.
        # DST 切換邊界: 11/3、3 月第二週日, 在 UTC 跨 ET-day 那 4-5 小時可能誤判 1 hr,
        # 用 ET-date 算 DST 才完全乾淨.
        # 估算 ET 偏移 (DST 期 -4h, 否則 -5h). 先用 UTC 日期粗估 DST, 再以該偏移
        # 反推 ET 日期, 用 ET 日期重算 DST (二次校正 — 邊界處才會生效).
        is_dst = _is_us_in_dst(now_utc.date())
        et_offset_hr = -4 if is_dst else -5
        et_now = now_utc + dt.timedelta(hours=et_offset_hr)
        if et_now.weekday() >= 5:  # 美東週末
            return False
        is_dst = _is_us_in_dst(et_now.date())
        # 切換完 DST 後, et_offset 也要重算 (極少數 case 跨日)
        et_offset_hr = -4 if is_dst else -5
        et_now = now_utc + dt.timedelta(hours=et_offset_hr)
        # 09:30 ~ 16:00 ET
        et_cur = et_now.hour + et_now.minute / 60.0
        return 9.5 <= et_cur < 16.0
    # 亞股 — UTC weekend 一律不算 (TW/JP/KR 時區只比 UTC 早 8-9 hr, 週末邊界不會誤判)
    if now_utc.weekday() >= 5:
        return False
    if c == "TW":
        return 1.0 <= cur < 5.5
    elif c == "JP":
        return 0.0 <= cur < 6.0
    elif c == "KR":
        return 0.0 <= cur < 6.5
    return False


def _is_us_trading_hours() -> bool:
    """美股是否在交易時段 — 保留 backward-compat alias."""
    return _is_market_in_session("US")


def get_active_session() -> str:
    """回傳目前 active session: 'asia' / 'us' / 'gap' (空窗期, 只剩加密貨幣)."""
    if any(_is_market_in_session(c) for c in ["TW", "JP", "KR"]):
        return "asia"
    if _is_market_in_session("US"):
        return "us"
    return "gap"


CRYPTO_CONFIG = {
    "BTC-USD": {"name": "BTC",  "threshold_pct": 2.5},
    "ETH-USD": {"name": "ETH",  "threshold_pct": 2.5},
}


# ---------------------------------------------------------------------------
# 系統性大跌警報配置 (繞過 ATR / cooldown / daily-cap throttle)
# ---------------------------------------------------------------------------
# 觸發條件 (任一):
#   A) 盤中跌幅 vs 今日開盤 <= INTRADAY_PCT  (預設 -3%)
#   B) 連續 2 日累計跌幅 (今日 close vs 前日 close 再串接) <= TWO_DAY_CUM_PCT (預設 -4%)
# 監控標的: 台股加權, 費城半導體 (台股先行指標), S&P500, 台積電 ADR
#
# Throttle (避免一天狂推):
#   - 同 symbol 兩次警報至少間隔 SYSTEMIC_COOLDOWN_MIN 分鐘 (預設 60)
#   - 全 symbol 一天最多 SYSTEMIC_MAX_PER_DAY 則 (預設 2)
#
# State: monitor_state["systemic_crash_alerts"][symbol] = {
#   "date": "YYYY-MM-DD", "last_alert_at": iso, "alerts_today": int
# }
SYSTEMIC_CRASH_CONFIG = {
    "^TWII": {"name": "台灣加權", "country": "TW"},
    "^SOX":  {"name": "費城半導體", "country": "US"},
    "^GSPC": {"name": "S&P 500", "country": "US"},
    "TSM":   {"name": "台積電 ADR", "country": "US"},
}
SYSTEMIC_INTRADAY_PCT = -3.0      # 觸發門檻: 盤中 -3%
SYSTEMIC_TWO_DAY_CUM_PCT = -4.0   # 觸發門檻: 連續 2 日累計 -4%
SYSTEMIC_COOLDOWN_MIN = 60        # 同 symbol 兩警報間最少間隔 (分)
SYSTEMIC_MAX_PER_DAY = 2          # 全 symbol 每日最多警報數


def _fetch_systemic_snapshot(symbol: str) -> Optional[Dict]:
    """抓系統性大跌判斷需要的數據:
       今日開盤 / 即時價 / 前日收盤 / 前前日收盤.

    優先用 5m intraday 抓 today_open + current, 然後用 daily 抓 prior closes.
    任一失敗 → 回 None.
    """
    import pandas as pd

    # 1) Daily 抓 prior close 跟前前日 close (近 5 個交易日)
    df_d = ds.fetch_yf_history(symbol, period="10d", interval="1d")
    if df_d is None or df_d.empty or len(df_d) < 2:
        return None
    try:
        df_d = df_d.copy()
        date_col_d = "Date" if "Date" in df_d.columns else df_d.columns[0]
        df_d["_d"] = pd.to_datetime(df_d[date_col_d]).dt.date
        df_d = df_d.sort_values("_d")
        latest_d = df_d["_d"].iloc[-1]
        sys_today = dt.date.today()
        # daily 已過期 → 跳 (盤前 / 假日 / yfinance 延遲)
        if (sys_today - latest_d).days >= 2:
            return None
        # 視 latest 是否為「今天」
        latest_is_today = (latest_d == sys_today)
        closes = df_d["Close"].astype(float).tolist()
    except Exception:
        return None

    # 2) 5m intraday 抓 today_open + current (若 latest_is_today 為 False, 表示盤前/休市)
    today_open = None
    current = None
    df_i = ds.fetch_yf_history(symbol, period="2d", interval="5m")
    if df_i is not None and not df_i.empty:
        try:
            df_i = df_i.copy()
            date_col_i = "Datetime" if "Datetime" in df_i.columns else df_i.columns[0]
            df_i["_dt"] = pd.to_datetime(df_i[date_col_i])
            df_i["_d"] = df_i["_dt"].dt.date
            today_in_intraday = df_i["_d"].max()
            sys_today = dt.date.today()
            if (sys_today - today_in_intraday).days < 1:
                today_bars = df_i[df_i["_d"] == today_in_intraday].sort_values("_dt")
                if not today_bars.empty:
                    today_open = float(today_bars.iloc[0]["Open"])
                    current = float(today_bars.iloc[-1]["Close"])
        except Exception:
            pass

    # 3) 若 intraday 拿不到 → fallback 用 daily (latest)
    if today_open is None or current is None:
        try:
            today_open = float(df_d["Open"].iloc[-1])
            current = float(df_d["Close"].iloc[-1])
        except Exception:
            return None

    # 4) prior close: 若 latest_is_today, 用倒數第 2;否則用倒數第 1
    try:
        if latest_is_today and len(closes) >= 2:
            prior_close = float(closes[-2])
        else:
            prior_close = float(closes[-1])
    except Exception:
        return None

    # 5) 前前日 close (給「連續 2 日累計」用)
    try:
        if latest_is_today and len(closes) >= 3:
            prior_prior_close = float(closes[-3])
        elif (not latest_is_today) and len(closes) >= 2:
            prior_prior_close = float(closes[-2])
        else:
            prior_prior_close = None
    except Exception:
        prior_prior_close = None

    return {
        "today_open": today_open,
        "current": current,
        "prior_close": prior_close,
        "prior_prior_close": prior_prior_close,
    }


def check_systemic_crash() -> Optional[Dict]:
    """檢測系統性大跌. 任一監控標的觸發即回傳整體 context.

    回傳 None (沒觸發) 或 dict:
    {
      "triggers": [{symbol, name, country, trigger_type, value, ...}, ...],
      "context": {
        "vix": float | None,
        "all_snapshots": [...全部標的的當前狀態給 Gemini 看全局],
        "macro_news": str | None,  # 國際新聞背景
        "checked_at": ISO,
      },
      "alert_index": int  # 今天第 N 次 (1 開始, 用於文案)
    }
    """
    # 載 state — 共用 monitor_state.json
    state = watchlist_store.load_monitor_state()
    crash_state = state.setdefault("systemic_crash_alerts", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    now_utc = dt.datetime.utcnow()

    # 跨日重置: 全部 symbol 都把舊日的 state 清掉
    for sym in list(crash_state.keys()):
        if crash_state[sym].get("date") != today_str:
            crash_state[sym] = {"date": today_str, "last_alert_at": None, "alerts_today": 0}

    # 今日已發過的總數 (跨 symbol)
    total_today = sum(s.get("alerts_today", 0) for s in crash_state.values())
    if total_today >= SYSTEMIC_MAX_PER_DAY:
        return None

    triggers: List[Dict] = []
    all_snapshots: List[Dict] = []

    for sym, cfg in SYSTEMIC_CRASH_CONFIG.items():
        country = cfg.get("country", "")
        # 該市場必須在 session 內 (避免抓 stale close 觸發假警報)
        if not _is_market_in_session(country):
            continue
        snap = _fetch_systemic_snapshot(sym)
        if not snap:
            continue
        today_open = snap["today_open"]
        current = snap["current"]
        prior_close = snap["prior_close"]
        prior_prior = snap.get("prior_prior_close")

        # 算盤中跌幅 vs 開盤 / vs 昨收 (取較大的負值)
        try:
            pct_vs_open = (current / today_open - 1) * 100 if today_open else 0.0
        except Exception:
            pct_vs_open = 0.0
        try:
            pct_vs_prior = (current / prior_close - 1) * 100 if prior_close else 0.0
        except Exception:
            pct_vs_prior = 0.0
        intraday_pct = min(pct_vs_open, pct_vs_prior)

        # 算 2 日累計 (前日 close → 今日 close, 並把昨日 close vs 前前日 close 加上)
        two_day_pct = None
        try:
            if prior_prior and prior_close and prior_prior > 0:
                # 兩日複合報酬: (1+d1)*(1+d2)-1
                d1 = prior_close / prior_prior - 1
                d2 = current / prior_close - 1 if prior_close else 0
                two_day_pct = ((1 + d1) * (1 + d2) - 1) * 100
        except Exception:
            two_day_pct = None

        snap_full = {
            "symbol": sym,
            "name": cfg["name"],
            "country": country,
            "today_open": round(today_open, 4),
            "current": round(current, 4),
            "prior_close": round(prior_close, 4),
            "pct_vs_open": round(pct_vs_open, 2),
            "pct_vs_prior": round(pct_vs_prior, 2),
            "intraday_pct": round(intraday_pct, 2),
            "two_day_pct": round(two_day_pct, 2) if two_day_pct is not None else None,
        }
        all_snapshots.append(snap_full)

        # === 判斷是否觸發 ===
        sym_state = crash_state.setdefault(sym, {"date": today_str, "last_alert_at": None, "alerts_today": 0})

        # Cooldown: 同 symbol 至少 60 分鐘
        last_at = sym_state.get("last_alert_at")
        if last_at:
            try:
                last_dt = dt.datetime.fromisoformat(last_at)
                if (now_utc - last_dt).total_seconds() < SYSTEMIC_COOLDOWN_MIN * 60:
                    continue
            except Exception:
                pass

        trigger_type = None
        trigger_value = None
        if intraday_pct <= SYSTEMIC_INTRADAY_PCT:
            trigger_type = "intraday"
            trigger_value = intraday_pct
        elif two_day_pct is not None and two_day_pct <= SYSTEMIC_TWO_DAY_CUM_PCT:
            trigger_type = "two_day"
            trigger_value = two_day_pct

        if trigger_type:
            triggers.append({
                **snap_full,
                "trigger_type": trigger_type,
                "trigger_value": round(trigger_value, 2),
            })
            sym_state["last_alert_at"] = now_utc.isoformat()
            sym_state["alerts_today"] = sym_state.get("alerts_today", 0) + 1

    if not triggers:
        # 沒觸發 — 仍存回 state (跨日 reset 已套用)
        state["systemic_crash_alerts"] = crash_state
        watchlist_store.save_monitor_state(state)
        return None

    # === 蒐集上下文 (VIX + 國際新聞) 給 Gemini 用 ===
    vix_now = None
    try:
        vix_df = ds.fetch_yf_history("^VIX", period="5d", interval="1d")
        if vix_df is not None and not vix_df.empty:
            vix_now = float(vix_df["Close"].astype(float).iloc[-1])
    except Exception:
        pass

    macro_news = None
    try:
        import news_sources
        macro_news = news_sources.build_news_context(include_trump=True)
    except Exception:
        macro_news = None

    # 算今天第幾次 (給文案用)
    new_total = sum(s.get("alerts_today", 0) for s in crash_state.values())

    state["systemic_crash_alerts"] = crash_state
    watchlist_store.save_monitor_state(state)

    return {
        "triggers": triggers,
        "context": {
            "vix": round(vix_now, 2) if vix_now is not None else None,
            "all_snapshots": all_snapshots,
            "macro_news": macro_news,
            "checked_at": now_utc.isoformat(),
        },
        "alert_index": new_total,
        "max_per_day": SYSTEMIC_MAX_PER_DAY,
    }


# ---------------------------------------------------------------------------
# ATR helper (給動態門檻用)
# ---------------------------------------------------------------------------
def _compute_atr(symbol: str, period_days: str = "2mo", n: int = 14) -> Optional[float]:
    """抓 daily K 線算 ATR(14). 失敗回 None."""
    try:
        import pandas as pd
        df = ds.fetch_yf_history(symbol, period=period_days, interval="1d")
        if df is None or df.empty or len(df) < n + 1:
            return None
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return float(tr.tail(n).mean())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 大盤點數警報
# ---------------------------------------------------------------------------
def _fetch_index_today_open_and_current(symbol: str) -> Optional[Dict]:
    """抓今天開盤價跟即時價 (用 5m 線推今日開盤)."""
    df = ds.fetch_yf_history(symbol, period="2d", interval="5m")
    if df.empty:
        # fallback to daily — 也要做 freshness check
        df_d = ds.fetch_yf_history(symbol, period="2d", interval="1d")
        if df_d.empty or len(df_d) < 1:
            return None
        try:
            import pandas as pd
            df_d = df_d.copy()
            date_col_d = "Date" if "Date" in df_d.columns else df_d.columns[0]
            latest_d = pd.to_datetime(df_d[date_col_d].iloc[-1]).date()
            sys_today = dt.date.today()
            if (sys_today - latest_d).days >= 1:
                return None  # daily 也是 stale, 跳過
            today_open = float(df_d["Open"].iloc[-1])
            today_close = float(df_d["Close"].iloc[-1])
            return {"open": today_open, "current": today_close}
        except Exception:
            return None

    import pandas as pd
    df = df.copy()
    date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
    df["_dt"] = pd.to_datetime(df[date_col])
    df["_d"] = df["_dt"].dt.date
    today = df["_d"].max()

    # 防線: 抓到的最新日期跟系統 UTC 今日差超過 1 天 → 視為 stale data, 不處理
    # (例: 韓國 5/1 休市但假日清單沒列, yfinance 返回 4/30 的 bars)
    sys_today = dt.date.today()
    if (sys_today - today).days >= 1:
        return None

    today_bars = df[df["_d"] == today].sort_values("_dt")
    if today_bars.empty:
        return None
    try:
        today_open = float(today_bars.iloc[0]["Open"])
        current = float(today_bars.iloc[-1]["Close"])
        return {"open": today_open, "current": current}
    except Exception:
        return None


def check_index_alerts() -> List[Dict]:
    """檢查大盤點數是否觸發新門檻. 該市場休市則跳過."""
    state = watchlist_store.load_monitor_state()
    idx_state = state.setdefault("index_alerts", {})

    # 假日檢查 (避免抓到前日 stale data 觸發假警報)
    closed_markets: set = set()
    try:
        import holiday_check
        for mk in ["TW", "JP", "KR", "US"]:
            if holiday_check.is_market_closed_today(mk):
                closed_markets.add(mk)
    except Exception:
        pass

    today_str = dt.date.today().strftime("%Y-%m-%d")
    alerts: List[Dict] = []

    for sym, cfg in INDEX_CONFIG.items():
        country = cfg.get("country")
        # 1) 該市場假日休市 → 跳過 (避免抓 stale data)
        if country in closed_markets:
            continue
        # 2) 該市場非交易時段 → 跳過 (per-market 判斷)
        #    TW/JP/KR 只在亞股交易時段監控, US 只在美股交易時段監控
        if not _is_market_in_session(country):
            continue
        info = _fetch_index_today_open_and_current(sym)
        if not info:
            continue
        today_open = info["open"]
        current = info["current"]
        diff = current - today_open  # 點數變化

        # 3 層 throttle 降低警報頻率:
        # (1) ATR 動態門檻 — 高波動期自動拉高 (費半 ^SOX ATR 約 80-150 點, ×0.8 = 64-120 接近其靜態 100)
        # (2) 時間 throttle — 兩次警報間至少 30 分鐘 (避免 5 分鐘內連環推)
        # (3) 每日上限 — 每 index 一天最多 4 次警報 (避免一整天一直跳)
        threshold = cfg["threshold"]
        try:
            atr14 = _compute_atr(sym, period_days="2mo", n=14)
            if atr14 is not None:
                # 原 0.3 太敏感 (TWII ATR 300×0.3=90 vs static 100), 改 0.8
                # 80% 標準差 = 一天典型大波段, 合理
                dyn = atr14 * 0.8
                upper_cap = threshold * 3  # 門檻最多 3 倍 (極高波動期才會 hit)
                if dyn > threshold:
                    threshold = round(min(dyn, upper_cap), 1)
        except Exception:
            pass

        # 取 bucket: -300, -150, 0, 150, 300, ...
        if diff >= 0:
            bucket = int(diff // threshold) * threshold
        else:
            bucket = -(int(-diff // threshold) * threshold)

        # 跨日重置
        sym_state = idx_state.setdefault(sym, {})
        if sym_state.get("date") != today_str:
            sym_state.clear()
            sym_state["date"] = today_str
            sym_state["last_bucket"] = 0
            sym_state["last_alert_diff"] = 0.0
            sym_state["last_alert_price"] = round(today_open, 2)
            sym_state["consecutive_count"] = 0
            sym_state["last_direction"] = "none"
            sym_state["last_alert_at"] = None       # 新增: 上次警報時間
            sym_state["alerts_today_count"] = 0     # 新增: 今日已警報次數

        last_bucket = sym_state.get("last_bucket", 0)
        if bucket != last_bucket and abs(bucket) >= threshold:
            # 在 throttle 之前先算 direction (throttle skip 也要更新, 避免 stale 方向標籤)
            direction = "漲" if diff > 0 else "跌"
            last_direction = sym_state.get("last_direction", "none")
            # 方向變了 → 不管有沒有 throttle, consecutive 都歸 1
            if direction != last_direction:
                sym_state["consecutive_count"] = 0  # 暫存歸零, 下面 +1
                sym_state["last_direction"] = direction

            # === Throttle layer 2: 時間間隔檢查 (兩次警報間至少 30 分鐘) ===
            # 重要: skip 時不更新 last_bucket, 留給下個 tick 重評
            last_at = sym_state.get("last_alert_at")
            if last_at:
                try:
                    last_dt = dt.datetime.fromisoformat(last_at)
                    elapsed = (dt.datetime.utcnow() - last_dt).total_seconds()
                    if elapsed < 30 * 60:  # 30 分鐘
                        continue
                except Exception:
                    pass

            # === Throttle layer 3: 每日上限 ===
            alerts_count = sym_state.get("alerts_today_count", 0)
            MAX_ALERTS_PER_DAY = 4
            if alerts_count >= MAX_ALERTS_PER_DAY:
                continue

            # 通過所有 throttle → 觸發警報, 累計 consecutive
            sym_state["consecutive_count"] = sym_state.get("consecutive_count", 0) + 1
            consecutive = sym_state["consecutive_count"]

            last_alert_diff = float(sym_state.get("last_alert_diff", 0))
            last_alert_price = float(sym_state.get("last_alert_price", today_open))
            leg_pts = round(diff - last_alert_diff, 2)

            alerts.append({
                "symbol": sym,
                "name": cfg["name"],
                "country": cfg["country"],
                "today_open": round(today_open, 2),
                "current": round(current, 2),
                "diff": round(diff, 2),
                "last_alert_price": round(last_alert_price, 2),
                "last_alert_diff": round(last_alert_diff, 2),
                "leg_pts": leg_pts,
                "direction": direction,
                "threshold_bucket": bucket,
                "threshold_used": threshold,  # 新增: 顯示實際用的 (ATR 動態)
                "consecutive": consecutive,
                "warning": consecutive >= 2,
                "alerts_today": alerts_count + 1,  # 今日第 N 次
            })
            sym_state["last_bucket"] = bucket
            sym_state["last_alert_diff"] = round(diff, 2)
            sym_state["last_alert_price"] = round(current, 2)
            sym_state["last_alert_at"] = dt.datetime.utcnow().isoformat()
            sym_state["alerts_today_count"] = alerts_count + 1

    state["index_alerts"] = idx_state
    watchlist_store.save_monitor_state(state)
    return alerts


# ---------------------------------------------------------------------------
# 加密貨幣警報 (排程制 — 一天頂多 1 次)
# ---------------------------------------------------------------------------
# 排程時段 (UTC hour). 用 monitor cron (15) 在每 hour 的 minute 15 觸發.
#   15:00 UTC = 23:00 台北 (晚上, BTC/ETH 簡短價變動提醒)
#
# 註: 原本 04:00 (noon) 已移除, 因為 04:00 UTC 已被 crypto_picks cron 用於推 5 檔 Top picks.
#     兩個重疊會在 15 分鐘內推兩封訊息 (5 檔 picks + BTC/ETH 簡短價變動), 多餘.
CRYPTO_SCHEDULE_UTC_HOURS = {
    15: "night",  # 台北 23:00
}


def check_crypto_alerts() -> List[Dict]:
    """加密貨幣排程警報.

    每 slot (台北 12:00 / 23:00) 最多 2 次:
    - First push (minute 15): 固定發, 比對「上一個 slot 的最後一次推播」
    - Second push (minute 45): 只在跟 first push 相比漲跌絕對值 >= threshold (2.5%)
                                才發, 訊息標記為「盤中變動」

    State 結構 (crypto_alerts[symbol]):
      last_slot              — dedup key, 例 "2026-05-02_night"
      first_alert_price      — 該 slot 第一次推播的價, 用來算 second push 是否觸發
      first_alert_time       — 該 slot 第一次推播的時間
      prev_alert_price       — 最近一次推播的價 (跨 slot, 給下個 slot 比對用)
      prev_alert_time        — 最近一次推播的時間
    """
    now_utc = dt.datetime.utcnow()
    cur_hour = now_utc.hour
    cur_minute = now_utc.minute

    # 不在排定 hour → 直接 return
    if cur_hour not in CRYPTO_SCHEDULE_UTC_HOURS:
        return []

    is_first_tick = cur_minute < 30  # cron 15,45 → 第一個 tick 在前半小時

    slot_label = CRYPTO_SCHEDULE_UTC_HOURS[cur_hour]
    today_str = now_utc.strftime("%Y-%m-%d")
    slot_key = f"{today_str}_{slot_label}"

    state = watchlist_store.load_monitor_state()
    crypto_state = state.setdefault("crypto_alerts", {})

    print(
        f"[crypto] slot={slot_key} tick={'first' if is_first_tick else 'second'} "
        f"min={cur_minute}, state has {len(crypto_state)} symbols",
        flush=True,
    )

    alerts: List[Dict] = []
    now_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    for sym, cfg in CRYPTO_CONFIG.items():
        # 抓即時價
        df = ds.fetch_yf_history(sym, period="2d", interval="1h")
        if df.empty:
            print(f"[crypto] {sym} no data, skip", flush=True)
            continue
        try:
            current = float(df["Close"].astype(float).iloc[-1])
        except Exception:
            continue

        sid_state = crypto_state.setdefault(sym, {})
        already_first = (sid_state.get("last_slot") == slot_key)
        threshold = float(cfg.get("threshold_pct", 2.5))

        # ===== Case A: 該 slot 還沒發過 first push =====
        if not already_first:
            # 必發 — 比對上一個 slot 的 prev_alert_price (cross-slot)
            prev_price = sid_state.get("prev_alert_price")
            prev_time = sid_state.get("prev_alert_time", "")
            try:
                prev_price = float(prev_price) if prev_price not in (None, "") else None
            except Exception:
                prev_price = None

            if prev_price and prev_price > 0:
                change_pct = (current / prev_price - 1) * 100
                change_abs = current - prev_price
                direction = "上漲" if change_pct > 0.05 else ("下跌" if change_pct < -0.05 else "持平")
                is_first_global = False
            else:
                change_pct = 0.0
                change_abs = 0.0
                direction = "首次紀錄"
                is_first_global = True

            alerts.append({
                "symbol": sym,
                "name": cfg["name"],
                "current": round(current, 2),
                "prev_price": round(prev_price, 2) if prev_price else None,
                "prev_time": prev_time,
                "change_pct": round(change_pct, 2),
                "change_abs": round(change_abs, 2),
                "direction": direction,
                "slot": slot_label,
                "slot_label_zh": "晚上 23:00",  # 04:00 noon 已被 crypto_picks 取代, 只剩 night slot
                "is_first": is_first_global,
                "alert_type": "scheduled",  # 固定排程推播
            })

            # 更新 state — 紀錄 first push 資訊 + cross-slot prev
            sid_state["last_slot"] = slot_key
            sid_state["first_alert_price"] = current
            sid_state["first_alert_time"] = now_str
            sid_state["prev_alert_price"] = current
            sid_state["prev_alert_time"] = now_str
            print(f"[crypto] {sym} FIRST push: {prev_price} → {current} ({change_pct:+.2f}%)", flush=True)

        # ===== Case B: 該 slot 已發過 first push, 檢查是否要發 second =====
        else:
            # second push 只在第二個 tick (minute >= 30) 評估
            if is_first_tick:
                # 同 slot 同 tick 已發過 → skip
                print(f"[crypto] {sym} already alerted for {slot_key} (first tick), skip", flush=True)
                continue

            first_price = sid_state.get("first_alert_price")
            try:
                first_price = float(first_price) if first_price not in (None, "") else None
            except Exception:
                first_price = None
            if not first_price or first_price <= 0:
                print(f"[crypto] {sym} no first_price recorded, skip", flush=True)
                continue
            change_pct = (current / first_price - 1) * 100
            change_abs = current - first_price
            if abs(change_pct) < threshold:
                print(f"[crypto] {sym} 2nd-tick change {change_pct:+.2f}% < {threshold}%, skip", flush=True)
                continue
            direction = "急漲" if change_pct > 0 else "急跌"
            alerts.append({
                "symbol": sym, "name": cfg["name"],
                "current": round(current, 2),
                "prev_price": round(first_price, 2),
                "prev_time": sid_state.get("first_alert_time", ""),
                "change_pct": round(change_pct, 2),
                "change_abs": round(change_abs, 2),
                "direction": direction,
                "slot": slot_label,
                "slot_label_zh": "晚上 23:00",
                "is_first": False,
                "alert_type": "intra_slot",
                "threshold_pct": threshold,
            })
            sid_state["prev_alert_price"] = current
            sid_state["prev_alert_time"] = now_str
            print(f"[crypto] {sym} SECOND push: {first_price} -> {current} ({change_pct:+.2f}%) >= {threshold}%", flush=True)

    state["crypto_alerts"] = crypto_state
    watchlist_store.save_monitor_state(state)
    return alerts
