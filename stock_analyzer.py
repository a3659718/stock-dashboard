"""
stock_analyzer.py
個股深度分析：給一個 stock_id (上市/上櫃皆可)，回傳完整評估。

包含：
  - K 線/量能 6 個月
  - MA20 / MA60
  - KD (9 日)
  - MACD
  - 三大法人 30 日累計
  - 融資融券 10 日變化
  - 各篩選條件命中狀態 (8 條)
  - 綜合評分與建議
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st

import data_sources as ds
import tw_screener as tw


def is_us_symbol(stock_id: str) -> bool:
    """判斷是否為美股代號 (含字母即視為美股, 純 4 位數字視為台股)."""
    sid = stock_id.strip().upper()
    if not sid:
        return False
    return any(c.isalpha() for c in sid)


def fetch_stock_full(stock_id: str) -> dict:
    """一次抓齊一支股票需要的資料。
    台股: FinMind 4 個 dataset (日線/法人/融資融券/股本)
    美股: yfinance 6 個月日線 + 公司資訊 (無法人/融資融券)
    """
    sid = stock_id.strip().upper() if is_us_symbol(stock_id) else stock_id.strip()
    if is_us_symbol(sid):
        return _fetch_us_full(sid)
    return _fetch_tw_full(sid)


def _fetch_tw_full(stock_id: str) -> dict:
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    start_long = (today - dt.timedelta(days=200)).strftime("%Y-%m-%d")
    start_short = (today - dt.timedelta(days=40)).strftime("%Y-%m-%d")

    daily = ds._finmind_get_one("TaiwanStockPrice", stock_id, start_long, end)
    if not daily.empty:
        daily["date"] = pd.to_datetime(daily["date"])
        if "max" in daily.columns and "high" not in daily.columns:
            daily = daily.rename(columns={"max": "high", "min": "low"})
        daily = daily.sort_values("date").reset_index(drop=True)

    inst = ds._finmind_get_one(
        "TaiwanStockInstitutionalInvestorsBuySell", stock_id, start_short, end
    )
    if not inst.empty:
        inst["date"] = pd.to_datetime(inst["date"])

    margin = ds._finmind_get_one(
        "TaiwanStockMarginPurchaseShortSale", stock_id, start_short, end
    )
    if not margin.empty:
        margin["date"] = pd.to_datetime(margin["date"])

    info = ds.get_taiwan_stock_info()
    row = info[info["stock_id"] == stock_id]
    name = row.iloc[0].get("stock_name", "") if not row.empty else ""
    market = row.iloc[0].get("type", "") if not row.empty else ""
    industry = row.iloc[0].get("industry_category", "") if not row.empty else ""

    return {
        "stock_id": stock_id, "name": name, "market": market, "industry": industry,
        "is_us": False,
        "daily": daily, "inst": inst, "margin": margin,
    }


def _fetch_us_full(symbol: str) -> dict:
    """美股: 用 yfinance 抓 6 個月日線 + 基本資訊。"""
    daily = ds.fetch_yf_history(symbol, period="6mo", interval="1d")
    if not daily.empty:
        # yfinance 欄位: Date, Open, High, Low, Close, Adj Close, Volume
        daily = daily.rename(columns={
            "Date": "date", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "Trading_Volume",
        })
        daily["date"] = pd.to_datetime(daily["date"])
        daily["stock_id"] = symbol
        daily = daily.sort_values("date").reset_index(drop=True)

    name = symbol
    market = "US"
    industry = ""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        info = t.info or {}
        name = info.get("longName") or info.get("shortName") or symbol
        industry = info.get("industry") or info.get("sector") or ""
    except Exception:
        pass

    return {
        "stock_id": symbol, "name": name, "market": market, "industry": industry,
        "is_us": True,
        "daily": daily,
        "inst": pd.DataFrame(),    # 美股無對應資料
        "margin": pd.DataFrame(),  # 美股無對應資料
    }


def compute_indicators(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return daily
    out = daily.copy()
    c = out["close"].astype(float)
    out["MA20"] = c.rolling(20).mean().round(2)
    out["MA60"] = c.rolling(60).mean().round(2)
    if "high" in out.columns and "low" in out.columns:
        k, d = tw._kd_series(c, out["high"].astype(float), out["low"].astype(float))
        out["K"] = k.round(1)
        out["D"] = d.round(1)
    dif, macd, hist = tw._macd_series(c)
    out["DIF"] = dif.round(3)
    out["MACD"] = macd.round(3)
    out["Hist"] = hist.round(3)
    return out


def evaluate_conditions(stock_id: str, full: dict, params: tw.TWParams | None = None) -> Dict[str, bool]:
    """逐一檢查條件命中狀態 (對單檔)。美股只會有技術面條件命中。"""
    params = params or tw.TWParams()
    daily = full["daily"]
    inst = full.get("inst", pd.DataFrame())
    margin = full.get("margin", pd.DataFrame())

    hits: Dict[str, bool] = {}
    if not daily.empty:
        s_break = tw.screen_break_ma(daily)
        s_vol = tw.screen_volume_burst(daily, params)
        s_above = tw.screen_above_ma_uptrend(daily, params)
        s_kd = tw.screen_kd_golden_cross(daily)
        s_macd = tw.screen_macd_turn_positive(daily)
        hits["break_ma"] = (not s_break.empty) and (s_break["stock_id"] == stock_id).any()
        hits["volume_burst"] = (not s_vol.empty) and (s_vol["stock_id"] == stock_id).any()
        hits["above_ma_uptrend"] = (not s_above.empty) and (s_above["stock_id"] == stock_id).any()
        hits["kd_golden_cross"] = (not s_kd.empty) and (s_kd["stock_id"] == stock_id).any()
        hits["macd_turn_positive"] = (not s_macd.empty) and (s_macd["stock_id"] == stock_id).any()

    if not margin.empty:
        s_short = tw.screen_short_increase(margin, params)
        hits["short_increase"] = (not s_short.empty) and (s_short["stock_id"] == stock_id).any()

    if not inst.empty:
        s_first = tw.screen_invtrust_first_buy(inst, params)
        s_consec = tw.screen_invtrust_consecutive_buy(inst, params)
        s_5d = tw.screen_invtrust_5d_accumulation(inst, params)
        hits["invtrust_first_buy"] = (not s_first.empty) and (s_first["stock_id"] == stock_id).any()
        hits["invtrust_consecutive"] = (not s_consec.empty) and (s_consec["stock_id"] == stock_id).any()
        hits["invtrust_5d_acc"] = (not s_5d.empty) and (s_5d["stock_id"] == stock_id).any()
        # 投本比
        shares = ds.fetch_shares_outstanding((stock_id,))
        if shares:
            s_cap = tw.screen_invtrust_capital_ratio(inst, shares, params)
            hits["capital_ratio"] = (not s_cap.empty) and (s_cap["stock_id"] == stock_id).any()
    return hits


def institutional_summary(inst: pd.DataFrame) -> pd.DataFrame:
    """30 日三大法人累計表."""
    if inst.empty:
        return pd.DataFrame()
    inst = inst.copy()
    inst["net"] = inst["buy"].astype(float) - inst["sell"].astype(float)
    summary = inst.groupby("name")["net"].sum().reset_index()
    summary["net"] = summary["net"].astype(int)
    summary = summary.rename(columns={"name": "法人", "net": "30日累計買賣超(張)"})
    return summary


def margin_summary(margin: pd.DataFrame) -> dict:
    if margin.empty:
        return {}
    margin = margin.sort_values("date")
    out = {}
    if "MarginPurchaseTodayBalance" in margin.columns:
        out["融資餘額(張)"] = int(margin["MarginPurchaseTodayBalance"].iloc[-1])
    for c in ["ShortSaleTodayBalance", "ShortSaleAfterBalance"]:
        if c in margin.columns:
            out["融券餘額(張)"] = int(margin[c].iloc[-1])
            if len(margin) >= 2:
                out["融券近日變化(張)"] = int(margin[c].iloc[-1] - margin[c].iloc[-2])
            break
    return out


def overall_score(hits: Dict[str, bool]) -> Tuple[int, List[str]]:
    """彙整命中項目，給 0-10 分及理由列表."""
    weight = {
        "break_ma": 1.5, "volume_burst": 1.0, "above_ma_uptrend": 1.0,
        "kd_golden_cross": 1.0, "macd_turn_positive": 1.0,
        "short_increase": 0.5, "invtrust_first_buy": 1.5,
        "invtrust_consecutive": 1.0, "invtrust_5d_acc": 1.0, "capital_ratio": 1.5,
    }
    score = 0.0
    reasons = []
    for k, v in hits.items():
        if v:
            score += weight.get(k, 0.5)
            reasons.append(tw.CONDITION_LABELS.get(k, k))
    return round(min(10.0, score), 1), reasons
