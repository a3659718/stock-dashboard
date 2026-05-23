"""
indicators.py
共用技術指標 & 模式偵測函式庫 — 給 upside_screener / tw_screener / actionable_picks 共用.

設計原則:
  1. 純函式 (沒副作用), 輸入是 pd.Series / DataFrame, 輸出也是 Series / scalar.
  2. 不依賴 streamlit / data_sources, 純 pandas + numpy.
  3. 對短序列 / NaN / 0 都有防呆, 不會 throw, 缺資料時回 None / NaN.
  4. 所有日線指標都假設 input 已排序 (date asc).

主要函式:
  - atr(high, low, close, period=14) -> Series
  - rsi(close, period=14) -> Series
  - bollinger_bands(close, period=20, k=2) -> (mid, upper, lower, width_pct)
  - distance_from_52w_high(close) / from_52w_low(close) -> (pct_to_high, pct_to_low)
  - is_bb_squeeze(width_pct, lookback=60) -> bool
  - find_local_extremes(series, window=5) -> (highs_idx, lows_idx)
  - rsi_bottom_divergence(close, rsi_s, lookback=30) -> bool
  - rsi_top_divergence(close, rsi_s, lookback=30) -> bool
  - ma_alignment_bullish(close, periods=(5,10,20,60)) -> bool
  - volume_expansion(volume, recent=5, base=20, ratio=1.2) -> bool
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# ATR — Average True Range (14)
# ---------------------------------------------------------------------------
def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ATR. 若資料不足 period+1 筆, 回 NaN-filled Series."""
    h = high.astype(float)
    l = low.astype(float)
    c = close.astype(float)
    prev_close = c.shift(1)
    tr = pd.concat([
        (h - l),
        (h - prev_close).abs(),
        (l - prev_close).abs(),
    ], axis=1).max(axis=1)
    # Wilder smoothing = EMA with alpha = 1/period
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ATR 相對股價的百分比 (動態波動率, 適合做停損 sizing)."""
    a = atr(high, low, close, period)
    c = close.astype(float)
    return (a / c) * 100


# ---------------------------------------------------------------------------
# RSI — Relative Strength Index (14)
# ---------------------------------------------------------------------------
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """經典 Wilder RSI(14). 短序列回 NaN."""
    c = close.astype(float)
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)  # 初期 NaN 視為中性


# ---------------------------------------------------------------------------
# Bollinger Bands (20, 2)
# ---------------------------------------------------------------------------
def bollinger_bands(close: pd.Series, period: int = 20, k: float = 2.0
                    ) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """回傳 (mid, upper, lower, width_pct).
    width_pct = (upper - lower) / mid * 100, 用來判斷 squeeze 與 expansion.
    """
    c = close.astype(float)
    mid = c.rolling(period, min_periods=period).mean()
    std = c.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    width_pct = (upper - lower) / mid * 100
    return mid, upper, lower, width_pct


def is_bb_squeeze(width_pct: pd.Series, lookback: int = 60, percentile: float = 20.0) -> bool:
    """判斷目前 BB width 是否落在過去 lookback 日的 percentile% 之下 (= 壓縮狀態).
    壓縮後通常會出現大波動 (volatility expansion), 是潛在突破訊號.
    """
    if width_pct is None or len(width_pct.dropna()) < lookback:
        return False
    recent = width_pct.dropna().iloc[-lookback:]
    threshold = np.percentile(recent.values, percentile)
    return bool(width_pct.iloc[-1] <= threshold)


# ---------------------------------------------------------------------------
# 52 週 (252 個交易日) 高低點相對位置
# ---------------------------------------------------------------------------
def distance_from_52w(close: pd.Series, window: int = 252
                       ) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """回傳 (52w_high, 52w_low, pct_from_high, pct_from_low).
    pct_from_high 為負值 (還差幾%才到高); pct_from_low 為正值 (已離低點漲了幾%).
    若資料 < window 筆, 用全部可用資料.
    """
    if close is None or len(close) == 0:
        return None, None, None, None
    c = close.astype(float)
    w = c.tail(min(window, len(c)))
    hi = float(w.max())
    lo = float(w.min())
    cur = float(c.iloc[-1])
    pct_from_hi = (cur / hi - 1) * 100 if hi > 0 else None
    pct_from_lo = (cur / lo - 1) * 100 if lo > 0 else None
    return hi, lo, pct_from_hi, pct_from_lo


# ---------------------------------------------------------------------------
# 局部高低點 (pivot points) — 用於背離 / 形態識別
# ---------------------------------------------------------------------------
def find_local_extremes(series: pd.Series, window: int = 5
                         ) -> Tuple[list, list]:
    """找 series 的局部高點與低點 index. window 是「左右各看 N 根」的判定範圍.
    回傳 (highs_idx, lows_idx).
    """
    s = series.astype(float).dropna().reset_index(drop=True)
    highs, lows = [], []
    n = len(s)
    if n < 2 * window + 1:
        return highs, lows
    for i in range(window, n - window):
        left = s.iloc[i - window:i]
        right = s.iloc[i + 1:i + 1 + window]
        v = s.iloc[i]
        if v > left.max() and v > right.max():
            highs.append(i)
        elif v < left.min() and v < right.min():
            lows.append(i)
    return highs, lows


# ---------------------------------------------------------------------------
# RSI 背離 — 底背離 (價創低 RSI 不創低) 是強進場訊號
# ---------------------------------------------------------------------------
def rsi_bottom_divergence(close: pd.Series, rsi_s: pd.Series,
                           lookback: int = 30, pivot_window: int = 3) -> bool:
    """偵測底背離: 近 N 日內 2 個局部低點, 第二個價低於第一個但 RSI 高於第一個.
    強進場訊號 (代表下跌動能耗盡).
    """
    if close is None or rsi_s is None:
        return False
    if len(close) < lookback or len(rsi_s) < lookback:
        return False
    c_recent = close.tail(lookback).reset_index(drop=True)
    r_recent = rsi_s.tail(lookback).reset_index(drop=True)
    _, c_lows = find_local_extremes(c_recent, window=pivot_window)
    if len(c_lows) < 2:
        return False
    i1, i2 = c_lows[-2], c_lows[-1]
    price_lower = c_recent.iloc[i2] < c_recent.iloc[i1]
    rsi_higher = r_recent.iloc[i2] > r_recent.iloc[i1]
    return bool(price_lower and rsi_higher)


def rsi_top_divergence(close: pd.Series, rsi_s: pd.Series,
                        lookback: int = 30, pivot_window: int = 3) -> bool:
    """偵測頂背離 (價創高 RSI 不創高) — 警示訊號, 上漲動能衰竭."""
    if close is None or rsi_s is None:
        return False
    if len(close) < lookback or len(rsi_s) < lookback:
        return False
    c_recent = close.tail(lookback).reset_index(drop=True)
    r_recent = rsi_s.tail(lookback).reset_index(drop=True)
    c_highs, _ = find_local_extremes(c_recent, window=pivot_window)
    if len(c_highs) < 2:
        return False
    i1, i2 = c_highs[-2], c_highs[-1]
    price_higher = c_recent.iloc[i2] > c_recent.iloc[i1]
    rsi_lower = r_recent.iloc[i2] < r_recent.iloc[i1]
    return bool(price_higher and rsi_lower)


# ---------------------------------------------------------------------------
# 移動平均多頭排列
# ---------------------------------------------------------------------------
def ma_alignment_bullish(close: pd.Series,
                          periods: Tuple[int, ...] = (5, 10, 20, 60)) -> bool:
    """檢查短期 > 中期 > 長期 MA 是否多頭排列 (price > MA1 > MA2 > MA3 > ...)."""
    c = close.astype(float)
    if len(c) < max(periods):
        return False
    mas = [c.rolling(p).mean().iloc[-1] for p in periods]
    if any(pd.isna(v) for v in mas):
        return False
    cur = c.iloc[-1]
    # price 應 > 第一個 MA, MA 應該依序遞減
    if cur < mas[0]:
        return False
    for i in range(len(mas) - 1):
        if mas[i] < mas[i + 1]:
            return False
    return True


def ma_alignment_bearish(close: pd.Series,
                          periods: Tuple[int, ...] = (5, 10, 20, 60)) -> bool:
    """空頭排列."""
    c = close.astype(float)
    if len(c) < max(periods):
        return False
    mas = [c.rolling(p).mean().iloc[-1] for p in periods]
    if any(pd.isna(v) for v in mas):
        return False
    cur = c.iloc[-1]
    if cur > mas[0]:
        return False
    for i in range(len(mas) - 1):
        if mas[i] > mas[i + 1]:
            return False
    return True


# ---------------------------------------------------------------------------
# 量能展開 / 萎縮
# ---------------------------------------------------------------------------
def volume_expansion(volume: pd.Series, recent: int = 5, base: int = 20,
                      ratio_threshold: float = 1.2) -> Tuple[bool, Optional[float]]:
    """近 recent 日均量 vs 過去 base 日均量 比值 > threshold 視為展開.
    回傳 (is_expanding, ratio).
    """
    if volume is None or len(volume) < base + recent:
        return False, None
    v = volume.astype(float)
    recent_avg = float(v.iloc[-recent:].mean())
    base_avg = float(v.iloc[-(base + recent):-recent].mean())
    if base_avg <= 0:
        return False, None
    ratio = recent_avg / base_avg
    return ratio >= ratio_threshold, round(ratio, 2)


def volume_dryup(volume: pd.Series, recent: int = 5, base: int = 20,
                  ratio_threshold: float = 0.6) -> Tuple[bool, Optional[float]]:
    """近 recent 日量縮到過去 base 日均量的 ratio_threshold 以下 = 籌碼沉澱.
    起漲前的 setup, 配合放量爆發訊號使用.
    """
    if volume is None or len(volume) < base + recent:
        return False, None
    v = volume.astype(float)
    recent_avg = float(v.iloc[-recent:].mean())
    base_avg = float(v.iloc[-(base + recent):-recent].mean())
    if base_avg <= 0:
        return False, None
    ratio = recent_avg / base_avg
    return ratio <= ratio_threshold, round(ratio, 2)


# ---------------------------------------------------------------------------
# ATR-based 動態進場 / 目標 / 停損 (修正 B8)
# ---------------------------------------------------------------------------
def atr_based_levels(high: pd.Series, low: pd.Series, close: pd.Series,
                      entry_price: Optional[float] = None,
                      stop_atr_mult: float = 1.5,
                      target_atr_mult: float = 3.0,
                      atr_period: int = 14) -> Optional[dict]:
    """用 ATR 計算動態進場區間 / 停損 / 目標, R:R 由 mult 比決定 (預設 1:2).
    高波動股的停損會自動拉寬, 低波動股的目標會自動收窄, 比固定 % 合理.

    回傳 {entry_low, entry_high, stop, target, atr, atr_pct, rr} 或 None.
    """
    if close is None or len(close) < atr_period + 1:
        return None
    a = atr(high, low, close, atr_period)
    if a is None or a.empty or pd.isna(a.iloc[-1]):
        return None
    a_val = float(a.iloc[-1])
    cur = float(close.iloc[-1]) if entry_price is None else float(entry_price)
    if cur <= 0 or a_val <= 0:
        return None
    # 進場區間: 現價 ± 0.3 ATR (對短線足夠寬, 避開盤中雜訊)
    entry_low = round(cur - 0.3 * a_val, 2)
    entry_high = round(cur + 0.3 * a_val, 2)
    stop = round(cur - stop_atr_mult * a_val, 2)
    target = round(cur + target_atr_mult * a_val, 2)
    rr = round(target_atr_mult / stop_atr_mult, 2)
    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "target": target,
        "atr": round(a_val, 3),
        "atr_pct": round(a_val / cur * 100, 2),
        "rr": rr,
    }


# ---------------------------------------------------------------------------
# 跌深反彈訊號 (oversold bounce setup)
# ---------------------------------------------------------------------------
def is_oversold_bounce(close: pd.Series, low: pd.Series,
                        rsi_s: pd.Series, lookback: int = 60,
                        rsi_threshold: float = 35.0,
                        recovery_pct: float = 2.0) -> Tuple[bool, dict]:
    """過去 lookback 內曾觸及 RSI < rsi_threshold (超賣),
    且最新 RSI 已回升, 同時最新收盤已 > 過去 5 日最低點 + recovery_pct%.
    """
    if close is None or len(close) < lookback:
        return False, {}
    if rsi_s is None or len(rsi_s) < lookback:
        return False, {}
    r_recent = rsi_s.tail(lookback)
    c_recent = close.tail(lookback)
    if not (r_recent.min() < rsi_threshold):
        return False, {}
    rsi_now = float(r_recent.iloc[-1])
    if rsi_now <= rsi_threshold:
        return False, {}  # 還在超賣區, 等再強一點
    recent_low = float(low.tail(5).min()) if low is not None else float(c_recent.tail(5).min())
    cur = float(c_recent.iloc[-1])
    bounce_pct = (cur / recent_low - 1) * 100 if recent_low > 0 else 0
    return bounce_pct >= recovery_pct, {
        "rsi_min": round(float(r_recent.min()), 1),
        "rsi_now": round(rsi_now, 1),
        "bounce_from_low_pct": round(bounce_pct, 2),
    }


# ===========================================================================
# 美股爆發股專用指標 (us_upside_screener 用)
# ===========================================================================

# ---------------------------------------------------------------------------
# 距離 All-Time-High (ATH) — 用全部歷史而非 52w
# ---------------------------------------------------------------------------
def distance_from_ath(close: pd.Series) -> Tuple[Optional[float], Optional[float]]:
    """回傳 (ath, pct_from_ath). pct_from_ath 為 0~負值.
    距 ATH < 5% 且量縮整理是經典爆發前夕 (Minervini stage 2 setup).
    """
    if close is None or len(close) == 0:
        return None, None
    c = close.astype(float)
    ath = float(c.max())
    cur = float(c.iloc[-1])
    pct = (cur / ath - 1) * 100 if ath > 0 else None
    return ath, pct


# ---------------------------------------------------------------------------
# Consolidation / Base detection
# ---------------------------------------------------------------------------
def is_tight_consolidation(close: pd.Series, lookback: int = 20,
                            max_range_pct: float = 8.0) -> Tuple[bool, Optional[float]]:
    """近 N 日股價在 max_range_pct% 內整理 = tight base.
    經典「VCP (Volatility Contraction Pattern)」前奏.
    回傳 (is_tight, actual_range_pct).
    """
    if close is None or len(close) < lookback:
        return False, None
    c = close.astype(float).tail(lookback)
    hi = float(c.max())
    lo = float(c.min())
    if lo <= 0:
        return False, None
    range_pct = (hi / lo - 1) * 100
    return range_pct <= max_range_pct, round(range_pct, 2)


def base_depth_pct(close: pd.Series, base_window: int = 30) -> Optional[float]:
    """base 期間的深度 (%) — 從 base 高點到 base 低點的回檔幅度.
    健康 base 深度 5-15%, 過深 (> 30%) 通常是趨勢破壞."""
    if close is None or len(close) < base_window:
        return None
    c = close.astype(float).tail(base_window)
    hi = float(c.max())
    lo = float(c.min())
    if hi <= 0:
        return None
    return round((1 - lo / hi) * 100, 2)


# ---------------------------------------------------------------------------
# Momentum acceleration — 短期動能 > 長期動能 = 動能加速
# ---------------------------------------------------------------------------
def momentum_acceleration(close: pd.Series) -> Tuple[bool, dict]:
    """偵測 5d > 10d (annualized) > 20d > 60d 的動能加速.
    這是「爆發股」最關鍵的訊號 — 上漲速率在變快.
    """
    if close is None or len(close) < 65:
        return False, {}
    c = close.astype(float)
    cur = float(c.iloc[-1])
    # 各期間 annualized return (% per day)
    rates = {}
    for w in (5, 10, 20, 60):
        try:
            prev = float(c.iloc[-(w + 1)])
            rates[w] = (cur / prev - 1) * 100 / w  # avg % per day
        except (IndexError, ZeroDivisionError):
            return False, {}
    # 加速 = 短期速率 > 中期速率 > 長期速率
    accel = rates[5] > rates[10] > rates[20] > rates[60] > 0
    return accel, {f"rate_{k}d": round(v, 3) for k, v in rates.items()}


# ---------------------------------------------------------------------------
# RVOL (Relative Volume) — 標準算法 + 較嚴格的門檻
# ---------------------------------------------------------------------------
def rvol(volume: pd.Series, lookback: int = 30) -> Optional[float]:
    """Relative Volume = 今日量 / 過去 N 日均量 (不含今日).
    爆發股通常 RVOL ≥ 2-3.
    """
    if volume is None or len(volume) < lookback + 1:
        return None
    v = volume.astype(float)
    today = float(v.iloc[-1])
    base = float(v.iloc[-(lookback + 1):-1].mean())
    if base <= 0:
        return None
    return round(today / base, 2)


# ---------------------------------------------------------------------------
# 52w high breakout — 經典 Minervini setup
# ---------------------------------------------------------------------------
def is_52w_high_breakout(close: pd.Series, window: int = 252,
                          breakout_tolerance: float = 1.0) -> Tuple[bool, dict]:
    """今天收盤 >= 52w high * (1 - tolerance%).
    tolerance=1 表示「接近 52w 高 (在 1% 內) 且突破」.
    """
    if close is None or len(close) < min(60, window):
        return False, {}
    c = close.astype(float)
    w = c.tail(min(window, len(c)))
    hi = float(w.max())
    cur = float(c.iloc[-1])
    if hi <= 0:
        return False, {}
    # 是新高 OR 在 tolerance 內
    if cur >= hi * (1 - breakout_tolerance / 100):
        # 進一步確認: 必須剛突破 (前一天還沒到)
        prev = float(c.iloc[-2]) if len(c) >= 2 else cur
        is_fresh = prev < hi * (1 - breakout_tolerance / 100)
        return True, {
            "52w_high": round(hi, 2), "pct_from_high": round((cur / hi - 1) * 100, 2),
            "is_fresh_breakout": is_fresh,
        }
    return False, {}


# ---------------------------------------------------------------------------
# Stage 2 uptrend 確認 (Minervini 多重 MA 條件)
# ---------------------------------------------------------------------------
def is_minervini_stage2(close: pd.Series) -> Tuple[bool, dict]:
    """Minervini Stage 2 trend template:
      1. 現價 > MA150 且 > MA200
      2. MA150 > MA200
      3. MA200 趨勢向上 (至少 1 個月)
      4. MA50 > MA150 > MA200 (短中長 MA 多頭排列)
      5. 現價 > MA50
      6. 現價 距 52w 高 ≤ 25%
      7. 現價 距 52w 低 ≥ 30%

    通過 ≥ 6 條視為 stage 2 上升趨勢.
    """
    if close is None or len(close) < 200:
        return False, {}
    c = close.astype(float)
    cur = float(c.iloc[-1])
    ma50 = float(c.rolling(50).mean().iloc[-1])
    ma150 = float(c.rolling(150).mean().iloc[-1])
    ma200 = float(c.rolling(200).mean().iloc[-1])
    ma200_1m_ago = float(c.rolling(200).mean().iloc[-22]) if len(c) >= 222 else None
    hi52 = float(c.tail(252).max()) if len(c) >= 60 else cur
    lo52 = float(c.tail(252).min()) if len(c) >= 60 else cur

    checks = [
        cur > ma150 and cur > ma200,                            # 1
        ma150 > ma200,                                            # 2
        ma200_1m_ago is None or ma200 > ma200_1m_ago,            # 3
        ma50 > ma150 > ma200,                                     # 4
        cur > ma50,                                                # 5
        cur >= hi52 * 0.75,                                       # 6 (距高 ≤ 25%)
        lo52 > 0 and cur >= lo52 * 1.30,                         # 7 (距低 ≥ 30%)
    ]
    passed = sum(checks)
    return passed >= 6, {
        "passed_checks": passed, "total_checks": len(checks),
        "ma50": round(ma50, 2), "ma150": round(ma150, 2), "ma200": round(ma200, 2),
        "pct_from_52w_high": round((cur / hi52 - 1) * 100, 2) if hi52 > 0 else None,
        "pct_from_52w_low": round((cur / lo52 - 1) * 100, 2) if lo52 > 0 else None,
    }


# ---------------------------------------------------------------------------
# Volume-Price-Trend (VPT) 簡化版 — 確認量價同步
# ---------------------------------------------------------------------------
def vpt_uptrend(close: pd.Series, volume: pd.Series, lookback: int = 20) -> bool:
    """VPT 累積近期斜率為正 = 量價同向放大. 用來區分「健康放量」vs「出貨放量」."""
    if close is None or volume is None or len(close) < lookback + 1:
        return False
    c = close.astype(float)
    v = volume.astype(float)
    pct = c.pct_change().fillna(0)
    vpt = (pct * v).cumsum()
    if len(vpt) < lookback:
        return False
    recent = vpt.iloc[-lookback:]
    # 線性回歸斜率 (簡化用 first-last 比較)
    return bool(recent.iloc[-1] > recent.iloc[0])
