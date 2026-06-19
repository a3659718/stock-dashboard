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
    # B7 修正: 投本比改用 20 日累計買超 / 流通股本, 門檻提到 1.5%
    # (原本 5 日 0.5% 太低, 任何投信小幅買進都會觸發)
    capital_ratio_pct: float = 1.5  # 投本比門檻 (%) - 20 日累計買超 / 流通股本
    capital_ratio_window: int = 20  # 投本比計算的窗口 (天)
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
    "capital_ratio":       "投本比 ≥ 1.5% (20日累計)",
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


def _last_trading_day_tw(today: dt.date | None = None) -> dt.date:
    """回傳「最近一個台股交易日」(週末 / 假日 → 往前推).
    B3 修正: 用 holiday_check 判斷, 失敗時 fallback 到只跳週末.
    """
    today = today or dt.date.today()
    try:
        import holiday_check
        # 從今天往前最多回 14 天找最近交易日
        d = today
        for _ in range(14):
            if not holiday_check.is_market_closed_today("TW", d):
                return d
            d -= dt.timedelta(days=1)
    except Exception:
        # fallback: 只跳週末
        d = today
        while d.weekday() >= 5:  # 5=Sat, 6=Sun
            d -= dt.timedelta(days=1)
        return d
    return today


def today_data_ready(df: pd.DataFrame) -> bool:
    """檢查資料是否已包含「最近一個交易日」(B3 修正: 假日 / 週末改用上一個交易日比對).
    """
    last_td = pd.Timestamp(_last_trading_day_tw())
    last = latest_trading_date(df)
    if last is None:
        return False
    return last.normalize() == last_td.normalize()


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
    # Bug fix: FinMind 若改名/缺 Trading_Volume 欄, 原本會在 groupby 迴圈內 KeyError 炸掉整個量能篩選.
    #          先檢查欄位, 缺就 graceful 回空 (不中斷其他篩選).
    if "Trading_Volume" not in daily.columns:
        print("[tw_screener] 缺 Trading_Volume 欄, 跳過量能篩選", flush=True)
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
        # B4 修正: 真正的「首次買超」= 過去 30 日內沒有任何一天 net > 0
        # (原邏輯只看累計 <= 0, 會把 "5 天大買 5 天大賣淨額為負, 今天又買" 誤判為首買)
        prior_buy_days = int((prior["net"] > 0).sum())
        cum_prior = float(prior["net"].sum())
        if today_net > 0 and prior_buy_days == 0:
            rows.append(
                {
                    "stock_id": sid,
                    "today_net_buy": int(today_net),
                    "prior_30d_cum": int(cum_prior),
                    "prior_buy_days": prior_buy_days,  # 應為 0
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
# 7) 投本比 ≥ X%  (B7 修正: 改用 20 日累計投信淨買 / 流通股本)
# ---------------------------------------------------------------------------
def screen_invtrust_capital_ratio(inst: pd.DataFrame, shares_map: dict, params: TWParams) -> pd.DataFrame:
    """投本比 = 投信 N 日累計淨買 (張) / 流通股本 (張) × 100 (%).

    B7 修正:
      - 視窗從 5 日改為 20 日 (params.capital_ratio_window), 5 日易被單日大買噪音帶偏
      - 門檻從 0.5% 改為 1.5% (見 TWParams 預設值)
      - 加入「短期防呆」: 若 N 日淨買 < 50 張, 直接跳過 (避免投信只買幾張就觸發)
    """
    if inst.empty or not shares_map:
        return pd.DataFrame()
    df = inst[inst["name"] == "Investment_Trust"].copy()
    if df.empty:
        return pd.DataFrame()
    df["net"] = df["buy"].astype(float) - df["sell"].astype(float)
    last_date = df["date"].max()
    window = getattr(params, "capital_ratio_window", 20)
    # L4 修正: window 內的「實際資料筆數」要 >= window * 0.5 才算數,
    # 否則樣本不足 (例如新上市股或投信完全沒進出), 不應該觸發訊號.
    min_required_rows = max(5, int(window * 0.5))
    rows = []
    for sid, g in df.groupby("stock_id"):
        g = g.sort_values("date")
        if g["date"].max() != last_date:
            continue
        last_window = g.tail(window)
        if len(last_window) < min_required_rows:
            continue  # L4: 資料天數不足, 跳過
        cum = float(last_window["net"].sum())
        if cum < 50:
            continue  # 短期防呆: 投信只買幾張, 投本比再高也沒意義
        shares = shares_map.get(str(sid))
        if not shares or shares <= 0:
            continue
        ratio = cum / shares * 100.0
        if ratio >= params.capital_ratio_pct:
            rows.append({
                "stock_id": sid,
                "shares_kilo": int(shares),
                f"{window}d_cum_net": int(cum),
                f"{window}d_data_rows": len(last_window),
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
    """回傳 (K, D) Series。標準台灣 9 日 KD。
    若連續 n 日 high == low (極罕見, 但會出現在停牌或無量股), 此時
    分母為 0 會產生 inf/-inf, 需先 replace 再 fillna 以避免污染後續迭代.
    """
    ll = low.rolling(n).min()
    hh = high.rolling(n).max()
    denom = (hh - ll).replace(0, np.nan)  # 避免除以 0
    rsv = (close - ll) / denom * 100
    rsv = rsv.replace([np.inf, -np.inf], np.nan).fillna(50)
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
    progress_cb=None,
) -> dict:
    """跑被啟用的篩選，並合併結果。
    market: 'twse' | 'tpex' | 'all'
    enabled: 條件 list
    progress_cb: callable(stage:str, pct:int) — 用來顯示進度
    """
    def _p(stage: str, pct: int):
        if progress_cb:
            try:
                progress_cb(stage, pct)
            except Exception:
                pass

    _p("準備掃描清單…", 5)
    params = params or TWParams()
    if enabled is None:
        enabled = list(CONDITION_LABELS.keys())

    info = ds.get_taiwan_stock_info()
    # 防禦性去重：FinMind 偶爾會回 dup rows (同檔在不同 type 各一筆)
    if not info.empty and "stock_id" in info.columns:
        info = info.drop_duplicates(subset=["stock_id"], keep="first").reset_index(drop=True)
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

    if need_daily:
        _p(f"抓全市場日線 ({len(universe)} 檔)…", 15)
        daily = ds.fetch_tw_universe_daily(universe_t, start, end)
        if not daily.empty:
            daily = daily.drop_duplicates(subset=["stock_id", "date"], keep="last").reset_index(drop=True)
    else:
        daily = pd.DataFrame()

    inst = pd.DataFrame()
    if need_inst:
        _p(f"抓投信法人資料 ({len(universe)} 檔)…", 35)
        inst_start = (today - dt.timedelta(days=params.invtrust_lookback_days + 5)).strftime("%Y-%m-%d")
        inst = ds.fetch_institutional_universe(universe_t, inst_start, end)
        if not inst.empty:
            inst = inst.drop_duplicates(subset=["stock_id", "date", "name"], keep="last").reset_index(drop=True)

    margin = pd.DataFrame()
    if need_margin:
        _p(f"抓融資融券資料 ({len(universe)} 檔)…", 55)
        margin_start = (today - dt.timedelta(days=10)).strftime("%Y-%m-%d")
        margin = ds.fetch_margin_universe(universe_t, margin_start, end)
        if not margin.empty:
            margin = margin.drop_duplicates(subset=["stock_id", "date"], keep="last").reset_index(drop=True)

    shares_map = {}
    if need_shares:
        _p("抓流通股本…", 70)
        shares_map = ds.fetch_shares_outstanding(universe_t)

    # 計算就緒狀態 (任一資料的最新日期)
    latest_date = None
    for d in (daily, inst, margin):
        if not d.empty:
            ld = d["date"].max()
            if latest_date is None or ld > latest_date:
                latest_date = ld
    # B3 修正: ready 判斷改為「資料最新日 == 最近一個交易日」, 假日 / 週末就用上一個交易日.
    last_td = pd.Timestamp(_last_trading_day_tw(today))
    ready = (latest_date is not None and pd.Timestamp(latest_date).normalize() == last_td.normalize())

    # 跑各 screen
    _p("計算各條件命中…", 80)
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
    market_map = info.set_index("stock_id")["type"].to_dict() if "type" in info.columns else {}

    def annotate(df: pd.DataFrame, hit: str) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        x = df.copy()
        x["hit"] = hit
        x["stock_name"] = x["stock_id"].map(name_map).fillna("")
        x["market"] = x["stock_id"].map(market_map).fillna("")
        return x

    _p("彙整結果…", 95)
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

        # === 補上實用數值欄位 (全部用 dict 查表，避免 reindex 重複錯誤) ===
        if not daily.empty:
            d_sorted = daily.sort_values(["stock_id", "date"]).drop_duplicates(
                subset=["stock_id", "date"], keep="last"
            )
            last_close_map = d_sorted.groupby("stock_id")["close"].last().astype(float).to_dict()
            prev_close_map = d_sorted.groupby("stock_id")["close"].apply(
                lambda s: float(s.iloc[-2]) if len(s) >= 2 else None
            ).to_dict()
            last_vol_map = d_sorted.groupby("stock_id")["Trading_Volume"].last().to_dict()
            avg5_vol_map = d_sorted.groupby("stock_id")["Trading_Volume"].apply(
                lambda s: float(s.iloc[-6:-1].mean()) if len(s) >= 6 else None
            ).to_dict()

            combined["現價"] = combined["stock_id"].map(last_close_map).round(2)
            combined["今日量"] = combined["stock_id"].map(last_vol_map).astype("Int64")

            def _calc_today_pct(sid):
                lc = last_close_map.get(sid)
                pc = prev_close_map.get(sid)
                if lc is None or pc is None or pc == 0:
                    return None
                return round((lc / pc - 1) * 100, 2)
            combined["今日%"] = combined["stock_id"].map(_calc_today_pct)

            def _calc_ratio(sid):
                lv = last_vol_map.get(sid)
                av = avg5_vol_map.get(sid)
                if lv is None or av is None or av == 0:
                    return None
                return round(float(lv) / float(av), 2)
            combined["量比"] = combined["stock_id"].map(_calc_ratio)

        # 投信 5 日累計買超張數
        if not inst.empty:
            it = inst[inst["name"] == "Investment_Trust"].copy()
            if not it.empty:
                it = it.drop_duplicates(subset=["stock_id", "date"], keep="last")
                it["net"] = it["buy"].astype(float) - it["sell"].astype(float)
                last_date = it["date"].max()
                # B12 修正: int() 對 NaN 會炸 ValueError, 用 _safe_int 包.
                def _safe_int(v, default=0):
                    try:
                        if v is None or pd.isna(v):
                            return default
                        return int(v)
                    except (TypeError, ValueError):
                        return default

                acc5_map = (
                    it.sort_values("date")
                    .groupby("stock_id")["net"]
                    .apply(lambda s: _safe_int(s.tail(5).sum()))
                    .to_dict()
                )
                today_map = (
                    it[it["date"] == last_date]
                    .groupby("stock_id")["net"]
                    .last()
                    .map(_safe_int)
                    .to_dict()
                )
                combined["投信5日(張)"] = combined["stock_id"].map(acc5_map)
                combined["投信今日(張)"] = combined["stock_id"].map(today_map)

        # 投本比
        if shares_map:
            cum = combined.get("投信5日(張)")
            if cum is not None:
                def _cap_ratio(sid, cum_val):
                    s = shares_map.get(str(sid))
                    if not s or s <= 0 or cum_val is None or pd.isna(cum_val):
                        return None
                    return round(float(cum_val) / float(s) * 100, 3)
                combined["投本比%"] = [
                    _cap_ratio(row["stock_id"], row.get("投信5日(張)"))
                    for _, row in combined.iterrows()
                ]

        # === 流動性 / 股價門檻過濾 (強勢股例外) ===
        if not daily.empty and (params.min_avg_volume > 0 or params.min_price > 0):
            d_clean = daily.drop_duplicates(subset=["stock_id", "date"], keep="last")
            avg5_vol_map = d_clean.groupby("stock_id")["Trading_Volume"].apply(
                lambda s: float(s.iloc[-6:-1].mean()) if len(s) >= 6 else 0.0
            ).to_dict()
            combined["_avg5_vol"] = combined["stock_id"].map(avg5_vol_map).fillna(0)
            today_pct = combined.get("今日%")
            cond_volume = combined["_avg5_vol"] >= params.min_avg_volume
            cond_price = combined["現價"].fillna(0) >= params.min_price if "現價" in combined else True
            if params.keep_strong_anyway and today_pct is not None:
                cond_strong = today_pct.fillna(0) >= 5.0
            else:
                cond_strong = pd.Series([False] * len(combined), index=combined.index)
            keep = (cond_volume & cond_price) | cond_strong
            combined = combined[keep].drop(columns=["_avg5_vol"]).reset_index(drop=True)
    else:
        combined = pd.DataFrame(columns=["stock_id", "stock_name", "market", "hit", "hit_count", "hits_label"])

    _p("完成！", 100)
    return {
        "ready": ready,
        "latest_date": latest_date,
        "enabled": enabled,
        "results": annotated,
        "combined": combined,
    }
