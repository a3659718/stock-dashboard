"""
backtest_upside.py
upside_screener 的 walk-forward 回測模組.

設計重點:
  - 避免 look-ahead bias: 在 "as of date T" 跑訊號時, 只用 T 以前的資料
  - 支援可選的 chip_provider (callable) — 沒提供時跑「純技術面」回測
  - 對每個 pick 計算多個 holding period 的 forward return
  - 與大盤 (TWII / 自訂 benchmark) 比較算 alpha

對外接口:
    backtest(
        prices: Dict[str, pd.DataFrame],   # {sid: daily_df with date/open/high/low/close/volume}
        names: Dict[str, str],
        start_date: str, end_date: str,
        hold_days: List[int] = [5, 10, 20],
        chip_provider: Optional[Callable[[str, date], dict]] = None,
        benchmark_df: Optional[pd.DataFrame] = None,
        rescan_every: int = 5,             # 每幾個交易日重新掃一次 (5 = 每週一次)
    ) -> dict

回傳:
    {
        'picks_df': DataFrame,    # 所有 picks + forward returns
        'summary': DataFrame,     # 三類 × 三 holding period 的彙整指標
        'meta': dict,
    }
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

import indicators as ind


# ---------------------------------------------------------------------------
# 條件參數 — 與 upside_screener 一致, 但允許 backtest 覆蓋以做敏感度分析
# ---------------------------------------------------------------------------
EARLY_PARAMS = {
    "max_pct_from_52w_low": 30.0,
    "min_pct_from_52w_low": 5.0,
    "max_5d_pct": 8.0,
    "max_20d_pct": 15.0,
    "min_chip_health": 55,
    "require_above_ma20": True,
}

MOMENTUM_PARAMS = {
    "max_pct_from_52w_high": -8.0,
    "min_5d_pct": 3.0,
    "max_5d_pct": 15.0,
    "min_today_pct": -1.0,
    "max_today_pct": 6.0,
    "max_rsi": 75.0,
    "min_rsi": 55.0,
    "min_chip_health": 60,
    "require_ma_bullish_alignment": True,
}

REVERSAL_PARAMS = {
    "max_20d_pct": -8.0,
    "min_60d_pct": -40.0,
    "min_today_pct": 0.5,
    "min_vol_ratio_today": 1.3,
}


# ---------------------------------------------------------------------------
# 在 "as of date" 計算 features (避免 look-ahead)
# ---------------------------------------------------------------------------
def _compute_features_at(sid: str, name: str,
                          daily_full: pd.DataFrame, as_of: pd.Timestamp,
                          chip_data: Optional[Dict] = None) -> Optional[Dict]:
    """切到 as_of 當天為止的資料, 計算 features."""
    if daily_full is None or daily_full.empty:
        return None
    daily = daily_full[daily_full["date"] <= as_of]
    if len(daily) < 30:
        return None
    try:
        c = daily["close"].astype(float).reset_index(drop=True)
        h = daily["high"].astype(float).reset_index(drop=True)
        l = daily["low"].astype(float).reset_index(drop=True)
        v = daily["volume"].astype(float).reset_index(drop=True)

        cur = float(c.iloc[-1])
        prev = float(c.iloc[-2])

        # MA
        ma20 = c.rolling(20).mean().iloc[-1] if len(c) >= 20 else np.nan
        ma60 = c.rolling(60).mean().iloc[-1] if len(c) >= 60 else np.nan

        # 動能
        today_pct = (cur / prev - 1) * 100 if prev > 0 else 0
        five_pct = (cur / float(c.iloc[-6]) - 1) * 100 if len(c) >= 6 else None
        twenty_pct = (cur / float(c.iloc[-21]) - 1) * 100 if len(c) >= 21 else None
        sixty_pct = (cur / float(c.iloc[-61]) - 1) * 100 if len(c) >= 61 else None

        # 52w
        hi52, lo52, pct_hi, pct_lo = ind.distance_from_52w(c, window=252)

        # 量能
        avg5_vol = float(v.iloc[-6:-1].mean()) if len(v) >= 6 else None
        vol_ratio_today = (float(v.iloc[-1]) / avg5_vol) if (avg5_vol and avg5_vol > 0) else None

        # 指標
        rsi_s = ind.rsi(c, 14)
        rsi_now = float(rsi_s.iloc[-1]) if not rsi_s.empty else None
        bb_mid, bb_up, bb_lo, bb_w = ind.bollinger_bands(c, 20, 2.0)
        bb_squeeze = ind.is_bb_squeeze(bb_w, lookback=60, percentile=25)
        ma_bull = ind.ma_alignment_bullish(c, periods=(5, 10, 20, 60))
        rsi_bot_div = ind.rsi_bottom_divergence(c, rsi_s, lookback=30, pivot_window=3)

        # ATR levels
        atr_s = ind.atr(h, l, c, 14)
        atr_now = float(atr_s.iloc[-1]) if not atr_s.empty and not pd.isna(atr_s.iloc[-1]) else None
        levels = ind.atr_based_levels(h, l, c) if atr_now else {}

        # 籌碼: 有提供就用, 沒提供時用 neutral defaults
        chip_health = (chip_data or {}).get("chip_health", 50)
        chip_consensus_dir = (chip_data or {}).get("chip_consensus", "neutral")
        chip_consensus_score = (chip_data or {}).get("chip_consensus_score", 0)
        it_consec = (chip_data or {}).get("it_consecutive", 0)
        fi_consec = (chip_data or {}).get("fi_consecutive", 0)
        it_5d_net = (chip_data or {}).get("it_5d_net", 0)
        fi_5d_net = (chip_data or {}).get("fi_5d_net", 0)

        return {
            "stock_id": sid, "name": name,
            "as_of": as_of,
            "current": cur, "ma20": ma20, "ma60": ma60,
            "today_pct": today_pct, "five_pct": five_pct,
            "twenty_pct": twenty_pct, "sixty_pct": sixty_pct,
            "pct_from_52w_high": pct_hi, "pct_from_52w_low": pct_lo,
            "rsi": rsi_now, "vol_ratio_today": vol_ratio_today,
            "bb_squeeze": bb_squeeze,
            "ma_bullish_alignment": ma_bull,
            "rsi_bottom_divergence": rsi_bot_div,
            "chip_health": chip_health,
            "chip_consensus": chip_consensus_dir,
            "chip_consensus_score": chip_consensus_score,
            "it_consecutive": it_consec, "fi_consecutive": fi_consec,
            "it_5d_net": it_5d_net, "fi_5d_net": fi_5d_net,
            "levels": levels,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 三類 check (回測版, 條件比上線版稍寬鬆以增加樣本數)
# ---------------------------------------------------------------------------
def _check_early(f: Dict, chip_required: bool) -> Optional[float]:
    p = EARLY_PARAMS
    if f.get("pct_from_52w_low") is None: return None
    if f["pct_from_52w_low"] > p["max_pct_from_52w_low"]: return None
    if f["pct_from_52w_low"] < p["min_pct_from_52w_low"]: return None
    if (f.get("five_pct") or 0) > p["max_5d_pct"]: return None
    if (f.get("twenty_pct") or 0) > p["max_20d_pct"]: return None
    if p["require_above_ma20"] and not (f.get("ma20") and f["current"] > f["ma20"]): return None
    if chip_required and (f.get("chip_health") or 0) < p["min_chip_health"]: return None
    # 分數
    score = 50
    if f.get("bb_squeeze"): score += 15
    if (f.get("chip_health") or 0) >= 70: score += 10
    if (f.get("chip_consensus") == "bullish" and (f.get("chip_consensus_score") or 0) >= 2): score += 10
    if (f.get("it_consecutive") or 0) >= 3: score += 5
    return min(100, score)


def _check_momentum(f: Dict, chip_required: bool) -> Optional[float]:
    p = MOMENTUM_PARAMS
    pct_hi = f.get("pct_from_52w_high")
    if pct_hi is None or pct_hi > p["max_pct_from_52w_high"]: return None
    fp = f.get("five_pct")
    if fp is None or fp < p["min_5d_pct"] or fp > p["max_5d_pct"]: return None
    tp = f.get("today_pct")
    if tp is None or tp < p["min_today_pct"] or tp > p["max_today_pct"]: return None
    rsi_v = f.get("rsi")
    if rsi_v is None or rsi_v > p["max_rsi"] or rsi_v < p["min_rsi"]: return None
    if p["require_ma_bullish_alignment"] and not f.get("ma_bullish_alignment"): return None
    if chip_required and (f.get("chip_health") or 0) < p["min_chip_health"]: return None
    score = 50
    if f.get("ma_bullish_alignment"): score += 10
    if (f.get("twenty_pct") or 0) >= 5: score += 5
    if (f.get("chip_health") or 0) >= 65: score += 10
    if (f.get("fi_consecutive") or 0) >= 3: score += 10
    if (f.get("it_consecutive") or 0) >= 3: score += 10
    return min(100, score)


def _check_reversal(f: Dict, chip_required: bool) -> Optional[float]:
    p = REVERSAL_PARAMS
    tp20 = f.get("twenty_pct")
    if tp20 is None or tp20 > p["max_20d_pct"]: return None
    sp = f.get("sixty_pct")
    if sp is not None and sp < p["min_60d_pct"]: return None
    if (f.get("today_pct") or 0) < p["min_today_pct"]: return None
    if (f.get("vol_ratio_today") or 0) < p["min_vol_ratio_today"]: return None
    if chip_required:
        if f.get("chip_consensus") != "bullish":
            if (f.get("it_5d_net") or 0) <= 0 and (f.get("fi_5d_net") or 0) <= 0:
                return None
    score = 50
    if f.get("rsi_bottom_divergence"): score += 20
    if (f.get("rsi") or 50) <= 40: score += 10
    if (f.get("it_5d_net") or 0) > 0: score += 10
    if (f.get("fi_5d_net") or 0) > 0: score += 10
    return min(100, score)


# ---------------------------------------------------------------------------
# Walk-forward 主回測
# ---------------------------------------------------------------------------
def backtest(prices: Dict[str, pd.DataFrame],
              names: Dict[str, str],
              start_date: str, end_date: str,
              hold_days: List[int] = [5, 10, 20],
              chip_provider: Optional[Callable] = None,
              benchmark_df: Optional[pd.DataFrame] = None,
              rescan_every: int = 5,
              verbose: bool = False) -> Dict:
    """跑 walk-forward 回測.

    prices: {sid: DataFrame} with columns [date, open, high, low, close, volume]
    chip_provider(sid, as_of_date) -> dict or None  — None = 純技術面回測
    rescan_every: 每幾個交易日重新跑一次 (避免天天重複命中)
    """
    chip_required = chip_provider is not None
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)

    # 找出 [start, end] 內所有交易日 (取最豐富的那檔股票)
    all_dates = sorted(set().union(*[set(df["date"].tolist()) for df in prices.values() if not df.empty]))
    trading_days = [d for d in all_dates if start_ts <= d <= end_ts]
    rescan_dates = trading_days[::rescan_every]

    rows = []
    n_scans = 0
    for as_of in rescan_dates:
        n_scans += 1
        if verbose and n_scans % 5 == 0:
            print(f"  [{n_scans}/{len(rescan_dates)}] scanning {as_of.date()}…", flush=True)
        for sid, df in prices.items():
            chip = chip_provider(sid, as_of) if chip_provider else None
            f = _compute_features_at(sid, names.get(sid, ""), df, as_of, chip)
            if not f:
                continue
            # 跑三類
            for cat, check_fn in [("early_stage", _check_early),
                                    ("momentum",    _check_momentum),
                                    ("reversal",    _check_reversal)]:
                sc = check_fn(f, chip_required)
                if sc is None:
                    continue
                # 計算 forward returns
                entry_price = float(f["current"])
                fwd = {}
                df_after = df[df["date"] > as_of].reset_index(drop=True)
                for hd in hold_days:
                    if len(df_after) >= hd:
                        exit_price = float(df_after.iloc[hd - 1]["close"])
                        fwd[f"ret_{hd}d"] = (exit_price / entry_price - 1) * 100
                        # 期間最高/最低 (給 MAE / MFE 用)
                        period = df_after.iloc[:hd]
                        fwd[f"max_{hd}d"] = (float(period["high"].max()) / entry_price - 1) * 100
                        fwd[f"min_{hd}d"] = (float(period["low"].min()) / entry_price - 1) * 100
                    else:
                        fwd[f"ret_{hd}d"] = None
                        fwd[f"max_{hd}d"] = None
                        fwd[f"min_{hd}d"] = None

                # 是否觸碰 ATR 目標 / 停損
                levels = f.get("levels") or {}
                if levels and len(df_after) > 0:
                    target = levels.get("target")
                    stop = levels.get("stop")
                    hit_target_at = None
                    hit_stop_at = None
                    for i, row in df_after.head(20).iterrows():
                        if target and not hit_target_at and float(row["high"]) >= target:
                            hit_target_at = i + 1
                        if stop and not hit_stop_at and float(row["low"]) <= stop:
                            hit_stop_at = i + 1
                        if hit_target_at and hit_stop_at:
                            break
                    fwd["hit_target_in_20d"] = hit_target_at
                    fwd["hit_stop_in_20d"] = hit_stop_at

                rows.append({
                    "as_of": as_of, "stock_id": sid, "name": f["name"],
                    "category": cat, "score": sc,
                    "entry_price": round(entry_price, 2),
                    "pct_from_52w_high": round(f.get("pct_from_52w_high") or 0, 2),
                    "pct_from_52w_low": round(f.get("pct_from_52w_low") or 0, 2),
                    "rsi": round(f.get("rsi") or 0, 1),
                    "vol_ratio": round(f.get("vol_ratio_today") or 0, 2),
                    "atr_pct": round((levels.get("atr_pct") or 0), 2),
                    "rr_target": levels.get("rr"),
                    **fwd,
                })

    picks_df = pd.DataFrame(rows)

    # benchmark forward return (TWII or 自訂)
    bench_rets = {hd: None for hd in hold_days}
    if benchmark_df is not None and not benchmark_df.empty:
        bdf = benchmark_df.sort_values("date").reset_index(drop=True)
        # 對每一個 rescan_date 算 forward return, 然後取平均做 baseline
        bench_returns_per_scan = {hd: [] for hd in hold_days}
        for as_of in rescan_dates:
            b_after = bdf[bdf["date"] > as_of].reset_index(drop=True)
            entry = bdf[bdf["date"] <= as_of]
            if entry.empty:
                continue
            entry_p = float(entry.iloc[-1]["close"])
            for hd in hold_days:
                if len(b_after) >= hd:
                    r = (float(b_after.iloc[hd - 1]["close"]) / entry_p - 1) * 100
                    bench_returns_per_scan[hd].append(r)
        for hd in hold_days:
            arr = bench_returns_per_scan[hd]
            bench_rets[hd] = round(float(np.mean(arr)), 2) if arr else None

    # 彙整: 三類 × 三 holding period
    summary_rows = []
    for cat in ["early_stage", "momentum", "reversal"]:
        sub = picks_df[picks_df["category"] == cat] if not picks_df.empty else pd.DataFrame()
        row = {"category": cat, "n_picks": len(sub)}
        for hd in hold_days:
            col = f"ret_{hd}d"
            valid = sub[col].dropna() if not sub.empty and col in sub.columns else pd.Series(dtype=float)
            if len(valid) > 0:
                row[f"mean_ret_{hd}d"] = round(float(valid.mean()), 2)
                row[f"median_ret_{hd}d"] = round(float(valid.median()), 2)
                row[f"win_rate_{hd}d"] = round(float((valid > 0).mean() * 100), 1)
                row[f"alpha_vs_bench_{hd}d"] = (
                    round(float(valid.mean() - (bench_rets[hd] or 0)), 2)
                    if bench_rets[hd] is not None else None
                )
                row[f"max_dd_{hd}d"] = round(float(sub[f"min_{hd}d"].dropna().mean()), 2) if f"min_{hd}d" in sub.columns else None
                row[f"max_run_{hd}d"] = round(float(sub[f"max_{hd}d"].dropna().mean()), 2) if f"max_{hd}d" in sub.columns else None
            else:
                row[f"mean_ret_{hd}d"] = None
                row[f"median_ret_{hd}d"] = None
                row[f"win_rate_{hd}d"] = None
                row[f"alpha_vs_bench_{hd}d"] = None
                row[f"max_dd_{hd}d"] = None
                row[f"max_run_{hd}d"] = None
        summary_rows.append(row)
    summary_df = pd.DataFrame(summary_rows)

    return {
        "picks_df": picks_df,
        "summary": summary_df,
        "benchmark_returns": bench_rets,
        "meta": {
            "n_stocks": len(prices),
            "n_scans": n_scans,
            "rescan_every": rescan_every,
            "start": start_date, "end": end_date,
            "chip_required": chip_required,
            "hold_days": hold_days,
        }
    }
