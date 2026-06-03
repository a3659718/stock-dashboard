"""
buffett_quality_filter.py
Buffett-Lite 品質過濾 (美股) — 用 yfinance ticker.info 抓 ROE/Debt/FCF.

評分:
  ROE ≥ 15%: +5 / 10-15%: +2 / < 5%: -3
  Debt/Equity < 0.5: +3 / > 1.0: -3
  Free Cash Flow Yield > 5%: +3 / 負: -3
  Profit Margin > 20%: +3 / < 5%: -2

API:
  check_quality(symbol) -> Dict {score, reasons, roe, debt_equity, fcf_yield, ...}
"""
from __future__ import annotations

from typing import Dict, List


def check_quality(symbol: str) -> Dict:
    """回 quality 分數 (0-20 範圍, 不限) + reasons list."""
    out = {
        "symbol": symbol,
        "score": 0,
        "reasons": [],
        "roe": None,
        "debt_equity": None,
        "fcf_yield": None,
        "profit_margin": None,
        "quality_label": "—",
    }
    try:
        import fundamentals_us as fu
        f = fu.fetch_us_fundamentals(symbol)
        score = 0
        reasons: List[str] = []

        roe = f.get("returnOnEquity") or f.get("roe")
        if roe is not None:
            roe_pct = float(roe) * 100 if abs(roe) < 1 else float(roe)
            out["roe"] = round(roe_pct, 2)
            if roe_pct >= 15:
                score += 5; reasons.append(f"✅ ROE {roe_pct:.2f}% (優質)")
            elif roe_pct >= 10:
                score += 2; reasons.append(f"➕ ROE {roe_pct:.2f}%")
            elif roe_pct < 5:
                score -= 3; reasons.append(f"❌ ROE {roe_pct:.2f}% 低 (品質差)")

        de = f.get("debtToEquity")
        if de is not None:
            try:
                de_ratio = float(de) / 100 if float(de) > 5 else float(de)
                out["debt_equity"] = round(de_ratio, 2)
                if de_ratio < 0.5:
                    score += 3; reasons.append(f"✅ Debt/Equity {de_ratio:.2f} (低負債)")
                elif de_ratio > 1.0:
                    score -= 3; reasons.append(f"⚠️ Debt/Equity {de_ratio:.2f} 高")
            except (TypeError, ValueError):
                pass

        # Free Cash Flow Yield = FCF / MarketCap
        fcf = f.get("freeCashflow")
        mcap = f.get("marketCap")
        if fcf is not None and mcap and mcap > 0:
            fcf_yield = float(fcf) / float(mcap) * 100
            out["fcf_yield"] = round(fcf_yield, 2)
            if fcf_yield > 5:
                score += 3; reasons.append(f"✅ FCF Yield {fcf_yield:.2f}% (現金充沛)")
            elif fcf_yield < 0:
                score -= 3; reasons.append(f"❌ FCF Yield {fcf_yield:.2f}% 負")

        pm = f.get("profitMargins")
        if pm is not None:
            pm_pct = float(pm) * 100 if abs(pm) < 1 else float(pm)
            out["profit_margin"] = round(pm_pct, 2)
            if pm_pct > 20:
                score += 3; reasons.append(f"✅ Profit Margin {pm_pct:.2f}%")
            elif pm_pct < 5:
                score -= 2; reasons.append(f"➖ Profit Margin {pm_pct:.2f}%")

        # 品質標籤
        if score >= 8:
            out["quality_label"] = "🟢 Buffett 級 (高品質)"
        elif score >= 4:
            out["quality_label"] = "🟡 中品質"
        elif score < 0:
            out["quality_label"] = "🔴 品質差"
        else:
            out["quality_label"] = "⚪ 普通"

        out["score"] = score
        out["reasons"] = reasons
    except Exception as e:
        print(f"[buffett_quality] {symbol} fail: {e}", flush=True)
    return out
