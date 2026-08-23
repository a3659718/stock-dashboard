"""
chip_advanced.py
台股獨有的進階籌碼指標 — 千張大戶 / 借券賣出 / 主力券商分點.

為什麼獨立成新檔:
  - chip_analyzer.py 已經很滿且 Edit 工具有截斷風險, 開新檔最安全
  - 這 3 個指標 FinMind dataset 名稱不穩定 (隨版本變), 集中放一個檔好維護
  - 給 upside_screener / chip_filter / actionable_picks 共用

API:
    fetch_large_holders_change(stock_id, days=30) -> Dict
        # 千張大戶比例變化
    fetch_securities_lending(stock_id, days=10) -> Dict
        # 借券賣出餘額 + 變化
    fetch_main_broker_flow(stock_id, days=5) -> Dict
        # 主力券商分點買賣超 (取近 5 日)
    chip_advanced_score(stock_id) -> Dict
        # 三項綜合分數 (0-30), 給 chip_health_score 加總用

注意: FinMind dataset 名稱與欄位常變動, 此模組所有 fetch 都用 try/except 包,
失敗一律 return {} 不阻塞主流程.
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, Optional

import pandas as pd

import data_sources as ds

try:
    import streamlit as st  # type: ignore
except Exception:
    class _NoOp:
        def cache_data(self, *args, **kwargs):
            def deco(f):
                return f
            if len(args) == 1 and callable(args[0]):
                return args[0]
            return deco
    st = _NoOp()  # type: ignore


# FinMind dataset 名稱 (可能隨版本變, 加 fallback list)
_LARGE_HOLDER_DATASETS = [
    "TaiwanStockShareholding",
    "TaiwanStockShareholdingClassChart",
]
_LENDING_DATASETS = [
    "TaiwanStockSecuritiesLending",
    "TaiwanStockShortSaleAndPurchase",
]
_BROKER_DATASETS = [
    "TaiwanStockTradingDailyReport",  # 券商分點日報 (大檔)
    "TaiwanStockBranchByStock",
    "TaiwanStockMainForce",
]


def _try_fetch(datasets: list, stock_id: str, start: str, end: str) -> pd.DataFrame:
    """嘗試多個 dataset 名稱, 第一個有資料的就回."""
    for ds_name in datasets:
        try:
            df = ds._finmind_get_one(ds_name, stock_id, start, end)
            if not df.empty:
                return df
        except Exception:
            continue
    return pd.DataFrame()


# ---------------------------------------------------------------------------
# 1. 千張大戶持股變化
# ---------------------------------------------------------------------------
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_large_holders_change(stock_id: str, days: int = 30) -> Dict:
    """抓近 N 天千張大戶 (持有 ≥ 1000 張) 持股比例變化.

    回傳 {large_holder_pct_now, large_holder_pct_30d_ago, change_pct, trend}
    trend: 'increasing' (主力集中), 'decreasing' (主力出貨), 'stable'
    抓不到回 {}.
    """
    # Bug fix: dt.date.today() 是伺服器本地/UTC 日期, 台北 00:00-07:59 這段 UTC
    # 還是「前一天」, 會讓抓取區間整段偏移一天。改用 TPE (UTC+8) 日期。
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days + 5)).strftime("%Y-%m-%d")
    df = _try_fetch(_LARGE_HOLDER_DATASETS, stock_id, start, end)
    if df.empty:
        return {}
    try:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        # 找 "≥ 1000 張" 持股比例欄位 (FinMind 欄位名稱不一)
        # 常見: HoldingSharesLevel = "1000 ~ 99999999" / "1,000-50,000" 等
        level_col = None
        for c in ["HoldingSharesLevel", "HoldingShares", "ShareholdingLevel"]:
            if c in df.columns:
                level_col = c
                break
        pct_col = None
        for c in ["percent", "Percent", "Percentage", "ShareholdingPercent"]:
            if c in df.columns:
                pct_col = c
                break
        if not level_col or not pct_col:
            return {}
        # 篩出大戶級別 (含 "1000" 或 "1,000" 字串)
        is_large = df[level_col].astype(str).str.contains("1000|1,000|>1000|≥1000", regex=True)
        large_df = df[is_large]
        if large_df.empty:
            return {}
        # 取最新與最舊各一日
        last_date = large_df["date"].max()
        first_date = large_df["date"].min()
        pct_now = float(large_df[large_df["date"] == last_date][pct_col].sum())
        pct_prev = float(large_df[large_df["date"] == first_date][pct_col].sum())
        change = pct_now - pct_prev
        if change >= 1.0:
            trend = "increasing"
        elif change <= -1.0:
            trend = "decreasing"
        else:
            trend = "stable"
        return {
            "large_holder_pct_now": round(pct_now, 2),
            "large_holder_pct_prev": round(pct_prev, 2),
            "change_pct": round(change, 2),
            "trend": trend,
            "data_days": int((last_date - first_date).days),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 2. 借券賣出 (Securities Lending)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_securities_lending(stock_id: str, days: int = 10) -> Dict:
    """抓近 N 天借券賣出餘額.

    借券 vs 融券差異: 借券是「機構級」放空管道 (券商 / 法人), 比融券更代表
    機構看空. 借券激增 = 機構看空; 借券回補 = 看空力道減退 (potential 軋空).

    回傳 {lending_balance_now, lending_change_5d, signal}
    signal: 'short_pressure' (借券激增), 'short_cover' (借券回補), 'stable'
    """
    # Bug fix: dt.date.today() 是伺服器本地/UTC 日期, 台北 00:00-07:59 這段 UTC
    # 還是「前一天」, 會讓抓取區間整段偏移一天。改用 TPE (UTC+8) 日期。
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days + 5)).strftime("%Y-%m-%d")
    df = _try_fetch(_LENDING_DATASETS, stock_id, start, end)
    if df.empty:
        return {}
    try:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        # 借券餘額欄位 (FinMind 變動大)
        bal_col = None
        for c in ["SecuritiesLendingBalance", "LendingBalance", "balance",
                  "shortBalance", "ShortSaleBalance"]:
            if c in df.columns:
                bal_col = c
                break
        if not bal_col:
            return {}
        bal_series = pd.to_numeric(df[bal_col], errors="coerce").dropna()
        if len(bal_series) < 2:
            return {}
        bal_now = float(bal_series.iloc[-1])
        bal_5d_ago = float(bal_series.iloc[max(0, len(bal_series) - 6)])
        change = bal_now - bal_5d_ago
        change_pct = (change / bal_5d_ago * 100) if bal_5d_ago > 0 else 0
        if change_pct >= 20:
            signal = "short_pressure"
        elif change_pct <= -15:
            signal = "short_cover"
        else:
            signal = "stable"
        return {
            "lending_balance_now": int(bal_now),
            "lending_change_5d": int(change),
            "lending_change_pct_5d": round(change_pct, 1),
            "signal": signal,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 3. 主力券商分點買賣超
# ---------------------------------------------------------------------------
# 常被視為「外資代理」的券商分點 (粗略)
_FOREIGN_PROXY_BROKERS = {
    "凱基-台北", "美林", "摩根士丹利", "瑞銀", "高盛", "JP摩根",
    "Goldman", "Morgan", "Merrill", "Citi",
}
# 常見「主力」分點
_MAIN_FORCE_BROKERS = {
    "永豐金-豐原", "凱基-松山", "富邦-嘉義", "群益-板橋",
}


@st.cache_data(ttl=900, show_spinner=False)
def fetch_main_broker_flow(stock_id: str, days: int = 5) -> Dict:
    """抓近 N 天主力券商買賣超.

    回傳 {top_buy_brokers: [...], top_sell_brokers: [...],
          foreign_proxy_net: int, main_force_net: int}
    """
    # Bug fix: dt.date.today() 是伺服器本地/UTC 日期, 台北 00:00-07:59 這段 UTC
    # 還是「前一天」, 會讓抓取區間整段偏移一天。改用 TPE (UTC+8) 日期。
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days + 3)).strftime("%Y-%m-%d")
    df = _try_fetch(_BROKER_DATASETS, stock_id, start, end)
    if df.empty:
        return {}
    try:
        # 欄位名常見: BrokerName / broker / SecuritiesTrader; net / buy-sell
        broker_col = None
        for c in ["BrokerName", "broker", "SecuritiesTrader", "BranchName"]:
            if c in df.columns:
                broker_col = c
                break
        net_col = None
        for c in ["net", "NetVolume", "buy_sell", "BuyMinusSell"]:
            if c in df.columns:
                net_col = c
                break
        # Fallback: 用 buy - sell 算
        if not net_col and "buy" in df.columns and "sell" in df.columns:
            df["_net"] = pd.to_numeric(df["buy"], errors="coerce").fillna(0) - \
                          pd.to_numeric(df["sell"], errors="coerce").fillna(0)
            net_col = "_net"
        if not broker_col or not net_col:
            return {}
        # 加總每個 broker 的 net
        agg = df.groupby(broker_col)[net_col].sum().sort_values(ascending=False)
        top_buy = agg.head(5).to_dict()
        top_sell = agg.tail(5).to_dict()
        # 外資代理 net
        fp_net = sum(v for k, v in agg.items()
                      if any(b in str(k) for b in _FOREIGN_PROXY_BROKERS))
        mf_net = sum(v for k, v in agg.items()
                      if any(b in str(k) for b in _MAIN_FORCE_BROKERS))
        return {
            "top_buy_brokers": {k: int(v) for k, v in top_buy.items() if v > 0},
            "top_sell_brokers": {k: int(v) for k, v in top_sell.items() if v < 0},
            "foreign_proxy_net": int(fp_net),
            "main_force_net": int(mf_net),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 4. 進階綜合分數
# ---------------------------------------------------------------------------
def chip_advanced_score(stock_id: str) -> Dict:
    """三項進階指標的綜合 0-30 分.

    +0~10: 千張大戶趨勢 (increasing +10, stable +3, decreasing -5)
    +0~10: 借券訊號 (short_cover +10, stable +5, short_pressure -5)
    +0~10: 主力券商 (外資代理 / 主力 net 正 +10, 負 -5)
    """
    large = fetch_large_holders_change(stock_id)
    lending = fetch_securities_lending(stock_id)
    brokers = fetch_main_broker_flow(stock_id)

    score = 0
    reasons = []
    warnings = []

    # 千張大戶
    if large.get("trend") == "increasing":
        score += 10
        reasons.append(f"大戶持股 +{large['change_pct']:.1f}% (主力集中)")
    elif large.get("trend") == "decreasing":
        score -= 5
        warnings.append(f"大戶持股 {large['change_pct']:.1f}% (主力出貨)")
    elif large.get("trend") == "stable":
        score += 3

    # 借券
    if lending.get("signal") == "short_cover":
        score += 10
        reasons.append(f"借券回補 {lending['lending_change_pct_5d']:+.0f}% (空頭撤退)")
    elif lending.get("signal") == "short_pressure":
        score -= 5
        warnings.append(f"借券 +{lending['lending_change_pct_5d']:.0f}% (機構看空)")
    elif lending.get("signal") == "stable":
        score += 3

    # 主力券商
    fp_net = brokers.get("foreign_proxy_net", 0)
    mf_net = brokers.get("main_force_net", 0)
    if fp_net > 1000:
        score += 8
        reasons.append(f"外資代理券商買 {fp_net:,} 張")
    elif fp_net < -1000:
        score -= 5
        warnings.append(f"外資代理券商賣 {abs(fp_net):,} 張")
    if mf_net > 500:
        score += 3
        reasons.append(f"主力分點淨買 {mf_net:,} 張")

    score = max(-15, min(30, score))
    return {
        "advanced_score": score,
        "reasons": reasons,
        "warnings": warnings,
        "large_holder": large,
        "lending": lending,
        "brokers": brokers,
    }
