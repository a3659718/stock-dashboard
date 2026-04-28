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
    max_stocks: int = 200

    # 新增 4 個條件參數
    consecutive_buy_days: int = 3  # 投信連續買超 N 天
    five_day_acc_lots: int = 100   # 5 日累計買超門檻 (張)
    capital_ratio_pct: float = 0.5  # 投本比門檻 (%)
    above_ma_window: int = 20       # MA 視窗 (預設月線)
    above_ma_slope_days: int = 5    # MA 斜率回看天數 (向上判斷)

    # 過濾條件
    min_avg_volume: int = 500     # 5 日均量門檻 (張) — 排除冷門股
    min_price: float = 5.0        # 最低股價 — 排除雞蛋水餃股
    exclude_etf: bool = True       # 排除 ETF / 權證 / TDR / 全額交割
    keep_strong_anyway: bool = True  # 強勢股 (今日漲幅>5%) 即使流動性不足仍保留


# 條件代號 -> 顯示名稱
CONDITION_LABELS = {
    "break_ma":            "突破月/季線",
    "volume_burst":        "量 5–10 倍均量",
    "short_increase":      "融券增加 ≥ 50 張",
    "invtrust_first_buy":  "投信 30 日首買",
    "invtrust_consecutive":"投信連續 3 天買超",
    "invtrust_5d_acc":     "5 日投信累計 ≥ 100 張",
    "capital_ratio":       "投本比 ≥ 0.5%",
    "above_ma_uptrend":    "MA20 上方且趨勢向上",
    "kd_golden_cross":     "KD 黃金交叉",
    "macd_turn_positive":  "MACD 翻紅",
}


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
# 5) 投信連續 N 天買超
# ---------------------------------------------------------------------------
def screen_invtrust_consecutive_buy(inst: pd.DataFrame, params: TWParams) -> pd.DataFrame:
    if inst.empty:
        return pd.DataFrame()
    df = inst[inst["name"] == "Investment_Trust"].copy()
    if df.empty:
        return pd.DataFrame()
    df["net"] = df["buy"].astype(float) - df["sell"].astype(float)
    last_date = df["date"].max()
    rows = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        if g["date"].max() != last_date:
            continue
        last_n = g.tail(params.consecutive_buy_days)
        if len(last_n) < params.consecutive_buy_days:
            continue
        if (last_n["net"] > 0).all():
            rows.append({
                "stock_id": sid,
                "consec_days": int(params.consecutive_buy_days),
                "consec_total_net": int(last_n["net"].sum()),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 6) 5 日投信累計買超 ≥ N 張
# ---------------------------------------------------------------------------
def screen_invtrust_5d_accumulation(inst: pd.DataFrame, params: TWParams) -> pd.DataFrame:
    if inst.empty:
        return pd.DataFrame()
    df = inst[inst["name"] == "Investment_Trust"].copy()
    if df.empty:
        return pd.DataFrame()
    df["net"] = df["buy"].astype(float) - df["sell"].astype(float)
    last_date = df["date"].max()
    rows = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        if g["date"].max() != last_date:
            continue
        last5 = g.tail(5)
        cum = float(last5["net"].sum())
        if cum >= params.five_day_acc_lots:
            rows.append({
                "stock_id": sid,
                "5d_cum_net": int(cum),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 7) 投本比 ≥ X%  (5 日投信淨買 / 流通股本千張 × 100)
# ---------------------------------------------------------------------------
def screen_invtrust_capital_ratio(inst: pd.DataFrame, shares_map: dict, params: TWParams) -> pd.DataFrame:
    if inst.empty or not shares_map:
        return pd.DataFrame()
    df = inst[inst["name"] == "Investment_Trust"].copy()
    if df.empty:
        return pd.DataFrame()
    df["net"] = df["buy"].astype(float) - df["sell"].astype(float)
    last_date = df["date"].max()
    rows = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        if g["date"].max() != last_date:
            continue
        cum = float(g.tail(5)["net"].sum())
        shares = shares_map.get(str(sid))
        if not shares or shares <= 0:
            continue
        ratio = cum / shares * 100.0
        if ratio >= params.capital_ratio_pct:
            rows.append({
                "stock_id": sid,
                "shares_kilo": int(shares),
                "5d_cum_net": int(cum),
                "capital_ratio_%": round(ratio, 3),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 8) 在 MA20 上方且 MA20 趨勢向上 (近 5 日 MA20 斜率 > 0)
# ---------------------------------------------------------------------------
def screen_above_ma_uptrend(daily: pd.DataFrame, params: TWParams) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    rows = []
    w = params.above_ma_window
    s = params.above_ma_slope_days
    for sid, g in daily.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < w + s + 1:
            continue
        c = g["close"].astype(float)
        ma = c.rolling(w).mean()
        if pd.isna(ma.iloc[-1]) or pd.isna(ma.iloc[-1 - s]):
            continue
        above = c.iloc[-1] > ma.iloc[-1]
        slope_up = ma.iloc[-1] > ma.iloc[-1 - s]
        if above and slope_up:
            rows.append({
                "stock_id": sid,
                "close": float(c.iloc[-1]),
                f"ma{w}": float(ma.iloc[-1]),
                f"ma{w}_slope_{s}d": round(float(ma.iloc[-1] - ma.iloc[-1 - s]), 3),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 9) KD 黃金交叉 (9 日 KD)
# ---------------------------------------------------------------------------
def _kd_series(close: pd.Series, high: pd.Series, low: pd.Series, n: int = 9) -> tuple:
    """回傳 (K, D) Series。標準台灣 9 日 KD。"""
    ll = low.rolling(n).min()
    hh = high.rolling(n).max()
    rsv = (close - ll) / (hh - ll) * 100
    rsv = rsv.fillna(50)
    k = [50.0]
    for v in rsv.iloc[1:]:
        k.append(k[-1] * 2 / 3 + (v if pd.notna(v) else 50) / 3)
    k_s = pd.Series(k, index=close.index)
    d = [50.0]
    for v in k_s.iloc[1:]:
        d.append(d[-1] * 2 / 3 + v / 3)
    d_s = pd.Series(d, index=close.index)
    return k_s, d_s


def screen_kd_golden_cross(daily: pd.DataFrame) -> pd.DataFrame:
    """KD 9 日：今天 K 由下穿上 D，且 K 仍 < 80 (避開過熱)."""
    if daily.empty:
        return pd.DataFrame()
    rows = []
    for sid, g in daily.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < 12:
            continue
        try:
            k, d = _kd_series(g["close"].astype(float),
                              g["high"].astype(float),
                              g["low"].astype(float))
            if pd.isna(k.iloc[-1]) or pd.isna(d.iloc[-1]):
                continue
            cross_up = k.iloc[-2] <= d.iloc[-2] and k.iloc[-1] > d.iloc[-1]
            if cross_up and k.iloc[-1] < 80:
                rows.append({
                    "stock_id": sid,
                    "K": round(float(k.iloc[-1]), 1),
                    "D": round(float(d.iloc[-1]), 1),
                })
        except Exception:
            continue
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 10) MACD 翻紅 (DIF 由負轉正 或 由下穿上 MACD signal)
# ---------------------------------------------------------------------------
def _macd_series(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    macd = dif.ewm(span=signal, adjust=False).mean()
    hist = dif - macd
    return dif, macd, hist


def screen_macd_turn_positive(daily: pd.DataFrame) -> pd.DataFrame:
    """MACD 翻紅: 今天柱狀體 (hist) 由負轉正 或 DIF 由下穿上 MACD."""
    if daily.empty:
        return pd.DataFrame()
    rows = []
    for sid, g in daily.groupby("stock_id"):
        g = g.sort_values("date")
        if len(g) < 35:
            continue
        try:
            dif, macd, hist = _macd_series(g["close"].astype(float))
            if pd.isna(hist.iloc[-1]) or pd.isna(hist.iloc[-2]):
                continue
            turn = hist.iloc[-2] <= 0 and hist.iloc[-1] > 0
            if turn:
                rows.append({
                    "stock_id": sid,
                    "DIF": round(float(dif.iloc[-1]), 3),
                    "MACD": round(float(macd.iloc[-1]), 3),
                    "Histogram": round(float(hist.iloc[-1]), 3),
                })
        except Exception:
            continue
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 統一執行
# ---------------------------------------------------------------------------
def run_all_screens(
    market: str = "all",
    params: TWParams | None = None,
    enabled: list | None = None,
) -> dict:
    """跑被啟用的篩選，並合併結果。
    market: 'twse' | 'tpex' | 'all'
    enabled: ['break_ma','volume_burst','short_increase','invtrust_first_buy',
              'invtrust_consecutive','invtrust_5d_acc','capital_ratio','above_ma_uptrend']
    """
    params = params or TWParams()
    if enabled is None:
        enabled = list(CONDITION_LABELS.keys())

    info = ds.get_taiwan_stock_info()
    info = ds.filter_tradeable_stocks(info, exclude_etf=params.exclude_etf)
    if market == "twse":
        info = info[info["type"] == "twse"]
    elif market == "tpex":
        info = info[info["type"] == "tpex"]

    universe = info["stock_id"].head(params.max_stocks).tolist()
    universe_t = tuple(universe)

    today = dt.date.today()
    start = (today - dt.timedelta(days=120)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    # 條件依賴的資料集
    need_daily = bool({"break_ma", "volume_burst", "above_ma_uptrend",
                        "kd_golden_cross", "macd_turn_positive"} & set(enabled))
    need_inst = bool({"invtrust_first_buy", "invtrust_consecutive",
                       "invtrust_5d_acc", "capital_ratio"} & set(enabled))
    need_margin = "short_increase" in enabled
    need_shares = "capital_ratio" in enabled

    daily = ds.fetch_tw_universe_daily(universe_t, start, end) if need_daily else pd.DataFrame()

    inst = pd.DataFrame()
    if need_inst:
        inst_start = (today - dt.timedelta(days=params.invtrust_lookback_days + 5)).strftime("%Y-%m-%d")
        inst = ds.fetch_institutional_universe(universe_t, inst_start, end)

    margin = pd.DataFrame()
    if need_margin:
        margin_start = (today - dt.timedelta(days=10)).strftime("%Y-%m-%d")
        margin = ds.fetch_margin_universe(universe_t, margin_start, end)

    shares_map = ds.fetch_shares_outstanding(universe_t) if need_shares else {}

    # 計算就緒狀態 (任一資料的最新日期)
    latest_date = None
    for d in (daily, inst, margin):
        if not d.empty:
            ld = d["date"].max()
            if latest_date is None or ld > latest_date:
                latest_date = ld
    ready = (latest_date is not None and pd.Timestamp(latest_date).normalize() == pd.Timestamp(today).normalize())

    # 跑各 screen
    results = {}
    if "break_ma" in enabled:
        results["break_ma"] = screen_break_ma(daily)
    if "volume_burst" in enabled:
        results["volume_burst"] = screen_volume_burst(daily, params)
    if "short_increase" in enabled:
        results["short_increase"] = screen_short_increase(margin, params)
    if "invtrust_first_buy" in enabled:
        results["invtrust_first_buy"] = screen_invtrust_first_buy(inst, params)
    if "invtrust_consecutive" in enabled:
        results["invtrust_consecutive"] = screen_invtrust_consecutive_buy(inst, params)
    if "invtrust_5d_acc" in enabled:
        results["invtrust_5d_acc"] = screen_invtrust_5d_accumulation(inst, params)
    if "capital_ratio" in enabled:
        results["capital_ratio"] = screen_invtrust_capital_ratio(inst, shares_map, params)
    if "above_ma_uptrend" in enabled:
        results["above_ma_uptrend"] = screen_above_ma_uptrend(daily, params)
    if "kd_golden_cross" in enabled:
        results["kd_golden_cross"] = screen_kd_golden_cross(daily)
    if "macd_turn_positive" in enabled:
        results["macd_turn_positive"] = screen_macd_turn_positive(daily)

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

    annotated = {k: annotate(v, CONDITION_LABELS[k]) for k, v in results.items()}

    parts = [df for df in annotated.values() if df is not None and not df.empty]
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

        # === 補上實用數值欄位 ===
        # 現價 + 今日漲跌 + 量比 (從 daily 算)
        if not daily.empty:
            last_idx = daily.groupby("stock_id").tail(1).set_index("stock_id")
            prev_idx = daily.groupby("stock_id").nth(-2).set_index("stock_id") if len(daily) > 1 else pd.DataFrame()
            combined["現價"] = combined["stock_id"].map(last_idx["close"].astype(float)).round(2)
            if "Trading_Volume" in last_idx.columns:
                combined["今日量"] = combined["stock_id"].map(last_idx["Trading_Volume"]).astype("Int64")
            if not prev_idx.empty and "close" in prev_idx.columns:
                last_close = combined["stock_id"].map(last_idx["close"].astype(float))
                prev_close = combined["stock_id"].map(prev_idx["close"].astype(float))
                combined["今日%"] = ((last_close / prev_close - 1) * 100).round(2)
            # 量比 (今日量 / 5日均量)
            avg5 = (
                daily.groupby("stock_id")["Trading_Volume"]
                .apply(lambda s: s.iloc[-6:-1].mean() if len(s) >= 6 else None)
            )
            ratio = (
                last_idx["Trading_Volume"].astype(float) / avg5.astype(float)
            ).round(2)
            combined["量比"] = combined["stock_id"].map(ratio)

        # 投信 5 日累計買超張數
        if not inst.empty:
            it = inst[inst["name"] == "Investment_Trust"].copy()
            if not it.empty:
                it["net"] = it["buy"].astype(float) - it["sell"].astype(float)
                last_date = it["date"].max()
                acc5 = (
                    it.sort_values("date")
                    .groupby("stock_id")["net"]
                    .apply(lambda s: int(s.tail(5).sum()))
                )
                today_buy = (
                    it[it["date"] == last_date]
                    .set_index("stock_id")["net"]
                    .apply(lambda v: int(v))
                )
                combined["投信5日(張)"] = combined["stock_id"].map(acc5)
                combined["投信今日(張)"] = combined["stock_id"].map(today_buy)

        # 投本比
        if shares_map:
            shares_s = pd.Series(shares_map)
            cum = combined.get("投信5日(張)")
            if cum is not None:
                shares_for = combined["stock_id"].map(shares_s).astype(float)
                combined["投本比%"] = (cum.astype(float) / shares_for * 100).round(3)

        # === 流動性 / 股價門檻過濾 (強勢股例外) ===
        if not daily.empty and (params.min_avg_volume > 0 or params.min_price > 0):
            avg5_vol = daily.groupby("stock_id")["Trading_Volume"].apply(
                lambda s: float(s.iloc[-6:-1].mean()) if len(s) >= 6 else 0.0
            )
            combined["_avg5_vol"] = combined["stock_id"].map(avg5_vol).fillna(0)
            today_pct = combined.get("今日%")
            cond_volume = combined["_avg5_vol"] >= params.min_avg_volume
            cond_price = combined["現價"].fillna(0) >= params.min_price if "現價" in combined else True
            cond_strong = (today_pct.fillna(0) >= 5.0) if (params.keep_strong_anyway and today_pct is not None) else False
            keep = (cond_volume & cond_price) | cond_strong
            combined = combined[keep].drop(columns=["_avg5_vol"]).reset_index(drop=True)
    else:
        combined = pd.DataFrame(columns=["stock_id", "stock_name", "market", "hit", "hit_count", "hits_label"])

    return {
        "ready": ready,
        "latest_date": latest_date,
        "enabled": enabled,
        "results": annotated,
        "combined": combined,
    }
