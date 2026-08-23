"""
stock_deep_analyzer.py
個股深度分析: 法說摘要 / PE vs 同業 / 籌碼變化 / K 線形態.

對外接口:
  fetch_recent_announcements(stock_id) → 重大訊息近 30 日 + Gemini 摘要
  compute_pe_vs_peers(stock_id) → PE 跟同產業比較
  fetch_holdings_change(stock_id) → 外資 / 投信持股比例變化
  detect_candle_patterns(stock_id) → 偵測經典 K 線形態 (錘子, 吞噬, 十字星等)
  get_deep_analysis(stock_id) → 一次跑全部 + 整合給 ai_analyzer 用

僅支援台股 (FinMind 資料). 美股部分功能不適用 (法人結構不同 + PE 取得方式不同).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import data_sources as ds


# ===========================================================================
# 1. 法說會 / 重大訊息摘要
# ===========================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_recent_announcements(stock_id: str, days: int = 30) -> Dict:
    """抓近 N 日重大訊息 + Gemini 摘要關鍵事件.

    Returns:
      {
        "raw_items": [{date, title, ...}, ...],   # 原始
        "summary": "Gemini 摘要 (2-3 句重點)",
        "key_events": ["法說會", "財報公告", ...],  # 分類標籤
        "count": N,
      }
    """
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()  # TPE 修正
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")

    raw_items = []
    # 方法 1: 重大訊息 (TaiwanStockMomentousReview)
    try:
        df = ds._finmind_get("TaiwanStockMomentousReview",
                              data_id=stock_id, start_date=start, end_date=end)
        if df is not None and not df.empty:
            for _, r in df.head(20).iterrows():
                raw_items.append({
                    "date": str(r.get("date", "") or ""),
                    "title": str(r.get("title", "") or r.get("content", "") or "")[:200],
                    "category": "重大訊息",
                })
    except Exception:
        pass

    # 方法 2: 新聞 (TaiwanStockNews)
    try:
        df_news = ds._finmind_get("TaiwanStockNews",
                                    data_id=stock_id, start_date=start, end_date=end)
        if df_news is not None and not df_news.empty:
            for _, r in df_news.head(10).iterrows():
                raw_items.append({
                    "date": str(r.get("date", "") or ""),
                    "title": str(r.get("title", "") or "")[:200],
                    "category": "新聞",
                })
    except Exception:
        pass

    # 按日期 desc 排序
    raw_items.sort(key=lambda x: x.get("date", ""), reverse=True)
    raw_items = raw_items[:25]

    # 分類關鍵事件
    key_events = []
    for it in raw_items:
        t = it.get("title", "")
        if any(kw in t for kw in ["法說", "法說會", "投資人說明會"]):
            key_events.append("法說會")
        if any(kw in t for kw in ["EPS", "財報", "獲利", "營收", "毛利"]):
            key_events.append("財報/營收")
        if any(kw in t for kw in ["合作", "訂單", "簽約", "得標"]):
            key_events.append("業務合作")
        if any(kw in t for kw in ["擴產", "新廠", "建廠"]):
            key_events.append("擴產")
        if any(kw in t for kw in ["減資", "增資", "股利", "庫藏股"]):
            key_events.append("股權異動")
    key_events = list(set(key_events))

    # Gemini 摘要 (失敗回 "")
    summary = ""
    if raw_items:
        summary = _gemini_summarize_announcements(stock_id, raw_items)

    return {
        "raw_items": raw_items,
        "summary": summary,
        "key_events": key_events,
        "count": len(raw_items),
    }


def _gemini_summarize_announcements(stock_id: str, items: List[Dict],
                                       model: str = "gemini-2.5-flash") -> str:
    """用 Gemini 把 N 條重大訊息 / 新聞 摘成 2-3 句."""
    if not items:
        return ""
    try:
        import ai_analyzer as _ai
        if not _ai.gemini_available():
            return ""
    except ImportError:
        return ""

    items_text = "\n".join(
        f"  {it.get('date','')} [{it.get('category','')}] {it.get('title','')[:120]}"
        for it in items[:15]
    )
    prompt = (
        f"以下是台股 {stock_id} 近 30 日的重大訊息 / 新聞.\n\n"
        f"{items_text}\n\n"
        "請用繁中 2-3 句摘要「最重要的 1-2 個事件」(法說 / 財報 / 訂單 / 法人變化等).\n"
        "格式: 直接給 2-3 行內容, 不要前後贅述, 不要 markdown.\n"
        "若沒明顯重要事件, 回 「近期無重大訊息」."
    )
    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.3, "max_output_tokens": 300},
            safety_settings=_ai.get_safety_settings(),
        )
        return (getattr(resp, "text", None) or "").strip()[:500]
    except Exception as e:
        print(f"[stock_deep] _gemini_summarize {stock_id} failed: {e}", flush=True)
        return ""


# ===========================================================================
# 2. PE 跟同業比較
# ===========================================================================
@st.cache_data(ttl=3600, show_spinner=False)
def compute_pe_vs_peers(stock_id: str) -> Dict:
    """個股最新 PE vs 同產業中位數 / 分位數.

    Returns:
      {
        "stock_pe": 25.3,
        "stock_industry": "半導體業",
        "peer_count": 18,
        "peer_median_pe": 22.1,
        "peer_avg_pe": 24.5,
        "stock_percentile": 60.5,    # 該股在同業裡排第幾 % (0-100)
        "valuation": "合理" / "偏高" / "低估" / "極高",
        "context": "半導體業 18 檔中位數 PE 22.1, 該股 25.3, 位居 60 percentile (略偏高)"
      }
    """
    out = {"stock_pe": None, "valuation": "—", "context": ""}

    _today_tw = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()  # TPE 修正
    today = _today_tw.strftime("%Y-%m-%d")
    start = (_today_tw - dt.timedelta(days=14)).strftime("%Y-%m-%d")

    # 該股 PE
    try:
        df = ds._finmind_get("TaiwanStockPER", data_id=stock_id,
                              start_date=start, end_date=today)
        if df is not None and not df.empty:
            last = df.iloc[-1]
            pe = float(last.get("PER", 0) or 0)
            if pe > 0:
                out["stock_pe"] = round(pe, 2)
    except Exception as e:
        print(f"[stock_deep] PER {stock_id} failed: {e}", flush=True)
        return out

    if not out["stock_pe"]:
        return out

    # 該股產業
    try:
        info = ds.get_taiwan_stock_info()
        row = info[info["stock_id"] == stock_id]
        if row.empty:
            return out
        industry = row.iloc[0].get("industry_category", "")
        out["stock_industry"] = industry
        if not industry:
            return out

        # 同業所有股票
        peers = info[info["industry_category"] == industry]["stock_id"].tolist()
        if stock_id in peers:
            peers.remove(stock_id)
        if len(peers) < 3:
            return out
        # 抓同業 PE (限 30 檔, 避免太慢)
        peers = peers[:30]
    except Exception as e:
        print(f"[stock_deep] industry lookup {stock_id} failed: {e}", flush=True)
        return out

    # 抓同業 PE (一次抓多檔)
    peer_pes = []
    for psid in peers:
        try:
            df_p = ds._finmind_get("TaiwanStockPER", data_id=psid,
                                     start_date=start, end_date=today)
            if df_p is not None and not df_p.empty:
                pe_p = float(df_p.iloc[-1].get("PER", 0) or 0)
                if 1 < pe_p < 200:  # 過濾極端值
                    peer_pes.append(pe_p)
        except Exception:
            continue

    if len(peer_pes) < 3:
        return out

    import statistics
    median = statistics.median(peer_pes)
    avg = statistics.mean(peer_pes)
    stock_pe = out["stock_pe"]
    percentile = sum(1 for p in peer_pes if p <= stock_pe) / len(peer_pes) * 100

    if stock_pe < median * 0.7:
        valuation = "低估"
    elif stock_pe < median * 1.15:
        valuation = "合理"
    elif stock_pe < median * 1.5:
        valuation = "偏高"
    else:
        valuation = "極高"

    out.update({
        "peer_count": len(peer_pes),
        "peer_median_pe": round(median, 2),
        "peer_avg_pe": round(avg, 2),
        "stock_percentile": round(percentile, 1),
        "valuation": valuation,
        "context": (
            f"{out.get('stock_industry','')} 同業 {len(peer_pes)} 檔, "
            f"中位 PE {median:.1f}, 平均 PE {avg:.1f}; "
            f"本股 PE {stock_pe:.1f} 在第 {percentile:.0f} percentile ({valuation})"
        ),
    })
    return out


# ===========================================================================
# 3. 外資 / 投信持股比例變化
# ===========================================================================
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_holdings_change(stock_id: str, days: int = 90) -> Dict:
    """近 N 日外資 / 投信持股比例變化.

    Returns:
      {
        "history": pd.DataFrame(date, foreign_pct, trust_pct),
        "foreign_pct_now": 65.2,
        "foreign_pct_30d_ago": 62.8,
        "foreign_change_30d": 2.4,
        "trust_pct_now": 1.5,
        "trust_change_30d": 0.3,
        "trend": "外資增持" / "外資減持" / "持平"
      }
    """
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()  # TPE 修正
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        df = ds._finmind_get("TaiwanStockShareholding",
                              data_id=stock_id, start_date=start, end_date=end)
    except Exception as e:
        print(f"[stock_deep] Shareholding {stock_id} failed: {e}", flush=True)
        return {}

    if df is None or df.empty:
        # Fallback: 用買賣超累積 + 流通股數估算
        return _estimate_from_buysell(stock_id, days)

    try:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        # FinMind 欄位: ForeignInvestmentShares, ForeignInvestmentShareRatio
        if "ForeignInvestmentShareRatio" not in df.columns:
            return {}
        df["foreign_pct"] = df["ForeignInvestmentShareRatio"].astype(float)
        latest = df.iloc[-1]
        now_pct = float(latest["foreign_pct"])

        # 30 日前
        cutoff_30d = today - dt.timedelta(days=30)
        before = df[df["date"] <= pd.Timestamp(cutoff_30d)]
        prev_pct = float(before.iloc[-1]["foreign_pct"]) if not before.empty else now_pct
        change_30d = now_pct - prev_pct

        # 90 日前
        cutoff_90d = today - dt.timedelta(days=90)
        before_90 = df[df["date"] <= pd.Timestamp(cutoff_90d)]
        prev_90 = float(before_90.iloc[-1]["foreign_pct"]) if not before_90.empty else now_pct
        change_90d = now_pct - prev_90

        if change_30d > 1.0:
            trend = "外資積極增持"
        elif change_30d > 0.3:
            trend = "外資緩步增持"
        elif change_30d < -1.0:
            trend = "外資減持"
        elif change_30d < -0.3:
            trend = "外資緩步減持"
        else:
            trend = "持平"

        return {
            "history": df[["date", "foreign_pct"]].copy(),
            "foreign_pct_now": round(now_pct, 2),
            "foreign_pct_30d_ago": round(prev_pct, 2),
            "foreign_change_30d": round(change_30d, 2),
            "foreign_change_90d": round(change_90d, 2),
            "trend": trend,
        }
    except Exception as e:
        print(f"[stock_deep] holdings parse {stock_id} failed: {e}", flush=True)
        return {}


def _estimate_from_buysell(stock_id: str, days: int) -> Dict:
    """Shareholding 抓不到時, 用累積買賣超估算趨勢."""
    try:
        import chip_analyzer
        chip = chip_analyzer.fetch_chip_data(stock_id, days=days)
        inst = chip.get("institutional", {})
        fi = inst.get("Foreign_Investor", {})
        fi_30d = fi.get("30d_total", 0) or 0
        fi_5d = fi.get("5d_total", 0) or 0
        consec = fi.get("consecutive_days", 0) or 0
        if fi_30d > 5000:
            trend = "外資積極增持 (估)"
        elif fi_30d > 1000:
            trend = "外資緩步增持 (估)"
        elif fi_30d < -5000:
            trend = "外資減持 (估)"
        elif fi_30d < -1000:
            trend = "外資緩步減持 (估)"
        else:
            trend = "持平 (估)"
        return {
            "history": None,
            "foreign_pct_now": None,
            "foreign_change_30d": None,
            "fi_30d_lots": fi_30d,
            "fi_5d_lots": fi_5d,
            "fi_consecutive": consec,
            "trend": trend,
            "note": "持股比例 API 失敗, 用累積買賣超估算",
        }
    except Exception:
        return {}


# ===========================================================================
# 4. K 線形態辨識 (5-6 個經典 pattern, 純 Python 無外部套件)
# ===========================================================================
@st.cache_data(ttl=600, show_spinner=False)
def detect_candle_patterns(stock_id: str, market: str = "TW",
                              lookback_days: int = 5) -> Dict:
    """偵測近 N 日 K 線形態.

    支援:
      - hammer 錘子線 (下影線長, 上影線短, 收盤 > 中點) → 跌勢反轉訊號
      - shooting_star 流星 (上影線長, 下影線短, 收盤 < 中點) → 漲勢反轉訊號
      - bullish_engulfing 陽包陰吞噬 → 跌勢反轉
      - bearish_engulfing 陰包陽吞噬 → 漲勢反轉
      - doji 十字星 (open ≈ close, 上下影線約等) → 趨勢猶豫
      - long_white 長紅 K (今日% > 4%, 上下影線小) → 強勢
      - long_black 長黑 K (今日% < -4%, 上下影線小) → 弱勢

    Returns:
      {
        "patterns": [{date, type, label, signal, day_index}],  # day_index 0=今天
        "trend_context": "短期上升" / "盤整" / "下降",
        "summary": "近 5 日: 錘子線×1 (2 日前) + 長紅 K (今日)"
      }
    """
    try:
        if market == "US":
            df = ds.fetch_yf_history(stock_id, period="2mo", interval="1d")
        else:
            df = None
            for suffix in [".TW", ".TWO"]:
                df = ds.fetch_yf_history(f"{stock_id}{suffix}", period="2mo", interval="1d")
                if df is not None and not df.empty:
                    break
        if df is None or df.empty or len(df) < lookback_days + 5:
            return {"patterns": [], "trend_context": "資料不足", "summary": ""}

        open_p = df["Open"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)

        patterns = []
        # 趨勢 context (用近 10 日收盤 vs 20MA)
        ma20 = close.tail(20).mean()
        last = float(close.iloc[-1])
        trend_context = "短期上升" if last > ma20 * 1.02 else (
            "短期下降" if last < ma20 * 0.98 else "盤整")

        # 掃描近 lookback_days
        for i in range(max(1, len(df) - lookback_days), len(df)):
            o, h, l, c = float(open_p.iloc[i]), float(high.iloc[i]), float(low.iloc[i]), float(close.iloc[i])
            body = abs(c - o)
            full_range = h - l
            if full_range <= 0:
                continue
            body_ratio = body / full_range
            upper_shadow = h - max(o, c)
            lower_shadow = min(o, c) - l
            is_bullish = c > o
            day_idx = len(df) - 1 - i  # 0 = 今天
            date = pd.to_datetime(df.iloc[i].name if hasattr(df.iloc[i], 'name') else df.index[i]).strftime("%Y-%m-%d") \
                if hasattr(df, 'index') else ""

            # 錘子線 (下影 ≥ 2x 實體, 上影短, 實體小)
            if (lower_shadow >= 2 * body and upper_shadow < 0.3 * body
                    and body_ratio < 0.4 and body > 0):
                patterns.append({
                    "date": date, "type": "hammer", "label": "錘子線",
                    "signal": "潛在跌勢反轉", "day_index": day_idx,
                })
            # 流星 (上影 ≥ 2x 實體, 下影短)
            elif (upper_shadow >= 2 * body and lower_shadow < 0.3 * body
                    and body_ratio < 0.4 and body > 0):
                patterns.append({
                    "date": date, "type": "shooting_star", "label": "流星",
                    "signal": "潛在漲勢反轉", "day_index": day_idx,
                })
            # 十字星 (實體很小, 上下影類似)
            elif body_ratio < 0.1 and full_range > 0.005 * c:
                patterns.append({
                    "date": date, "type": "doji", "label": "十字星",
                    "signal": "趨勢猶豫", "day_index": day_idx,
                })
            # 長紅 / 長黑
            elif body_ratio > 0.7:
                pct = (c / o - 1) * 100 if o else 0
                if is_bullish and pct > 4:
                    patterns.append({
                        "date": date, "type": "long_white", "label": "長紅 K",
                        "signal": f"強勢 ({pct:+.1f}%)", "day_index": day_idx,
                    })
                elif (not is_bullish) and pct < -4:
                    patterns.append({
                        "date": date, "type": "long_black", "label": "長黑 K",
                        "signal": f"弱勢 ({pct:+.1f}%)", "day_index": day_idx,
                    })

            # 吞噬形態 (用前一日)
            if i > 0:
                o_p, c_p = float(open_p.iloc[i-1]), float(close.iloc[i-1])
                # 陽包陰: 前一日陰線, 今天大陽線完全吞噬
                if c_p < o_p and is_bullish and c > o_p and o < c_p:
                    patterns.append({
                        "date": date, "type": "bullish_engulfing", "label": "陽包陰吞噬",
                        "signal": "潛在跌勢反轉", "day_index": day_idx,
                    })
                # 陰包陽
                elif c_p > o_p and not is_bullish and c < o_p and o > c_p:
                    patterns.append({
                        "date": date, "type": "bearish_engulfing", "label": "陰包陽吞噬",
                        "signal": "潛在漲勢反轉", "day_index": day_idx,
                    })

        # 摘要
        if not patterns:
            summary = "近 5 日無明顯經典形態"
        else:
            # 按 day_index asc (越近今天越前面)
            patterns.sort(key=lambda x: x["day_index"])
            counts = {}
            for p in patterns:
                counts[p["label"]] = counts.get(p["label"], 0) + 1
            summary_parts = [f"{lbl} × {n}" for lbl, n in counts.items()]
            summary = " · ".join(summary_parts)

        return {
            "patterns": patterns,
            "trend_context": trend_context,
            "summary": summary,
        }
    except Exception as e:
        print(f"[stock_deep] detect_patterns {stock_id} failed: {e}", flush=True)
        return {"patterns": [], "trend_context": "錯誤", "summary": ""}



# ===========================================================================
# 5. 財報數據: 月營收 YoY + 季 EPS YoY
# ===========================================================================
@st.cache_data(ttl=86400, show_spinner=False)
def fetch_fundamental_metrics(stock_id: str) -> Dict:
    """近期月營收 YoY + 最新季 EPS YoY.

    Returns:
      {
        "monthly_revenue": [
          {"date": "2026-04", "revenue": 1234, "yoy_pct": 12.3, "mom_pct": 3.5},
          ...  # 近 6 個月
        ],
        "latest_revenue_yoy": 12.3,         # 最新月 YoY
        "revenue_trend": "加速" / "減速" / "平穩",
        "latest_eps": 4.5,
        "latest_eps_quarter": "2026Q1",
        "eps_yoy_pct": 25.0,
        "eps_trend": "穩定成長" / "雙位數成長" / "衰退" / "轉虧為盈" / "由盈轉虧",
        "summary": "近 6 月營收 YoY 平均 +12.5%; 2026Q1 EPS 4.5 (YoY +25%)"
      }
    """
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()  # TPE 修正
    start = (today - dt.timedelta(days=365)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")

    out = {"summary": ""}

    # 1) 月營收 (TaiwanStockMonthRevenue)
    try:
        df = ds._finmind_get("TaiwanStockMonthRevenue",
                              data_id=stock_id, start_date=start, end_date=end)
        if df is not None and not df.empty:
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")
            # FinMind 欄位: revenue (萬元), revenue_month, revenue_year, last_year_revenue
            recent = df.tail(6).copy()
            monthly = []
            for _, r in recent.iterrows():
                rev = float(r.get("revenue", 0) or 0)
                last_yr_rev = float(r.get("last_year_revenue", 0) or 0)
                yoy_pct = ((rev / last_yr_rev - 1) * 100) if last_yr_rev > 0 else None
                monthly.append({
                    "date": r["date"].strftime("%Y-%m"),
                    "revenue": int(rev),
                    "yoy_pct": round(yoy_pct, 2) if yoy_pct is not None else None,
                })
            # MoM (近一個月 vs 前一個月)
            if len(monthly) >= 2:
                prev = monthly[-2]["revenue"]
                cur = monthly[-1]["revenue"]
                if prev > 0:
                    monthly[-1]["mom_pct"] = round((cur / prev - 1) * 100, 2)

            out["monthly_revenue"] = monthly
            if monthly:
                out["latest_revenue_yoy"] = monthly[-1].get("yoy_pct")
                # 趨勢: 近 3 月 YoY 跟前 3 月 比
                yoys = [m["yoy_pct"] for m in monthly if m.get("yoy_pct") is not None]
                if len(yoys) >= 4:
                    recent_avg = sum(yoys[-3:]) / 3
                    earlier_avg = sum(yoys[:-3]) / max(1, len(yoys) - 3)
                    diff = recent_avg - earlier_avg
                    if diff > 5:
                        out["revenue_trend"] = "加速 (近 3 月 YoY +%.1f%% vs 前 3 月 +%.1f%%)" % (recent_avg, earlier_avg)
                    elif diff < -5:
                        out["revenue_trend"] = "減速 (近 3 月 YoY +%.1f%% vs 前 3 月 +%.1f%%)" % (recent_avg, earlier_avg)
                    else:
                        out["revenue_trend"] = "平穩"
    except Exception as e:
        print(f"[stock_deep] MonthRevenue {stock_id} failed: {e}", flush=True)

    # 2) 季 EPS (TaiwanStockFinancialStatements)
    try:
        df_fs = ds._finmind_get("TaiwanStockFinancialStatements",
                                  data_id=stock_id, start_date=start, end_date=end)
        if df_fs is not None and not df_fs.empty:
            # FinMind 結構: type, value, date
            # 找 EPS row
            eps_rows = df_fs[df_fs["type"].astype(str).str.contains("EPS", case=False, na=False)]
            if eps_rows.empty:
                # 老 FinMind 可能是 "BasicEPS" 或 "EPS"
                eps_rows = df_fs[df_fs["type"].astype(str).isin(["EPS", "BasicEPS"])]
            if not eps_rows.empty:
                eps_rows = eps_rows.copy()
                eps_rows["date"] = pd.to_datetime(eps_rows["date"])
                eps_rows = eps_rows.sort_values("date")
                latest = eps_rows.iloc[-1]
                latest_eps = float(latest["value"])
                # 找去年同季
                target_date = latest["date"] - pd.DateOffset(years=1)
                # 精確比對「去年同一季」 (用 pandas Period 對齊到 Quarter)
                # 不能用 ±60 天 window — 會 cross-quarter 拿到錯的季 (e.g. Q2 vs Q1)
                latest_q = latest["date"].to_period("Q")
                target_q = (latest["date"] - pd.DateOffset(years=1)).to_period("Q")
                window = eps_rows[eps_rows["date"].dt.to_period("Q") == target_q]
                if not window.empty:
                    # 同季可能多筆 (e.g. 修正版), 取最後一筆 (最新 publish)
                    prev_year_eps = float(window.iloc[-1]["value"])
                    if prev_year_eps == 0:
                        eps_yoy_pct = None
                    else:
                        eps_yoy_pct = (latest_eps / prev_year_eps - 1) * 100
                    out["latest_eps"] = round(latest_eps, 2)
                    out["latest_eps_quarter"] = latest["date"].strftime("%Y-%m")
                    out["prev_year_eps"] = round(prev_year_eps, 2)
                    if eps_yoy_pct is not None:
                        out["eps_yoy_pct"] = round(eps_yoy_pct, 2)
                        # Trend label
                        if prev_year_eps < 0 and latest_eps > 0:
                            out["eps_trend"] = "轉虧為盈"
                        elif prev_year_eps > 0 and latest_eps < 0:
                            out["eps_trend"] = "由盈轉虧"
                        elif eps_yoy_pct > 50:
                            out["eps_trend"] = "高速成長"
                        elif eps_yoy_pct > 15:
                            out["eps_trend"] = "雙位數成長"
                        elif eps_yoy_pct > 0:
                            out["eps_trend"] = "小幅成長"
                        elif eps_yoy_pct > -15:
                            out["eps_trend"] = "小幅衰退"
                        else:
                            out["eps_trend"] = "明顯衰退"
                else:
                    out["latest_eps"] = round(latest_eps, 2)
                    out["latest_eps_quarter"] = latest["date"].strftime("%Y-%m")
                    out["eps_trend"] = "缺去年同季資料"
    except Exception as e:
        print(f"[stock_deep] FinancialStatements {stock_id} failed: {e}", flush=True)

    # Summary
    parts = []
    if out.get("monthly_revenue"):
        n = len(out["monthly_revenue"])
        yoys = [m["yoy_pct"] for m in out["monthly_revenue"] if m.get("yoy_pct") is not None]
        if yoys:
            avg = sum(yoys) / len(yoys)
            parts.append(f"近 {n} 月營收 YoY 均 {avg:+.1f}%")
            if out.get("revenue_trend"):
                parts.append(out["revenue_trend"])
    if out.get("latest_eps") is not None:
        q = out.get("latest_eps_quarter", "")
        eps_str = f"{q} EPS {out['latest_eps']}"
        if out.get("eps_yoy_pct") is not None:
            eps_str += f" (YoY {out['eps_yoy_pct']:+.1f}%)"
        if out.get("eps_trend"):
            eps_str += f" — {out['eps_trend']}"
        parts.append(eps_str)
    out["summary"] = "; ".join(parts) if parts else ""
    return out


# ===========================================================================
# 6. 個別重大訊息 sentiment 標籤 (利多/利空/中性)
# ===========================================================================
def evaluate_announcement_sentiment(items: List[Dict]) -> List[Dict]:
    """對每條重大訊息標 sentiment_label.

    優先用 stock_catalyst._score_news_sentiment 關鍵字評分 (快, 無 quota).
    Gemini 升級版可以批次調 (這版先用關鍵字, 後續可加).
    """
    if not items:
        return items
    try:
        import stock_catalyst
    except ImportError:
        for it in items:
            it["sentiment_label"] = "—"
            it["sentiment_score"] = 0
        return items

    for it in items:
        title = it.get("title", "") or ""
        # 中文 title 用中文字典
        s = stock_catalyst._score_news_sentiment(title, lang="zh")
        sc = s.get("score", 0)
        it["sentiment_score"] = sc
        if sc > 0:
            it["sentiment_label"] = "利多"
            it["sentiment_keywords"] = s.get("bullish", [])[:2]
        elif sc < 0:
            it["sentiment_label"] = "利空"
            it["sentiment_keywords"] = s.get("bearish", [])[:2]
        else:
            it["sentiment_label"] = "中性"
            it["sentiment_keywords"] = []
    return items

# ===========================================================================
# 整合接口 — 一次拿全部, 給 ai_analyzer 用
# ===========================================================================
def get_deep_analysis(stock_id: str, market: str = "TW") -> Dict:
    """跑全部 6 種深度分析, 失敗的部分回空 dict 不會 crash.

    台股: 重大訊息 + PE 同業 + 籌碼 + K 形態 + 財報 + 訊息 sentiment
    美股: 只 K 形態 (其他要付費 API)
    """
    out = {"stock_id": stock_id, "market": market}
    if market == "TW":
        try:
            ann = fetch_recent_announcements(stock_id)
            # 對每條訊息標 sentiment 利多/利空
            if ann.get("raw_items"):
                ann["raw_items"] = evaluate_announcement_sentiment(ann["raw_items"])
                # 統計利多 / 利空 數量
                bull = sum(1 for it in ann["raw_items"] if it.get("sentiment_label") == "利多")
                bear = sum(1 for it in ann["raw_items"] if it.get("sentiment_label") == "利空")
                ann["sentiment_breakdown"] = {"bullish": bull, "bearish": bear,
                                                 "neutral": len(ann["raw_items"]) - bull - bear}
            out["announcements"] = ann
        except Exception as e:
            print(f"[stock_deep] announcements failed: {e}", flush=True)
            out["announcements"] = {}
        try:
            out["pe_peers"] = compute_pe_vs_peers(stock_id)
        except Exception as e:
            print(f"[stock_deep] pe_peers failed: {e}", flush=True)
            out["pe_peers"] = {}
        try:
            out["holdings"] = fetch_holdings_change(stock_id)
        except Exception as e:
            print(f"[stock_deep] holdings failed: {e}", flush=True)
            out["holdings"] = {}
        try:
            out["fundamentals"] = fetch_fundamental_metrics(stock_id)
        except Exception as e:
            print(f"[stock_deep] fundamentals failed: {e}", flush=True)
            out["fundamentals"] = {}
    # K 形態台美都能跑
    try:
        out["candle_patterns"] = detect_candle_patterns(stock_id, market=market)
    except Exception as e:
        print(f"[stock_deep] candle_patterns failed: {e}", flush=True)
        out["candle_patterns"] = {}
    return out


def fmt_deep_analysis_for_prompt(deep: Dict) -> str:
    """把 deep_analysis dict 格式化成可塞 Gemini prompt 的 context 字串."""
    if not deep:
        return ""
    parts = []
    # 法說 / 重大訊息
    ann = deep.get("announcements") or {}
    if ann.get("summary"):
        parts.append(f"近期重大訊息: {ann['summary']}")
    if ann.get("sentiment_breakdown"):
        sb = ann["sentiment_breakdown"]
        parts.append(
            f"訊息 sentiment: 利多 {sb.get('bullish',0)} / 利空 {sb.get('bearish',0)} / 中性 {sb.get('neutral',0)}"
        )
    if ann.get("key_events"):
        parts.append(f"事件分類: {', '.join(ann['key_events'])}")
    # 財報數據
    fund = deep.get("fundamentals") or {}
    if fund.get("summary"):
        parts.append(f"財報數據: {fund['summary']}")
    # PE
    pe = deep.get("pe_peers") or {}
    if pe.get("context"):
        parts.append(f"估值: {pe['context']}")
    # 持股
    h = deep.get("holdings") or {}
    if h.get("trend"):
        if h.get("foreign_pct_now") is not None:
            parts.append(
                f"外資持股: {h['foreign_pct_now']:.1f}% "
                f"(30d {h.get('foreign_change_30d',0):+.2f}pp), {h['trend']}"
            )
        elif h.get("fi_30d_lots"):
            parts.append(f"外資 30 日累積: {h['fi_30d_lots']:+,} 張, {h['trend']}")
    # K 形態
    cp = deep.get("candle_patterns") or {}
    if cp.get("summary"):
        parts.append(f"K 線形態 (近 5 日): {cp['summary']}; 趨勢: {cp.get('trend_context','')}")

    return "\n".join(parts) if parts else ""
