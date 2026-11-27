"""
intraday_volume.py
盤中「時間校正量比」— 今日到目前為止的累計量 ÷「平常到這個時間點」的累計量。

背景 (audit #2):
  原本盤中量比 = 今日盤中累計量 ÷ 過去 N 日「整天」平均量。開盤 30 分鐘時, 就算
  每一檔都照平常步調在成交, 量比也只有 0.1~0.3 → 量能分數整個被系統性壓低/反轉,
  「量比 ≥1.5」幾乎只剩爆量 5 倍的股票才過得了, 「量比 <0.8」則是人人有份。
  這支模組讓量比 1.0 回到「以這個時間點來說是正常量」的意思。

兩種方法 (依可用資料自動選擇):
  1) 歷史同時段 (首選): 呼叫端已經抓了 yfinance 5m/1m intraday (period="5d"),
     就用「前 N 個交易日, 累計到同一個時鐘時間的量」的平均當分母。
     → intraday_volume_ratio(df_intraday, market)
  2) 固定量能曲線 (fallback): 只有日線 (今日那根是盤中未完成的 K 棒) 時,
     分母 = 過去 N 日平均整日量 × cum_fraction(開盤後經過分鐘數)。
     → daily_volume_ratio(df_daily, market) / adjust_full_day_ratio(raw, market)

時間一律換算成「該市場的時區」(GitHub runner 是 UTC):
  TW: 09:00–13:30 Asia/Taipei (270 分鐘)
  US: 09:30–16:00 America/New_York (390 分鐘, 夏令/冬令由 tz 資料庫處理)

經過時間夾在 [MIN_ELAPSED_MIN, 盤長]; 收盤後 (或資料最後一天已不是今天) 量比
= 一般的「今日整日量 ÷ 平均整日量」。

本模組不發任何網路請求 — 只吃呼叫端用 ds.fetch_yf_history 抓回來的 DataFrame
(yfinance 走全域鎖 + threads=False, 不要在這裡另開抓取)。
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# 市場時段
# ---------------------------------------------------------------------------
SESSIONS: Dict[str, Dict] = {
    "TW": {"tz": "Asia/Taipei", "open": (9, 0), "close": (13, 30), "length": 270},
    "US": {"tz": "America/New_York", "open": (9, 30), "close": (16, 0), "length": 390},
}

MIN_ELAPSED_MIN = 5.0   # 開盤前幾分鐘量太少、曲線分母趨近 0 → 至少當作開盤 5 分鐘

# ---------------------------------------------------------------------------
# 固定量能曲線 (U 型: 開盤、收盤量大, 午盤量小) — 「開盤後經過分鐘數 → 累計佔整日量比例」
# 節點之間線性內插。數值是經驗估計 (台股 / 美股大型股的典型日內分布), 不是精確統計:
#
# 台股 (270 分): 開盤前 30 分鐘約佔 22%, 13:25-13:30 收盤集合競價約佔 8~10%。
#   分鐘:  0     5     15    30    60    90    120   150   180   210   240   265   270
#   累計:  0    .06   .13   .22   .35   .45   .53   .60   .67   .74   .82   .90   1.0
#
# 美股 (390 分): 開盤前 30 分鐘約佔 12%, 最後 30 分鐘 (含收盤競價) 約佔 20%。
#   分鐘:  0     5     15    30    60    90    120   180   240   300   330   360   385   390
#   累計:  0    .03   .065  .12   .20   .27   .33   .44   .55   .66   .72   .80   .88   1.0
# ---------------------------------------------------------------------------
_CUM_CURVES: Dict[str, Tuple[Tuple[float, float], ...]] = {
    "TW": (
        (0, 0.0), (5, 0.06), (15, 0.13), (30, 0.22), (60, 0.35), (90, 0.45),
        (120, 0.53), (150, 0.60), (180, 0.67), (210, 0.74), (240, 0.82),
        (265, 0.90), (270, 1.0),
    ),
    "US": (
        (0, 0.0), (5, 0.03), (15, 0.065), (30, 0.12), (60, 0.20), (90, 0.27),
        (120, 0.33), (180, 0.44), (240, 0.55), (300, 0.66), (330, 0.72),
        (360, 0.80), (385, 0.88), (390, 1.0),
    ),
}


def _mkt(market: str) -> str:
    m = str(market or "TW").upper()
    return m if m in SESSIONS else "TW"


def _now_local(market: str, now: Optional[dt.datetime] = None) -> pd.Timestamp:
    """把 now 轉成市場當地時間. now 沒給 = 現在; naive now 一律視為 UTC (runner 是 UTC)."""
    tz = SESSIONS[_mkt(market)]["tz"]
    ts = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(tz)


def _minutes_since_open(ts_local: pd.Timestamp, market: str) -> float:
    oh, om = SESSIONS[_mkt(market)]["open"]
    return (ts_local.hour * 60 + ts_local.minute + ts_local.second / 60.0) - (oh * 60 + om)


def elapsed_minutes(market: str, now: Optional[dt.datetime] = None) -> float:
    """開盤後經過分鐘數 (當地時間, 未夾限; 開盤前為負, 收盤後 > 盤長)."""
    return _minutes_since_open(_now_local(market, now), market)


def cum_fraction(elapsed_min: float, market: str = "TW") -> float:
    """固定曲線: 開盤後 elapsed_min 分鐘時, 累計量約佔整日量的比例 (0~1)."""
    m = _mkt(market)
    length = SESSIONS[m]["length"]
    x = max(MIN_ELAPSED_MIN, min(float(elapsed_min), float(length)))
    pts = _CUM_CURVES[m]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 > x0 else y1
    return 1.0


def _session_in_progress(market: str, now_local: pd.Timestamp) -> bool:
    """now 是否落在今日盤中 (週末不算; 國定假日由呼叫端用資料日期 / holiday_check 判斷)."""
    if now_local.weekday() >= 5:
        return False
    e = _minutes_since_open(now_local, market)
    return 0 <= e < SESSIONS[_mkt(market)]["length"]


def profile_ratio(today_cum_vol: float, avg_full_day_vol: float, market: str = "TW",
                  now: Optional[dt.datetime] = None, session_done: bool = False) -> Optional[float]:
    """曲線法: 今日累計量 ÷ (平均整日量 × cum_fraction(經過分鐘)). 收盤後 = 整日量比."""
    if not avg_full_day_vol or avg_full_day_vol <= 0 or today_cum_vol is None:
        return None
    frac = 1.0 if session_done else cum_fraction(elapsed_minutes(market, now), market)
    return float(today_cum_vol) / (float(avg_full_day_vol) * frac)


# ---------------------------------------------------------------------------
# 1) intraday bars → 歷史同時段累計量
# ---------------------------------------------------------------------------
def _bars_local(df: pd.DataFrame, market: str) -> Optional[pd.DataFrame]:
    """整理 intraday bars: 回 [_ts(當地 tz), _d(當地日期), _m(開盤後分鐘), Volume]."""
    if df is None or df.empty or "Volume" not in df.columns:
        return None
    if "Datetime" in df.columns:
        ts = pd.to_datetime(df["Datetime"])
    elif "Date" in df.columns:
        ts = pd.to_datetime(df["Date"])
    elif isinstance(df.index, pd.DatetimeIndex):
        ts = pd.Series(df.index, index=df.index)
    else:
        ts = pd.to_datetime(df[df.columns[0]])
    tz = SESSIONS[_mkt(market)]["tz"]
    # yfinance intraday 正常是交易所時區的 tz-aware; naive 時視為已是當地時間
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(tz)
    else:
        ts = ts.dt.tz_localize(tz)
    out = pd.DataFrame({
        "_ts": ts.reset_index(drop=True),
        "Volume": pd.to_numeric(df["Volume"], errors="coerce").fillna(0.0).astype(float)
                    .reset_index(drop=True),
    })
    out["_d"] = out["_ts"].dt.date
    oh, om = SESSIONS[_mkt(market)]["open"]
    out["_m"] = out["_ts"].dt.hour * 60 + out["_ts"].dt.minute - (oh * 60 + om)
    return out.sort_values("_ts").reset_index(drop=True)


def intraday_volume_ratio_detail(df_intraday: pd.DataFrame, market: str = "TW",
                                 now: Optional[dt.datetime] = None,
                                 lookback_days: int = 5) -> Optional[Dict]:
    """用 intraday bars (5m/1m, 含前幾日) 算時間校正量比.

    回 {ratio, method, elapsed_min, today_vol, expected_vol} 或 None (資料不足).
      method = "full_day"  — 收盤後 / 資料最後一天不是今天: 今日整日量 ÷ 前 N 日平均整日量
               "history"   — 盤中: 今日累計 ÷ 前 N 日「同一時鐘時間」累計量平均
               "profile"   — 盤中但前幾日同時段資料不足: 退回固定曲線
    最後一根 bar 可能還沒走完: 前幾日對應那根 bar 依「已經過幾分鐘 / bar 長度」按比例計入。
    """
    m = _mkt(market)
    bars = _bars_local(df_intraday, m)
    if bars is None or bars.empty:
        return None
    length = SESSIONS[m]["length"]
    today = bars["_d"].max()
    today_bars = bars[bars["_d"] == today]
    prev = bars[bars["_d"] < today]
    if today_bars.empty or prev.empty:
        return None
    today_vol = float(today_bars["Volume"].sum())
    prev_days = sorted(prev["_d"].unique())[-max(1, int(lookback_days)):]
    prev = prev[prev["_d"].isin(prev_days)]
    prev_totals = prev.groupby("_d")["Volume"].sum()
    avg_full = float(prev_totals.mean()) if len(prev_totals) else 0.0
    if avg_full <= 0:
        return None

    now_local = _now_local(m, now)
    now_min = _minutes_since_open(now_local, m)
    session_done = (now_local.date() > today) or (now_min >= length)
    if session_done:
        return {"ratio": today_vol / avg_full, "method": "full_day",
                "elapsed_min": float(length), "today_vol": today_vol, "expected_vol": avg_full}

    # bar 長度: 取今日/前幾日相鄰 bar 間隔的中位數 (5m → 5)
    diffs = bars.groupby("_d")["_m"].diff().dropna()
    diffs = diffs[diffs > 0]
    bar_len = float(diffs.median()) if not diffs.empty else 5.0
    last_m = float(today_bars["_m"].max())
    # 最後一根 bar 已走完的比例 (資料延遲 → 那根其實已完整 → 夾到 1)
    frac_last = (now_min - last_m) / bar_len if bar_len > 0 else 1.0
    frac_last = max(0.0, min(1.0, frac_last))
    # 經過時間至少 MIN_ELAPSED_MIN: 開盤第一根 bar 剛開始時不要用接近 0 的分母
    if last_m + frac_last * bar_len < MIN_ELAPSED_MIN:
        frac_last = min(1.0, (MIN_ELAPSED_MIN - last_m) / bar_len) if bar_len > 0 else 1.0
    elapsed = max(MIN_ELAPSED_MIN, min(last_m + frac_last * bar_len, float(length)))

    cums = []
    for d in prev_days:
        b = prev[prev["_d"] == d]
        # 那天在這個時間點之後根本沒有 bar (提早收盤 / 缺資料) → 不列入
        if b.empty or b["_m"].max() < last_m:
            continue
        full = b.loc[b["_m"] < last_m, "Volume"].sum()
        part = b.loc[(b["_m"] >= last_m) & (b["_m"] < last_m + bar_len), "Volume"].sum()
        cums.append(float(full + frac_last * part))
    expected = (sum(cums) / len(cums)) if cums else 0.0
    if expected > 0:
        return {"ratio": today_vol / expected, "method": "history",
                "elapsed_min": elapsed, "today_vol": today_vol, "expected_vol": expected}
    exp_p = avg_full * cum_fraction(elapsed, m)
    return {"ratio": today_vol / exp_p, "method": "profile",
            "elapsed_min": elapsed, "today_vol": today_vol, "expected_vol": exp_p}


def intraday_volume_ratio(df_intraday: pd.DataFrame, market: str = "TW",
                          now: Optional[dt.datetime] = None,
                          lookback_days: int = 5) -> Optional[float]:
    """intraday_volume_ratio_detail 的簡化版: 只回 ratio (資料不足回 None)."""
    d = intraday_volume_ratio_detail(df_intraday, market, now=now, lookback_days=lookback_days)
    return None if d is None else float(d["ratio"])


# ---------------------------------------------------------------------------
# 2) 日線 (今日 K 棒盤中未完成) → 固定曲線
# ---------------------------------------------------------------------------
def daily_volume_ratio(df_daily: pd.DataFrame, market: str = "TW",
                       now: Optional[dt.datetime] = None,
                       lookback_days: int = 5) -> Optional[float]:
    """日線版量比: 最後一根量 ÷ 前 lookback_days 根平均量; 若最後一根是「今天」且
    還在盤中, 分母改成 平均整日量 × cum_fraction(經過分鐘). 資料不足回 None.
    """
    if df_daily is None or df_daily.empty or "Volume" not in df_daily.columns:
        return None
    vol = pd.to_numeric(df_daily["Volume"], errors="coerce").astype(float)
    if len(vol) < lookback_days + 1:
        return None
    avg = float(vol.iloc[-(lookback_days + 1):-1].mean())
    last = float(vol.iloc[-1])
    if not avg or avg <= 0 or pd.isna(avg) or pd.isna(last):
        return None
    m = _mkt(market)
    now_local = _now_local(m, now)
    last_date = None
    try:
        if "Date" in df_daily.columns:
            last_date = pd.Timestamp(df_daily["Date"].iloc[-1])
        elif isinstance(df_daily.index, pd.DatetimeIndex):
            last_date = pd.Timestamp(df_daily.index[-1])
        if last_date is not None and last_date.tzinfo is not None:
            last_date = last_date.tz_convert(SESSIONS[m]["tz"])
        last_date = last_date.date() if last_date is not None else None
    except Exception:
        last_date = None
    in_progress = (last_date == now_local.date()) and _session_in_progress(m, now_local)
    return profile_ratio(last, avg, m, now=now_local, session_done=not in_progress)


def time_adjust_fraction(market: str = "TW", now: Optional[dt.datetime] = None,
                         check_holiday: bool = True) -> Optional[float]:
    """盤中時回 cum_fraction(經過分鐘) (0~1), 不在盤中 (盤前 / 收盤後 / 週末 / 假日) 回 None."""
    m = _mkt(market)
    now_local = _now_local(m, now)
    if not _session_in_progress(m, now_local):
        return None
    if check_holiday:
        try:
            from radar.core import holiday_check
            if holiday_check.is_market_closed_today(m, today=now_local.date()):
                return None
        except Exception as e:
            # 查不到假日 → 照工作日處理 (最壞是假日盤中時段量比被高估, 不會中斷推播)
            print(f"[intraday_volume] holiday_check failed: {e}", flush=True)
    return cum_fraction(_minutes_since_open(now_local, m), m)


def adjust_full_day_ratio(raw_ratio: Optional[float], market: str = "TW",
                          now: Optional[dt.datetime] = None,
                          check_holiday: bool = True,
                          _frac: Optional[float] = -1.0) -> Optional[float]:
    """把已經算好的「今日累計 ÷ 平均整日量」量比換成時間校正量比 (= raw / cum_fraction).

    給拿不到原始日期的資料源用 (例如 sector_pulse.fetch_intraday_metrics 只回「量比」欄)。
    只在「當地今天是交易日且 now 在盤中」時校正, 其他時間 (盤前 / 收盤後 / 週末 / 假日,
    此時最後一根日 K 是完整的一天) 原樣回傳。
    限制: 無法得知個別股票最後一根日 K 是不是今天 — 盤中暫停交易的個股會被高估。
    """
    if raw_ratio is None:
        return None
    try:
        raw = float(raw_ratio)
    except (TypeError, ValueError):
        return raw_ratio
    if pd.isna(raw):
        return raw_ratio
    frac = time_adjust_fraction(market, now, check_holiday) if _frac == -1.0 else _frac
    return raw if not frac else raw / frac


_ADJUSTED_ATTR = "vol_ratio_time_adjusted"


def adjust_ratio_column(df: pd.DataFrame, market: str = "TW", col: str = "量比",
                        now: Optional[dt.datetime] = None) -> pd.DataFrame:
    """對 DataFrame 的量比欄做 adjust_full_day_ratio (回新 df, 不改原物件).

    用 df.attrs[vol_ratio_time_adjusted] 標記, 同一份資料重複呼叫不會被校正兩次。
    (若日後 sector_pulse 本身改用時間校正量比, 請在它回傳的 df 設這個 attr, 這裡就自動略過。)
    """
    if df is None or df.empty or col not in df.columns:
        return df
    if df.attrs.get(_ADJUSTED_ATTR):
        return df
    out = df.copy()
    frac = time_adjust_fraction(market, now)
    if frac:
        def _adj(v):
            r = adjust_full_day_ratio(v, market, _frac=frac)
            return round(float(r), 2) if isinstance(r, (int, float)) and not pd.isna(r) else v
        out[col] = out[col].apply(_adj)
    out.attrs[_ADJUSTED_ATTR] = True
    return out
