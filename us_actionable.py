"""
us_actionable.py
美股可進場精選 (對應台股 actionable_picks).

把 us_screener.run_us_recommendation 的候選池過篩成「現在能進場前 N 名」,
帶完整卡片資訊: 3 層目標 / 進場區間 / 停損 / R:R / earnings 警示 /
sector ETF 強度 / Gemini 結論 / 入場標籤 / 持倉建議.

對外 API:
  compute_us_actionable_picks(top_n=10, min_score=55) -> List[Dict]
"""
from __future__ import annotations

from typing import Dict, List, Optional
from functools import lru_cache

import data_sources as ds


# Sector ETF mapping (給 sector strength 用)
SECTOR_ETF = {
    "Technology": "XLK",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Consumer Defensive": "XLP",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Basic Materials": "XLB",
}


@lru_cache(maxsize=64)
def _fetch_etf_5d_pct(etf: str) -> Optional[float]:
    """抓 sector ETF 近 5 日漲跌. cache 跨 picks 共用."""
    try:
        df = ds.fetch_yf_history(etf, period="10d", interval="1d")
        if df is None or df.empty or len(df) < 6:
            return None
        c = df["Close"].astype(float)
        return round((float(c.iloc[-1]) / float(c.iloc[-6]) - 1) * 100, 2)
    except Exception:
        return None


def _add_3tier_targets_us(pick: Dict) -> None:
    """加 target_short / target_mid / target_long. 用 ATR + Fib + measured move."""
    sym = pick.get("symbol", "")
    cur = pick.get("current") or pick.get("price")
    if not sym or cur is None:
        return
    try:
        import indicators as ind
        df = ds.fetch_yf_history(sym, period="1y", interval="1d")
        if df is None or df.empty or len(df) < 60:
            return
        c = df["Close"].astype(float).reset_index(drop=True)
        h = df["High"].astype(float).reset_index(drop=True)
        l = df["Low"].astype(float).reset_index(drop=True)

        # 短線: ATR × 3
        try:
            lv = ind.atr_based_levels(h, l, c, entry_price=float(cur),
                                       stop_atr_mult=1.5, target_atr_mult=3.0) or {}
            if lv.get("target"):
                pick["target_short"] = round(float(lv["target"]), 2)
            if lv.get("stop"):
                pick["stop"] = round(float(lv["stop"]), 2)
            if lv.get("entry_low") and lv.get("entry_high"):
                pick["entry_low"] = round(float(lv["entry_low"]), 2)
                pick["entry_high"] = round(float(lv["entry_high"]), 2)
            if lv.get("rr"):
                pick["rr"] = round(float(lv["rr"]), 2)
        except Exception:
            pass

        # 中線: Fib 1.27
        try:
            fib = ind.fibonacci_extension_targets(c, lookback=252, pivot_window=10) or {}
            if fib.get("fib_127"):
                pick["target_mid"] = round(float(fib["fib_127"]), 2)
        except Exception:
            pass

        # 長線: Measured Move 或 Fib 1.62
        try:
            mm = ind.measured_move_target(c, base_lookback=60, min_base_days=15) or {}
            if mm.get("target"):
                pick["target_long"] = round(float(mm["target"]), 2)
            else:
                fib2 = ind.fibonacci_extension_targets(c, lookback=252, pivot_window=10) or {}
                if fib2.get("fib_162"):
                    pick["target_long"] = round(float(fib2["fib_162"]), 2)
        except Exception:
            pass
    except Exception as e:
        print(f"[us_actionable] {sym} 3-tier targets 失敗: {e}", flush=True)


def _add_earnings_guard(pick: Dict, fund: Dict) -> None:
    """加 earnings_date 警告: 進入沉默期 / 財報週前 ≤ 7 天."""
    ed = fund.get("earningsDate")
    if not ed:
        return
    try:
        import datetime as dt
        ed_str = str(ed)[:10]
        ed_date = dt.datetime.strptime(ed_str, "%Y-%m-%d").date()
        days_to = (ed_date - dt.date.today()).days
        if 0 <= days_to <= 3:
            pick.setdefault("warnings", []).append(
                f"⚠️ 財報 {ed_str} ({days_to} 天內), 進場跳空風險高"
            )
        elif 4 <= days_to <= 7:
            pick.setdefault("warnings", []).append(
                f"📅 財報 {ed_str} (約 {days_to} 天後), 進入沉默期前"
            )
    except Exception:
        pass


def _add_sector_etf_strength(pick: Dict, fund: Dict) -> None:
    """加同類股 ETF 5d 強度作 reason (sector rotation)."""
    sec = fund.get("sector")
    if not sec:
        return
    etf = SECTOR_ETF.get(sec)
    if not etf:
        return
    pct_5d = _fetch_etf_5d_pct(etf)
    if pct_5d is None:
        return
    pick["sector_etf"] = etf
    pick["sector_etf_5d_pct"] = pct_5d
    if pct_5d >= 2.0:
        pick.setdefault("reasons", []).append(
            f"🚀 同類股 ETF {etf} 5d +{pct_5d:.2f}% (強勢板塊)"
        )
    elif pct_5d <= -3.0:
        pick.setdefault("warnings", []).append(
            f"⚠️ 同類股 ETF {etf} 5d {pct_5d:.2f}% (板塊走弱)"
        )


def _enrich_pick(row: Dict) -> Dict:
    """把 us_screener 一筆 row 變成 actionable card dict.

    HIGH-A fix: us_screener 真實欄位是 last / daily_% (不是 price / 今日%).
    """
    import fundamentals_us as fu
    import entry_label_helper as el

    sym = str(row.get("symbol", ""))
    if not sym:
        return {}

    # 取 fundamentals (PE, EPS, sector, earningsDate)
    fund = fu.fetch_us_fundamentals(sym)

    # quick_evaluate (entry_label/score/action)
    eval_res = el.quick_evaluate(sym, market="US")

    # us_screener 真實 row schema: symbol/last/daily_%/5d_%/20d_%/RS_20d/題材/近期新聞/score/催化劑
    current = row.get("last") or row.get("price") or row.get("current")
    today_pct = row.get("daily_%") if row.get("daily_%") is not None else row.get("今日%")

    pick = {
        "symbol": sym,
        "stock_id": sym,           # 對齊台股 schema
        "name": fund.get("longName") or row.get("name", sym),
        "current": current,
        "today_pct": today_pct,
        "theme": row.get("題材") or fund.get("industry", ""),
        "entry_label": eval_res.get("entry_label"),
        "entry_emoji": eval_res.get("entry_emoji"),
        "entry_score": eval_res.get("entry_score"),
        "entry_action": eval_res.get("entry_action"),
        "score": eval_res.get("entry_score", 50) / 10.0,  # 轉成 0-10 對齊台股
        "reasons": [],
        "warnings": [],
        # PE / EPS
        "pe": fund.get("trailingPE"),
        "pe_label": fu.fmt_pe_label(fund.get("trailingPE")),
        "forward_pe": fund.get("forwardPE"),
        "peg": fund.get("pegRatio"),
        "eps": fund.get("trailingEps"),
        "earnings_date": fund.get("earningsDate"),
        "sector": fund.get("sector"),
        "industry": fund.get("industry"),
        "marketcap_str": fu.fmt_marketcap(fund.get("marketCap")),
        # 催化劑/新聞 (從 row 帶)
        "catalyst": row.get("催化劑") or "",
        "news_count": len(row.get("近期新聞") or []),
    }

    # 上漲機率 / 持有期 (用 entry_label 簡單映射)
    label = eval_res.get("entry_label", "")
    if label == "BUY":
        pick["win_prob"] = "60%"
        pick["hold_period"] = "5-15 日"
    elif label == "WAIT":
        pick["win_prob"] = "50%"
        pick["hold_period"] = "—"
    else:
        pick["win_prob"] = "40%"
        pick["hold_period"] = "—"

    # 加 reasons (從 us_screener catalyst + 入場行為)
    if row.get("催化劑"):
        pick["reasons"].append(f"💡 {row['催化劑']}")
    # entry_action 當 reason 之一
    if eval_res.get("entry_action") and eval_res["entry_action"] != "—":
        pick["reasons"].append(f"🎯 系統建議: {eval_res['entry_action']}")

    # 3 層目標 + 進場/停損 (修改 pick in-place)
    _add_3tier_targets_us(pick)

    # earnings_date 警示
    _add_earnings_guard(pick, fund)

    # sector ETF 強度
    _add_sector_etf_strength(pick, fund)

    # PE 評論 (簡短 reason)
    pe = pick.get("pe")
    if pe and pe > 0:
        if pe < 10:
            pick["reasons"].append(f"💰 PE {pe:.1f} (極低, 留意是否價值陷阱)")
        elif pe > 50:
            pick["warnings"].append(f"⚠️ PE {pe:.1f} (估值偏高, 留意修正)")

    return pick


def compute_us_actionable_picks(top_n: int = 10, min_score: int = 65,
                                  us_pool: Optional[Dict] = None) -> List[Dict]:
    """美股可進場精選 (對應 actionable_picks).

    流程:
      1. 從 us_screener.run_us_recommendation 拿候選池 (HIGH-F fix:
         caller 已抓過可傳 us_pool 避免重複, 省 60-90s)
      2. 每檔跑 _enrich_pick 加 entry_label/3-tier targets/earnings_date/sector ETF
      3. 篩 entry_score >= min_score (HIGH-E fix: 預設 65 涵蓋純 BUY + 高分 WAIT;
         若不足 5 檔 fallback 降到 55, 避免回空清單)
      4. 按 entry_score 降冪取 top_n
    """
    try:
        if us_pool is None:
            import us_screener
            us_pool = us_screener.run_us_recommendation(top_n=max(top_n * 2, 20))
        top_picks = us_pool.get("top_picks")
        if top_picks is None or top_picks.empty:
            return []
        rows = top_picks.to_dict("records")
    except Exception as e:
        print(f"[us_actionable] us_screener 失敗: {e}", flush=True)
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    picks: List[Dict] = []
    # HIGH-H fix: max_workers 從 4 → 2, 避免跟 us_screener 內部 thread 疊套撞 Yahoo rate limit
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_enrich_pick, r): r for r in rows}
        for fut in as_completed(futs):
            p = fut.result()
            if p:
                picks.append(p)

    # 篩 entry_score (純 BUY 為主)
    qualified = [p for p in picks if (p.get("entry_score") or 0) >= min_score]
    # E fix: 若不足 5 檔, fallback 降到 55 (涵蓋 WAIT 高分段)
    if len(qualified) < 5 and min_score > 55:
        qualified = [p for p in picks if (p.get("entry_score") or 0) >= 55]
        print(f"[us_actionable] min_score={min_score} 不足 5 檔, fallback 55", flush=True)
    qualified.sort(key=lambda x: x.get("entry_score", 0) or 0, reverse=True)
    return qualified[:top_n]
