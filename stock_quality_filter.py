"""
stock_quality_filter.py — 強勢股品質過濾

問題: strong_stock_alert 用 today_pct*2 + vol_ratio*1 太簡單, 推的股票
品質參差 — 30-40% 真強勢, 其他是「短線拉抬」.

加 5 個品質指標:
  1. RS (相對強度 vs 大盤) — RS > 80 = 強過 80% 個股
  2. 連續強勢 — 連 3 日 ≥ +1% 才算有動能 (一日強不算)
  3. 籌碼配合 — 外資/投信連買 ≥ 3 日 (給台股加分)
  4. 不在 52w 高 — 距 52w 高 ≥ 5% (避免追頂)
  5. wait_pullback — 強勢股拉回 5MA / MA20 才推 (不追今日 +5% 的)

API:
  compute_quality_score(stock_id, today_pct, vol_ratio, market="TW") -> Dict
  classify_action(quality, today_pct) -> str  # "強勢追入" / "拉回待買" / "觀望" / "追高警示"
"""
from __future__ import annotations

from typing import Dict, Optional


# 門檻
RS_GOOD = 80           # RS ≥ 80 = 強
RS_STRONG = 90         # RS ≥ 90 = 極強
NEAR_HIGH_PCT = -5.0   # 距 52w 高 > -5% (太接近) = 追頂風險
CONSEC_DAYS = 3        # 連 N 日 ≥ +1% 才算動能
CONSEC_MIN_PCT = 1.0   # 每日至少 ≥ +1%


def _fetch_daily(symbol: str, period: str = "6mo"):
    """抓日線 (cache 透過 data_sources 內部)."""
    try:
        import data_sources as ds
        return ds.fetch_yf_history(symbol, period=period, interval="1d")
    except Exception:
        return None


def compute_rs(stock_id: str, market: str = "TW", lookback: int = 20) -> Optional[float]:
    """相對強度 (RS) vs 大盤.

    RS = stock_return - market_return + 50, clip 0-100
    這是簡化版 — 真正 IBD RS 是 percentile rank, 但我們資料量不夠.

    回 0-100, 50 = 跟大盤一樣, >80 = 強, <20 = 弱
    """
    try:
        # 個股
        if market == "TW":
            df = None
            for sfx in [".TW", ".TWO"]:
                df = _fetch_daily(f"{stock_id}{sfx}")
                if df is not None and not df.empty and len(df) >= lookback + 1:
                    break
            bench = "^TWII"
        else:
            df = _fetch_daily(stock_id)
            bench = "SPY"
        if df is None or df.empty or len(df) < lookback + 1:
            return None
        c = df["Close"].astype(float)
        stock_ret = (float(c.iloc[-1]) / float(c.iloc[-lookback - 1]) - 1) * 100

        # 大盤
        bdf = _fetch_daily(bench)
        if bdf is None or bdf.empty or len(bdf) < lookback + 1:
            return None
        bc = bdf["Close"].astype(float)
        bench_ret = (float(bc.iloc[-1]) / float(bc.iloc[-lookback - 1]) - 1) * 100

        # RS 簡化計算
        diff = stock_ret - bench_ret  # 個股超額報酬
        # 映射到 0-100: 0% 超額 → 50, +20% 超額 → 100, -20% 超額 → 0
        rs = 50 + diff * 2.5
        return max(0, min(100, round(rs, 0)))
    except Exception:
        return None


def compute_consecutive_strength(stock_id: str, market: str = "TW",
                                    min_pct: float = CONSEC_MIN_PCT,
                                    days: int = CONSEC_DAYS) -> int:
    """近 N 日每日漲 ≥ min_pct% 的連續天數 (從最近反向數).

    回: 連續天數 (0 = 沒連續, 3 = 連 3 日強勢)
    """
    try:
        if market == "TW":
            df = None
            for sfx in [".TW", ".TWO"]:
                df = _fetch_daily(f"{stock_id}{sfx}", period="1mo")
                if df is not None and not df.empty and len(df) >= days + 1:
                    break
        else:
            df = _fetch_daily(stock_id, period="1mo")
        if df is None or df.empty or len(df) < days + 1:
            return 0
        c = df["Close"].astype(float)
        # 從最近一天反向數
        n = 0
        for i in range(1, min(days + 1, len(c))):
            if (float(c.iloc[-i]) / float(c.iloc[-i - 1]) - 1) * 100 >= min_pct:
                n += 1
            else:
                break
        return n
    except Exception:
        return 0


def compute_distance_from_52w_high(stock_id: str, market: str = "TW") -> Optional[float]:
    """個股距 52 週高的百分比. 負值 = 還低於高.

    例: -3% = 距高 3% (危險, 接近高)
        -15% = 距高 15% (還有空間)
    """
    try:
        if market == "TW":
            df = None
            for sfx in [".TW", ".TWO"]:
                df = _fetch_daily(f"{stock_id}{sfx}", period="1y")
                if df is not None and not df.empty and len(df) >= 50:
                    break
        else:
            df = _fetch_daily(stock_id, period="1y")
        if df is None or df.empty:
            return None
        c = df["Close"].astype(float)
        cur = float(c.iloc[-1])
        hi52 = float(df["High"].astype(float).max())
        if hi52 <= 0:
            return None
        return round((cur / hi52 - 1) * 100, 2)
    except Exception:
        return None


def compute_chip_streak(stock_id: str, market: str = "TW") -> Dict:
    """籌碼配合 — 外資/投信連續買超天數 (台股 only)."""
    out = {"foreign_streak": 0, "trust_streak": 0}
    if market != "TW":
        return out
    try:
        import chip_analyzer as ca
        chip = ca.fetch_chip_data(stock_id, days=10)
        inst = chip.get("institutional", {}) if chip else {}
        for fkey in ["Foreign_Investor", "外資", "Foreign_Dealer_Self"]:
            if fkey in inst:
                out["foreign_streak"] = int(inst[fkey].get("consecutive_days", 0) or 0)
                break
        for tkey in ["Investment_Trust", "投信"]:
            if tkey in inst:
                out["trust_streak"] = int(inst[tkey].get("consecutive_days", 0) or 0)
                break
    except Exception:
        pass
    return out


def compute_quality_score(stock_id: str, today_pct: float, vol_ratio: float,
                            market: str = "TW") -> Dict:
    """計算完整品質分數 (0-100).

    分數組成:
      - 基礎: today_pct + vol_ratio (40 分)
      - RS: ≥80 +15, ≥90 +25 (25 分)
      - 連續強勢: 連 N 日 +10 (10 分)
      - 籌碼: 外資+投信都連買 +15 (15 分)
      - 不追頂: 距 52w 高 ≤ -5% +10 (10 分)

    總分 100 滿. ≥ 70 = 高品質強勢股.
    """
    score = 0
    detail = {}

    # 1. 基礎 (max 40)
    base = min(40, today_pct * 5 + vol_ratio * 5)
    score += max(0, base)
    detail["base_score"] = round(max(0, base), 1)

    # 2. RS (max 25)
    rs = compute_rs(stock_id, market)
    detail["rs"] = rs
    if rs is not None:
        if rs >= RS_STRONG:
            score += 25
        elif rs >= RS_GOOD:
            score += 15
        elif rs >= 60:
            score += 5

    # 3. 連續強勢 (max 10)
    cs = compute_consecutive_strength(stock_id, market)
    detail["consecutive_strong_days"] = cs
    if cs >= CONSEC_DAYS:
        score += 10
    elif cs >= 2:
        score += 5

    # 4. 籌碼 (max 15) — 台股 only
    chip = compute_chip_streak(stock_id, market)
    detail["foreign_streak"] = chip["foreign_streak"]
    detail["trust_streak"] = chip["trust_streak"]
    if chip["foreign_streak"] >= 3 and chip["trust_streak"] >= 3:
        score += 15
    elif chip["foreign_streak"] >= 3 or chip["trust_streak"] >= 3:
        score += 8

    # 5. 不追頂 (max 10)
    dist = compute_distance_from_52w_high(stock_id, market)
    detail["dist_from_52w_high"] = dist
    if dist is not None:
        if dist <= -15:
            score += 10  # 有空間
        elif dist <= -5:
            score += 5   # 中性
        elif dist <= -2:
            score += 0   # 接近高
        else:
            score -= 5   # 在高位以上, 扣分

    detail["quality_score"] = round(score, 0)
    return detail


def classify_action(quality_detail: Dict, today_pct: float) -> str:
    """根據 quality + today_pct 給出動作建議.

    回:
      "🔥 強勢追入" — score ≥ 75 + today_pct < 3% (還沒過熱)
      "💎 高品質拉回待買" — score ≥ 75 + today_pct ≥ 3% (拉回 5MA 再進)
      "✅ 一般強勢" — score 60-75
      "⚠️ 短線拉抬" — score < 60 + today_pct > 5% (品質低但漲多)
      "🟡 觀望" — score < 60
    """
    score = quality_detail.get("quality_score", 0) or 0
    if score >= 75:
        if today_pct < 3:
            return "🔥 強勢追入"
        else:
            return "💎 高品質拉回待買"
    elif score >= 60:
        return "✅ 一般強勢"
    elif today_pct > 5:
        return "⚠️ 短線拉抬 (品質低)"
    else:
        return "🟡 觀望"


def filter_quality_picks(picks: list, min_score: int = 60, market: str = "TW") -> list:
    """過濾 picks list, 只留品質 ≥ min_score 的.

    每檔加 quality_score + action 標籤.
    """
    out = []
    for p in picks:
        sid = str(p.get("stock_id") or p.get("symbol", ""))
        tp = float(p.get("today_pct", 0) or 0)
        vr = float(p.get("vol_ratio", 0) or 0)
        if not sid:
            continue
        q = compute_quality_score(sid, tp, vr, market)
        p["quality_score"] = q.get("quality_score", 0)
        p["quality_action"] = classify_action(q, tp)
        p["rs"] = q.get("rs")
        p["consecutive_strong_days"] = q.get("consecutive_strong_days")
        p["dist_from_52w_high"] = q.get("dist_from_52w_high")
        if p["quality_score"] >= min_score:
            out.append(p)
    return out
