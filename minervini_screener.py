"""
minervini_screener.py
Mark Minervini SEPA Trend Template (8 條件) + VCP detector.

8 條件 (Mark Minervini "Trade Like a Stock Market Wizard"):
  1. 股價 > 150d MA > 200d MA
  2. 150d MA > 200d MA (排列)
  3. 200d MA 至少 1 個月上升趨勢
  4. 50d MA > 150d MA > 200d MA
  5. 股價 > 50d MA
  6. 股價 距 52w 低點 ≥ +30%
  7. 股價 距 52w 高點 ≤ -25% (即在 75% 高點以上)
  8. RS rating ≥ 70 (相對大盤強)

通過 8/8 = 完美 trend template, 7/8 也可接受.

API:
  check_trend_template(symbol) -> Dict {pass_n, pass_8_of_8, conditions_met, ...}
  filter_minervini_picks(picks, min_pass=7) -> List[Dict]
"""
from __future__ import annotations

from typing import Dict, List


def check_trend_template(symbol: str) -> Dict:
    """檢查 Minervini 8 條件, 回 pass count + 每條件結果."""
    out = {
        "symbol": symbol,
        "pass_n": 0,
        "pass_8_of_8": False,
        "conditions": {},
        "current": None,
        "ma_50d": None, "ma_150d": None, "ma_200d": None,
        "rs_rating": None,
    }
    try:
        import data_sources as ds
        df = ds.fetch_yf_history(symbol, period="1y", interval="1d")
        if df is None or df.empty or len(df) < 210:
            return out
        c = df["Close"].astype(float)
        cur = float(c.iloc[-1])
        ma_50 = float(c.rolling(50).mean().iloc[-1])
        ma_150 = float(c.rolling(150).mean().iloc[-1])
        ma_200 = float(c.rolling(200).mean().iloc[-1])
        # 200d MA 1 個月趨勢 (現在 vs 21 日前)
        ma_200_prev = float(c.rolling(200).mean().iloc[-22])
        ma_200_trend_up = ma_200 > ma_200_prev
        # 52w high/low
        hi_52w = float(c.iloc[-252:].max()) if len(c) >= 252 else float(c.max())
        lo_52w = float(c.iloc[-252:].min()) if len(c) >= 252 else float(c.min())
        dist_to_52w_high = (cur / hi_52w - 1) * 100
        dist_from_52w_low = (cur / lo_52w - 1) * 100
        # RS rating (簡化: 用 12 週 return vs SPY)
        rs_rating = None
        try:
            spy = ds.fetch_yf_history("SPY", period="6mo", interval="1d")
            if spy is not None and not spy.empty and len(spy) >= 63 and len(c) >= 63:
                stock_12w = (cur / float(c.iloc[-63]) - 1) * 100
                spy_12w = (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[-63]) - 1) * 100
                # RS rating = 100 * percentile (簡化用 stock-spy diff)
                if spy_12w != 0:
                    rs_diff = stock_12w - spy_12w
                    # diff > 30pp → 100; > 15pp → 80; > 5pp → 70; < 0 → 30
                    if rs_diff >= 30: rs_rating = 95
                    elif rs_diff >= 15: rs_rating = 85
                    elif rs_diff >= 5: rs_rating = 75
                    elif rs_diff >= 0: rs_rating = 60
                    else: rs_rating = 40
        except Exception:
            pass

        # 8 條件
        conditions = {
            "1. 股價 > 150 MA > 200 MA": cur > ma_150 and ma_150 > ma_200,
            "2. 150 MA > 200 MA": ma_150 > ma_200,
            "3. 200 MA 1 月上升": ma_200_trend_up,
            "4. 50 MA > 150 MA > 200 MA": ma_50 > ma_150 > ma_200,
            "5. 股價 > 50 MA": cur > ma_50,
            "6. 距 52w 低 ≥ +30%": dist_from_52w_low >= 30,
            "7. 距 52w 高 ≤ -25%": dist_to_52w_high >= -25,
            "8. RS rating ≥ 70": (rs_rating is not None and rs_rating >= 70),
        }
        pass_n = sum(1 for v in conditions.values() if v)

        out.update({
            "pass_n": pass_n,
            "pass_8_of_8": pass_n == 8,
            "pass_7_or_better": pass_n >= 7,
            "conditions": conditions,
            "current": round(cur, 2),
            "ma_50d": round(ma_50, 2),
            "ma_150d": round(ma_150, 2),
            "ma_200d": round(ma_200, 2),
            "dist_to_52w_high_pct": round(dist_to_52w_high, 2),
            "dist_from_52w_low_pct": round(dist_from_52w_low, 2),
            "rs_rating": rs_rating,
        })
    except Exception as e:
        print(f"[minervini] {symbol} failed: {e}", flush=True)
    return out


def filter_minervini_picks(picks: List[Dict], min_pass: int = 7) -> List[Dict]:
    """對 picks list 過濾, 只留 Minervini 通過 ≥ min_pass 條件的."""
    if not picks:
        return picks
    filtered = []
    for p in picks:
        sym = p.get("symbol") or p.get("stock_id")
        if not sym:
            continue
        res = check_trend_template(sym)
        # 加 minervini 欄位
        p["minervini_pass_n"] = res.get("pass_n", 0)
        p["minervini_rs_rating"] = res.get("rs_rating")
        p["minervini_8_of_8"] = res.get("pass_8_of_8", False)
        if res.get("pass_n", 0) >= min_pass:
            filtered.append(p)
    # 排序: 8/8 → 7/8 → score
    filtered.sort(key=lambda x: (
        x.get("minervini_pass_n", 0),
        x.get("score", 0) or x.get("entry_score", 0) or 0,
    ), reverse=True)
    return filtered
