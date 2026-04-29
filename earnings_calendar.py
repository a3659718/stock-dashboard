"""
earnings_calendar.py
個股法說會 / 財報 / 月營收日期 + 利空利多判讀。

【台股】
  - 月營收: 每月 10 日前公告 (上月)
  - 季報:
      Q1 截止 4/30, 通常 4 月中下旬陸續公布
      Q2 截止 8/14
      Q3 截止 11/14
      Q4 + 年報 截止 3/31
  - 法說會: FinMind 沒有專屬 dataset，從 TaiwanStockNews 標題比對「法說會」「投資人說明會」推測

【美股】
  - 財報: yfinance.Ticker.earnings_dates 直接拿，含過去 + 未來
  - 月營收: 美股一般沒這個慣例
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

import data_sources as ds


# ---------------------------------------------------------------------------
# 台股財報慣例日期
# ---------------------------------------------------------------------------
TW_QUARTERLY_DEADLINES = [
    (5, 15),   # Q1 (3/31 結束) 截止 5/15 (公發行) 但實際多 4 月底~5 月初
    (8, 14),   # Q2
    (11, 14),  # Q3
    (3, 31),   # Q4 + 年報 (隔年 3/31)
]


def _next_tw_earnings_deadline(today: dt.date) -> Tuple[dt.date, str]:
    """估下一個台股財報截止日。"""
    candidates: List[dt.date] = []
    labels = ["Q1 季報", "Q2 季報", "Q3 季報", "Q4 / 年報"]
    for i, (month, day) in enumerate(TW_QUARTERLY_DEADLINES):
        for year in (today.year, today.year + 1):
            try:
                d = dt.date(year, month, day)
                if d > today:
                    candidates.append((d, labels[i]))
                    break
            except Exception:
                continue
    if not candidates:
        return None, ""
    candidates.sort(key=lambda x: x[0])
    return candidates[0]


def _next_tw_monthly_revenue(today: dt.date) -> Tuple[dt.date, str]:
    """估下一次月營收公告日 (每月 10 日前)."""
    if today.day < 10:
        target = dt.date(today.year, today.month, 10)
    else:
        # 下個月 10 日
        nxt_year = today.year + (1 if today.month == 12 else 0)
        nxt_month = 1 if today.month == 12 else today.month + 1
        target = dt.date(nxt_year, nxt_month, 10)
    return target, f"{(target.month - 1) or 12} 月營收"


# ---------------------------------------------------------------------------
# 從 FinMind 抓最近一次財報 / 月營收
# ---------------------------------------------------------------------------
@st.cache_data(ttl=86400, show_spinner=False)
def _last_tw_quarterly(stock_id: str) -> Optional[dt.date]:
    today = dt.date.today()
    start = (today - dt.timedelta(days=200)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    try:
        df = ds._finmind_get("TaiwanStockFinancialStatements",
                              data_id=stock_id, start_date=start, end_date=end)
        if df is None or df.empty or "date" not in df.columns:
            return None
        d = pd.to_datetime(df["date"]).max()
        return d.date() if pd.notna(d) else None
    except Exception:
        return None


@st.cache_data(ttl=86400, show_spinner=False)
def _last_tw_monthly_revenue(stock_id: str) -> Optional[Dict]:
    today = dt.date.today()
    start = (today - dt.timedelta(days=120)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    try:
        df = ds._finmind_get("TaiwanStockMonthRevenue",
                              data_id=stock_id, start_date=start, end_date=end)
        if df is None or df.empty:
            return None
        df["date"] = pd.to_datetime(df["date"])
        last = df.sort_values("date").iloc[-1]
        out = {"date": last["date"].date()}
        # YoY 變化 (FinMind 欄位 RevenueYearGrowth or 計算)
        for c in ["RevenueYearGrowth", "revenue_year_growth"]:
            if c in df.columns:
                out["yoy_growth"] = float(last[c])
                break
        if "yoy_growth" not in out:
            # 自己算 YoY
            try:
                rev_now = float(last.get("Revenue", last.get("revenue", 0)) or 0)
                yoy_row = df[df["date"] == (last["date"] - pd.DateOffset(years=1))]
                if not yoy_row.empty:
                    rev_yoy = float(yoy_row.iloc[0].get("Revenue", yoy_row.iloc[0].get("revenue", 0)) or 0)
                    if rev_yoy > 0:
                        out["yoy_growth"] = round((rev_now / rev_yoy - 1) * 100, 1)
            except Exception:
                pass
        return out
    except Exception:
        return None


@st.cache_data(ttl=86400, show_spinner=False)
def _last_tw_investor_conference(stock_id: str) -> Optional[dt.date]:
    """從 TaiwanStockNews 找最近的法說會新聞."""
    today = dt.date.today()
    start = (today - dt.timedelta(days=120)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    try:
        df = ds._finmind_get("TaiwanStockNews", data_id=stock_id,
                              start_date=start, end_date=end)
        if df is None or df.empty:
            return None
        if "title" not in df.columns:
            return None
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        kw = re.compile("法說|投資人說明|法人說明|法說會", re.IGNORECASE)
        matched = df[df["title"].astype(str).apply(lambda t: bool(kw.search(t)))]
        if matched.empty:
            return None
        d = matched["date"].max()
        return d.date() if pd.notna(d) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 美股 earnings via yfinance
# ---------------------------------------------------------------------------
@st.cache_data(ttl=43200, show_spinner=False)
def _us_earnings_info(symbol: str) -> Dict:
    try:
        import yfinance as yf
    except ImportError:
        return {}
    try:
        t = yf.Ticker(symbol)
        edf = t.earnings_dates
    except Exception:
        edf = None
    out = {}
    if edf is not None and not edf.empty:
        try:
            edf2 = edf.reset_index()
            date_col = "Earnings Date" if "Earnings Date" in edf2.columns else edf2.columns[0]
            edf2[date_col] = pd.to_datetime(edf2[date_col], errors="coerce", utc=True).dt.tz_localize(None)
            today_ts = pd.Timestamp.utcnow().tz_localize(None)
            past = edf2[edf2[date_col] < today_ts].sort_values(date_col)
            future = edf2[edf2[date_col] >= today_ts].sort_values(date_col)
            if not past.empty:
                out["last_earnings"] = past.iloc[-1][date_col].date()
                # EPS surprise if available
                for col in ["Surprise(%)", "Surprise %"]:
                    if col in past.columns:
                        try:
                            v = float(past.iloc[-1][col])
                            out["last_surprise_pct"] = round(v, 1)
                        except Exception:
                            pass
                        break
            if not future.empty:
                out["next_earnings"] = future.iloc[0][date_col].date()
        except Exception:
            pass

    # Calendar (備用，含 Estimate)
    try:
        cal = t.calendar
        if isinstance(cal, dict):
            ne = cal.get("Earnings Date")
            if isinstance(ne, list) and ne:
                ne0 = ne[0]
                if isinstance(ne0, dt.date) and "next_earnings" not in out:
                    out["next_earnings"] = ne0
    except Exception:
        pass

    return out


# ---------------------------------------------------------------------------
# 利空利多判讀
# ---------------------------------------------------------------------------
def _interpret_event(days_until: int, event_label: str) -> Tuple[str, str]:
    """根據事件幾天後到，回 (sentiment, brief_text)."""
    if days_until is None:
        return "neutral", ""
    if days_until < 0:
        days_after = -days_until
        if days_after <= 3:
            return "watch", f"剛公布 {days_after} 天，市場仍在消化"
        if days_after <= 14:
            return "neutral", f"已公布 {days_after} 天"
        return "neutral", ""
    if days_until == 0:
        return "warn", "今日公布，波動大"
    if days_until <= 3:
        return "warn", f"{days_until} 天後 — 高度不確定，注意倉位"
    if days_until <= 7:
        return "caution", f"{days_until} 天後 — 接近財報，波動可能加大"
    if days_until <= 14:
        return "neutral", f"{days_until} 天後 — 可能開始醞釀預期"
    return "neutral", f"{days_until} 天後"


def _sentiment_label(sentiment: str) -> str:
    return {
        "warn": "⚠️ 警戒",
        "caution": "⚠ 注意",
        "watch": "👀 觀察",
        "neutral": "➖ 中性",
    }.get(sentiment, "—")


# ---------------------------------------------------------------------------
# 對外: 取得個股事件資訊
# ---------------------------------------------------------------------------
def get_stock_events(stock_id: str, market: str = "TW") -> Dict:
    """回傳 {last_*, next_*, sentiment, brief}.
    market: 'TW' or 'US'.
    """
    today = dt.date.today()

    if market == "US":
        info = _us_earnings_info(stock_id)
        last_e = info.get("last_earnings")
        next_e = info.get("next_earnings")
        days_until = (next_e - today).days if next_e else None
        sentiment, brief = _interpret_event(days_until, "earnings") if next_e else ("neutral", "")
        return {
            "market": "US",
            "last_earnings": last_e,
            "next_earnings": next_e,
            "last_surprise_pct": info.get("last_surprise_pct"),
            "days_until_next": days_until,
            "sentiment": sentiment,
            "brief": brief,
            "summary": _us_summary(last_e, next_e, info.get("last_surprise_pct"), days_until, brief),
        }

    # TW
    last_q = _last_tw_quarterly(stock_id)
    next_dl, next_label = _next_tw_earnings_deadline(today)
    days_until = (next_dl - today).days if next_dl else None
    last_rev = _last_tw_monthly_revenue(stock_id)
    next_rev_dt, next_rev_label = _next_tw_monthly_revenue(today)
    last_conf = _last_tw_investor_conference(stock_id)

    # 利空利多: 取最早的事件當主要 sentiment
    primary_event_days = days_until
    sentiment, brief = _interpret_event(primary_event_days, "earnings") if primary_event_days else ("neutral", "")

    return {
        "market": "TW",
        "last_quarterly": last_q,
        "next_quarterly_deadline": next_dl,
        "next_quarterly_label": next_label,
        "last_monthly_revenue": last_rev.get("date") if last_rev else None,
        "last_monthly_yoy": last_rev.get("yoy_growth") if last_rev else None,
        "next_monthly_revenue": next_rev_dt,
        "next_monthly_label": next_rev_label,
        "last_investor_conference": last_conf,
        "days_until_next": days_until,
        "sentiment": sentiment,
        "brief": brief,
        "summary": _tw_summary(last_q, next_dl, next_label, last_rev, next_rev_dt, next_rev_label,
                                last_conf, brief),
    }


def _tw_summary(last_q, next_dl, next_label, last_rev, next_rev_dt, next_rev_label,
                last_conf, brief: str) -> str:
    parts = []
    if last_q:
        parts.append(f"上次季報 {last_q.strftime('%Y-%m-%d')}")
    if next_dl:
        parts.append(f"下次 {next_label} 截止 {next_dl.strftime('%Y-%m-%d')}")
    if last_rev:
        d = last_rev.get("date")
        yoy = last_rev.get("yoy_growth")
        if d:
            piece = f"上次月營收 {d.strftime('%Y-%m-%d')}"
            if yoy is not None:
                arrow = "📈" if yoy > 0 else "📉"
                piece += f" {arrow}YoY {yoy:+.1f}%"
            parts.append(piece)
    if next_rev_dt:
        parts.append(f"下次{next_rev_label} ~{next_rev_dt.strftime('%Y-%m-%d')}")
    if last_conf:
        parts.append(f"近期法說會 {last_conf.strftime('%Y-%m-%d')}")
    line = " · ".join(parts) if parts else "—"
    if brief:
        line += f" 【{brief}】"
    return line


def _us_summary(last_e, next_e, surprise, days_until, brief: str) -> str:
    parts = []
    if last_e:
        ls = f"Last earnings {last_e.strftime('%Y-%m-%d')}"
        if surprise is not None:
            arrow = "📈" if surprise > 0 else "📉"
            ls += f" {arrow}{surprise:+.1f}%"
        parts.append(ls)
    if next_e:
        parts.append(f"Next earnings {next_e.strftime('%Y-%m-%d')}")
    line = " · ".join(parts) if parts else "—"
    if brief:
        line += f" 【{brief}】"
    return line


def annotate_picks_with_events(picks_data: List[Dict], market: str = "TW") -> Dict[str, Dict]:
    """對 picks 一次補上所有事件資訊。
    回傳 {stock_id: events_dict}.
    """
    out: Dict[str, Dict] = {}
    for r in picks_data or []:
        sid = str(r.get("stock_id", "") or r.get("代號", "") or r.get("symbol", ""))
        if not sid:
            continue
        try:
            out[sid] = get_stock_events(sid, market=market)
        except Exception:
            out[sid] = {}
    return out
