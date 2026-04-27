"""
tw_screener.py
台股四大條件篩選器：
  1) 突破月線 (MA20) 或 季線 (MA60)
  2) 今日成交量 >= 5 日均量的 5~10 倍
  3) 融券今日餘額 較前日增加 >= 50 張
  4) 投信近 30 日「首次」買超 (累積 buy-sell <=0 且今日 buy-sell > 0)

回傳格式: pandas.DataFrame，欄位包含 stock_id / name / market / hits (命中條件清單)
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd
import streamlit as st

import data_sources as ds


# ---------------------------------------------------------------------------
# 參數
# ---------------------------------------------------------------------------
@dataclass
class TWParams:
    vol_min_ratio: float = 5.0
    vol_max_ratio: float = 10.0
    short_inc_lots: int = 50  # 融券增加 (張)
    invtrust_lookback_days: int = 30
    use_market_data: bool = True  # 使用 FinMind 全市場 daily (省 API)


# ---------------------------------------------------------------------------
# 資料就緒判斷
# ---------------------------------------------------------------------------
def latest_trading_date(df: pd.DataFrame) -> pd.Timestamp | None:
    if df is None or df.empty:
        return None
    return df["date"].max()


def today_data_ready(df: pd.DataFrame) -> bool:
    """檢查資料是否已包含「今天」(交易日尚未盤後落地時 latest != today)."""
    today = pd.Timestamp(dt.date.today())
    last = latest_trading_date(df)
    if last is None:
        return False
    return last.normalize() == today.normalize()


# ---------------------------------------------------------------------------
# 1) 突破均線
# ---------------------------------------------------------------------------
def screen_break_ma(daily: pd.DataFrame) -> pd.DataFrame:
    """傳入全市場 daily，回傳今日剛突破 MA20 / MA60 的清單."""
    if daily.empty:
        return pd.DataFrame()
    rows = []
    for sid, g in daily.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < 65:
            continue
        c = g["close"].astype(float)
        ma20 = c.rolling(20).mean()
        ma60 = c.rolling(60).mean()
        if pd.isna(ma20.iloc[-1]) or pd.isna(ma60.iloc[-1]):
            continue
        broke_ma20 = c.iloc[-1] > ma20.iloc[-1] and c.iloc[-2] <= ma20.iloc[-2]
        broke_ma60 = c.iloc[-1] > ma60.iloc[-1] and c.iloc[-2] <= ma60.iloc[-2]
        if broke_ma20 or broke_ma60:
            tags = []
            if broke_ma20:
                tags.append("突破月線")
            if broke_ma60:
                tags.append("突破季線")
            rows.append(
                {
                    "stock_id": sid,
                    "close": float(c.iloc[-1]),
                    "ma20": float(ma20.iloc[-1]),
                    "ma60": float(ma60.iloc[-1]),
                    "break_type": " / ".join(tags),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2) 量能爆量 (5 ~ 10 倍)
# ---------------------------------------------------------------------------
def screen_volume_burst(daily: pd.DataFrame, params: TWParams) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    rows = []
    for sid, g in daily.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < 6:
            continue
        vols = g["Trading_Volume"].astype(float)
        today_vol = vols.iloc[-1]
        avg5 = vols.iloc[-6:-1].mean()  # 不含今日的近 5 日均量
        if avg5 <= 0 or pd.isna(today_vol):
            continue
        ratio = today_vol / avg5
        if params.vol_min_ratio <= ratio <= params.vol_max_ratio:
            rows.append(
                {
                    "stock_id": sid,
                    "today_volume": int(today_vol),
                    "avg5_volume": int(avg5),
                    "vol_ratio": round(float(ratio), 2),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3) 融券增加 (>= 50 張)
# ---------------------------------------------------------------------------
def screen_short_increase(margin: pd.DataFrame, params: TWParams) -> pd.DataFrame:
    if margin.empty:
        return pd.DataFrame()
    # FinMind 欄位: ShortSaleTodayBalance (張) / ShortSaleSell / ShortSaleBuy
    bal_col = None
    for cand in ["ShortSaleTodayBalance", "ShortSaleAfterBalance", "ShortSaleBalance"]:
        if cand in margin.columns:
            bal_col = cand
            break
    if bal_col is None:
        return pd.DataFrame()

    rows = []
    for sid, g in margin.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < 2:
            continue
        bal = g[bal_col].astype(float)
        diff = bal.iloc[-1] - bal.iloc[-2]
        if diff >= params.short_inc_lots:
            rows.append(
                {
                    "stock_id": sid,
                    "short_balance_today": int(bal.iloc[-1]),
                    "short_balance_prev": int(bal.iloc[-2]),
                    "short_increase": int(diff),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 4) 投信 30 日首次買超
# ---------------------------------------------------------------------------
def screen_invtrust_first_buy(inst: pd.DataFrame, params: TWParams) -> pd.DataFrame:
    if inst.empty:
        return pd.DataFrame()
    # 只保留 Investment_Trust
    df = inst[inst["name"] == "Investment_Trust"].copy()
    if df.empty:
        return pd.DataFrame()
    df["net"] = df["buy"].astype(float) - df["sell"].astype(float)

    rows = []
    last_date = df["date"].max()
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        if g["date"].max() != last_date:
            continue
        today_net = float(g[g["date"] == last_date]["net"].sum())
        prior = g[g["date"] < last_date]
        cum_prior = float(prior["net"].sum())
        if today_net > 0 and cum_prior <= 0:
            rows.append(
                {
                    "stock_id": sid,
                    "today_net_buy": int(today_net),
                    "prior_30d_cum": int(cum_prior),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 統一執行
# ---------------------------------------------------------------------------
def run_all_screens(market: str = "all", params: TWParams | None = None) -> dict:
    """跑全部四個篩選並合併結果。
    market: 'twse' | 'tpex' | 'all'
    回傳: dict(各條件 DataFrame, combined: 全部命中合併後的 DataFrame, ready: bool)
    """
    params = params or TWParams()
    info = ds.get_taiwan_stock_info()
    if market == "twse":
        info = info[info["type"] == "twse"]
    elif market == "tpex":
        info = info[info["type"] == "tpex"]

    today = dt.date.today()
    start = (today - dt.timedelta(days=120)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    daily = ds.fetch_tw_market_daily(start, end)
    if not daily.empty:
        daily = daily[daily["stock_id"].isin(info["stock_id"])]

    # 法人 / 融資融券要的時間區間比較短
    inst_start = (today - dt.timedelta(days=params.invtrust_lookback_days + 5)).strftime("%Y-%m-%d")
    inst = ds.fetch_institutional_market(inst_start, end)
    if not inst.empty:
        inst = inst[inst["stock_id"].isin(info["stock_id"])]

    margin_start = (today - dt.timedelta(days=10)).strftime("%Y-%m-%d")
    margin = ds.fetch_margin_short_market(margin_start, end)
    if not margin.empty:
        margin = margin[margin["stock_id"].isin(info["stock_id"])]

    ready = today_data_ready(daily)

    s_break = screen_break_ma(daily)
    s_vol = screen_volume_burst(daily, params)
    s_short = screen_short_increase(margin, params)
    s_inst = screen_invtrust_first_buy(inst, params)

    # 合併
    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}
    market_map = info.set_index("stock_id")["type"].to_dict()

    def annotate(df: pd.DataFrame, hit: str) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        x = df.copy()
        x["hit"] = hit
        x["stock_name"] = x["stock_id"].map(name_map).fillna("")
        x["market"] = x["stock_id"].map(market_map).fillna("")
        return x

    s_break = annotate(s_break, "突破月/季線")
    s_vol = annotate(s_vol, "量能爆增")
    s_short = annotate(s_short, "融券增加")
    s_inst = annotate(s_inst, "投信首買")

    # 合併出 combined：以 stock_id 聚合 hits
    parts = [df for df in [s_break, s_vol, s_short, s_inst] if not df.empty]
    if parts:
        all_long = pd.concat(parts, ignore_index=True)
        combined = (
            all_long.groupby(["stock_id", "stock_name", "market"])["hit"]
            .agg(lambda s: sorted(set(s)))
            .reset_index()
        )
        combined["hit_count"] = combined["hit"].apply(len)
        combined["hits_label"] = combined["hit"].apply(lambda xs: "、".join(xs))
        combined = combined.sort_values(["hit_count", "stock_id"], ascending=[False, True]).reset_index(drop=True)
    else:
        combined = pd.DataFrame(columns=["stock_id", "stock_name", "market", "hit", "hit_count", "hits_label"])

    return {
        "ready": ready,
        "latest_date": latest_trading_date(daily),
        "break_ma": s_break,
        "volume_burst": s_vol,
        "short_increase": s_short,
        "invtrust_first_buy": s_inst,
        "combined": combined,
    }
