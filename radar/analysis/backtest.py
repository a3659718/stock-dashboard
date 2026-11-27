"""
backtest.py
簡易條件回測。

對過去 N 個交易日做 walk-forward：
  - 每個 as-of 日，用「截至那一天為止」的資料跑各 condition
  - 對每個命中的股票，算 +5d / +10d / +20d 的後續漲跌幅
  - 彙整勝率、平均報酬、最大跌幅

進場規則 = 隔日開盤進場 (2026-11-27 修正, audit #1)：
  - 訊號用 T (含) 以前的日線算；T 日收盤後才知道訊號, 所以不能用 T 日收盤價進場。
  - 進場價 = T+1 開盤 (開盤缺失 / 0 才退回 T+1 收盤；沒有 T+1 K 棒則跳過該訊號)。
  - r{N}d = T+N 收盤 / 進場價 - 1 (持有天數從 T+1 起算)；max_high_20d / max_low_20d 也從進場後算。
  - ⚠️ 2026-11-27 之前跑出來的勝率 / 報酬是「T 日收盤進場」, 與現在的結果不可直接比較。

只回測 daily-only 條件 (純價量計算的)，避開法人/融資融券 (要每天打 API 太貴)：
  - break_ma             突破月/季線
  - volume_burst         量爆
  - above_ma_uptrend     站上 MA20 + 趨勢向上
  - kd_golden_cross      KD 黃金交叉
  - macd_turn_positive   MACD 翻紅
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import streamlit as st

from radar.analysis.backtest_upside import ENTRY_RULE, _next_open_entry
from radar.data import data_sources as ds
from radar.screeners import tw_screener as tw

# 可回測的條件
BACKTESTABLE_CONDITIONS = {
    "break_ma": "突破月/季線",
    "volume_burst": "量 5–10 倍均量",
    "above_ma_uptrend": "MA20 上方且趨勢向上",
    "kd_golden_cross": "KD 黃金交叉",
    "macd_turn_positive": "MACD 翻紅",
}


def _run_one_condition(cond: str, daily_slice: pd.DataFrame, params: tw.TWParams) -> List[str]:
    """跑一個條件，回傳命中 stock_id list。"""
    if cond == "break_ma":
        df = tw.screen_break_ma(daily_slice)
    elif cond == "volume_burst":
        df = tw.screen_volume_burst(daily_slice, params)
    elif cond == "above_ma_uptrend":
        df = tw.screen_above_ma_uptrend(daily_slice, params)
    elif cond == "kd_golden_cross":
        df = tw.screen_kd_golden_cross(daily_slice)
    elif cond == "macd_turn_positive":
        df = tw.screen_macd_turn_positive(daily_slice)
    else:
        return []
    return df["stock_id"].tolist() if df is not None and not df.empty else []


def _forward_returns(daily_for_stock: pd.DataFrame, as_of: pd.Timestamp,
                      horizons=(5, 10, 20)) -> Dict[str, float]:
    """訊號日 = as_of (T)；以 T+1 開盤為進場價 (隔日開盤進場)，算持有 N 天 (T+1 起算) 的報酬 (%).

    舊版用 T 日收盤當基準 — 訊號是 T 日收盤後才算得出來, 實盤不可能用 T 收盤成交 → 報酬 / 勝率被高估。
    T 日沒有 K 棒 (停牌 / 資料缺) 或沒有 T+1 K 棒 → 回傳 {} (呼叫端跳過)。
    """
    base_row = daily_for_stock[daily_for_stock["date"] == as_of]
    if base_row.empty:
        return {}
    signal_close = float(base_row["close"].iloc[0])
    after = daily_for_stock[daily_for_stock["date"] > as_of].sort_values("date").reset_index(drop=True)
    entry = _next_open_entry(after)
    if entry is None:
        return {}
    entry_date, entry_price, entry_basis = entry
    out = {
        "signal_close": round(signal_close, 2),
        "entry_date": entry_date.date(),
        "entry_price": round(entry_price, 2),
        "entry_basis": entry_basis,
    }
    for h in horizons:
        if len(after) >= h:
            future_close = float(after.iloc[h - 1]["close"])
            out[f"r{h}d"] = (future_close / entry_price - 1) * 100
        else:
            out[f"r{h}d"] = None
    # 期間內最大 drawdown / 最大上漲 — 只看進場之後的 K 棒
    # (開盤進場: T+1 整根都在進場後; 退回收盤進場: T+1 的高低點在進場前, 跳過)
    path_start = 0 if entry_basis == "open" else 1
    within = after.iloc[path_start:20]
    if not within.empty:
        out["max_high_20d"] = (within["high"].max() / entry_price - 1) * 100 if "high" in within else None
        out["max_low_20d"] = (within["low"].min() / entry_price - 1) * 100 if "low" in within else None
    return out


def run_backtest(stock_universe: List[str], conditions: List[str],
                  days_back: int = 60, params: tw.TWParams | None = None,
                  progress_cb=None) -> dict:
    """
    主入口。
    - stock_universe: 要回測的股票清單
    - conditions: 要回測的條件代號列表
    - days_back: 回測 N 個交易日
    """
    params = params or tw.TWParams()
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    end = today.strftime("%Y-%m-%d")
    # 多抓 50 天緩衝以利 MA60 計算
    start = (today - dt.timedelta(days=days_back + 150)).strftime("%Y-%m-%d")

    daily_all = ds.fetch_tw_universe_daily(tuple(stock_universe), start, end)
    if daily_all.empty:
        return {"error": "抓不到日線資料"}

    # 排序與索引
    daily_all = daily_all.sort_values(["stock_id", "date"]).reset_index(drop=True)

    # 取得交易日清單 (從整體資料抽)
    trading_days = sorted(daily_all["date"].unique())
    if len(trading_days) < 80:
        return {"error": f"資料天數不足 ({len(trading_days)} 天)"}

    # 取最後 days_back 天作為 as-of (保留之後 20 天的 forward window)
    # 即只用 [-days_back-20:-20] 區間作為 as-of
    eligible = trading_days[-(days_back + 20):-20] if len(trading_days) > days_back + 20 else trading_days[:-20]
    if len(eligible) == 0:
        return {"error": "可用 as-of 日期區間不足"}

    # 預先依 stock_id 分組以加速 forward return 計算
    by_stock = {sid: g.sort_values("date").reset_index(drop=True)
                 for sid, g in daily_all.groupby("stock_id")}

    # 結果儲存
    rows = []  # 每筆: as_of, stock_id, condition, r5d, r10d, r20d, max_high, max_low

    total = len(eligible)
    for i, as_of in enumerate(eligible):
        sliced = daily_all[daily_all["date"] <= as_of]
        for cond in conditions:
            picks = _run_one_condition(cond, sliced, params)
            for sid in picks:
                if sid not in by_stock:
                    continue
                fwd = _forward_returns(by_stock[sid], pd.Timestamp(as_of))
                if not fwd:
                    continue  # 沒有 T 日 / T+1 K 棒 → 實盤無法進場, 不計入命中
                rows.append({
                    "as_of": pd.Timestamp(as_of).date(),
                    "stock_id": sid,
                    "condition": cond,
                    **fwd,
                })
        if progress_cb:
            try:
                progress_cb(i + 1, total)
            except Exception:
                pass

    if not rows:
        return {"error": "回測期間無命中"}
    df = pd.DataFrame(rows)

    # 彙整: 每個 condition 的勝率與平均報酬
    summary_rows = []
    for cond in conditions:
        sub = df[df["condition"] == cond]
        if sub.empty:
            continue
        for h in [5, 10, 20]:
            col = f"r{h}d"
            valid = sub[col].dropna()
            if valid.empty:
                continue
            summary_rows.append({
                "condition": tw.CONDITION_LABELS.get(cond, cond),
                "horizon": f"+{h}d",
                "進場方式": ENTRY_RULE,
                "命中次數": len(valid),
                "勝率%": round(float((valid > 0).mean() * 100), 1),
                "平均%": round(float(valid.mean()), 2),
                "中位%": round(float(valid.median()), 2),
                "最大漲%": round(float(valid.max()), 2),
                "最大跌%": round(float(valid.min()), 2),
            })

    summary = pd.DataFrame(summary_rows)
    return {
        "raw": df,
        "summary": summary,
        "as_of_range": (eligible[0], eligible[-1]),
        "n_trading_days": len(eligible),
        "entry_rule": ENTRY_RULE,
    }


def run_combo_backtest(stock_universe: List[str], conditions: List[str],
                       days_back: int = 60, params: tw.TWParams | None = None) -> dict:
    """組合回測：要求多個條件同時命中才算。"""
    params = params or tw.TWParams()
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days_back + 150)).strftime("%Y-%m-%d")

    daily_all = ds.fetch_tw_universe_daily(tuple(stock_universe), start, end)
    if daily_all.empty:
        return {"error": "抓不到日線資料"}
    daily_all = daily_all.sort_values(["stock_id", "date"]).reset_index(drop=True)
    trading_days = sorted(daily_all["date"].unique())
    if len(trading_days) < 80:
        return {"error": "資料天數不足"}

    eligible = trading_days[-(days_back + 20):-20] if len(trading_days) > days_back + 20 else trading_days[:-20]
    by_stock = {sid: g.sort_values("date").reset_index(drop=True)
                 for sid, g in daily_all.groupby("stock_id")}

    rows = []
    for as_of in eligible:
        sliced = daily_all[daily_all["date"] <= as_of]
        # 對每個 condition 取 set of stock_ids，求交集
        sets = []
        for cond in conditions:
            sets.append(set(_run_one_condition(cond, sliced, params)))
        if not sets:
            continue
        combo_picks = set.intersection(*sets) if len(sets) > 1 else sets[0]
        for sid in combo_picks:
            if sid not in by_stock:
                continue
            fwd = _forward_returns(by_stock[sid], pd.Timestamp(as_of))
            if not fwd:
                continue  # 沒有 T 日 / T+1 K 棒 → 不計入
            rows.append({"as_of": pd.Timestamp(as_of).date(), "stock_id": sid, **fwd})

    if not rows:
        return {"error": "組合條件回測期間無命中"}
    df = pd.DataFrame(rows)
    summary_rows = []
    for h in [5, 10, 20]:
        col = f"r{h}d"
        valid = df[col].dropna()
        if valid.empty:
            continue
        summary_rows.append({
            "組合": " + ".join(tw.CONDITION_LABELS.get(c, c) for c in conditions),
            "horizon": f"+{h}d",
            "進場方式": ENTRY_RULE,
            "命中次數": len(valid),
            "勝率%": round(float((valid > 0).mean() * 100), 1),
            "平均%": round(float(valid.mean()), 2),
            "中位%": round(float(valid.median()), 2),
            "最大漲%": round(float(valid.max()), 2),
            "最大跌%": round(float(valid.min()), 2),
        })
    return {"raw": df, "summary": pd.DataFrame(summary_rows), "entry_rule": ENTRY_RULE}
