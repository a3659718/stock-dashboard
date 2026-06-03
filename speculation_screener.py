"""
speculation_screener.py
投機股 (Story Stock) 專屬篩選 — 放寬 fundamentals, 強調 narrative + volume + theme heat.

跟一般 us_actionable 不同的地方:
  1. 不要求 ROE/EPS positive (允許虧損)
  2. 不要求 PE/PEG (高 PE 或負都接受)
  3. 距 52w 低 / 200d MA 條件大幅放寬
  4. 強調: 量縮 (volatility contraction) + RS 正向 + narrative score 高

主題 universe (2026 熱門):
  量子 / SMR 核能 / 太空 / AI 邊緣 / EV/Air Taxi / 加密 / 稀土 / Defense

API:
  compute_speculation_picks(top_n=10) -> List[Dict]
"""
from __future__ import annotations

from typing import Dict, List


# 投機股 universe (依主題分群)
SPECULATION_THEMES = {
    "量子計算": ["IONQ", "RGTI", "QBTS", "QUBT"],
    "SMR 小型核反應": ["OKLO", "NNE", "SMR", "BWXT", "LEU", "CCJ"],
    "太空商業": ["RKLB", "ASTS", "PL"],
    "AI 邊緣計算": ["SOUN", "BBAI", "SERV"],
    "EV/Air Taxi": ["JOBY", "ACHR", "RIVN", "LCID"],
    "加密 mining": ["MARA", "RIOT", "CLSK", "BTBT"],
    "稀土/中國脫鉤": ["MP", "USAR", "TMC"],
    "Defense Tech": ["PLTR", "KTOS", "AVAV", "ANRO"],
    "Biotech moonshot": ["CARV", "SAVA", "MNMD", "TLRY"],
}


def _flatten_universe() -> List[Dict]:
    """回 [{sym, theme}, ...]."""
    out = []
    for theme, syms in SPECULATION_THEMES.items():
        for s in syms:
            out.append({"sym": s, "theme": theme})
    return out


def _check_speculation_metrics(sid: str) -> Dict:
    """投機股專屬指標. 放寬條件."""
    try:
        import data_sources as ds
        df = ds.fetch_yf_history(sid, period="6mo", interval="1d")
        if df is None or df.empty or len(df) < 100:
            return {}
        c = df["Close"].astype(float)
        v = df["Volume"].astype(float)
        cur = float(c.iloc[-1])
        # 過去 6 個月波動率 (低 → 盤整收縮 = 好)
        c_6mo = c.iloc[-126:]
        hi = float(c_6mo.max())
        lo = float(c_6mo.min())
        range_pct = (hi - lo) / lo * 100 if lo > 0 else 999
        # 過去 30d 波動率
        c_30d = c.iloc[-30:]
        atr_30d = float((c_30d.diff().abs()).mean())
        atr_pct_30d = atr_30d / cur * 100 if cur > 0 else 999
        # RS 用 60d return vs SPY
        try:
            spy = ds.fetch_yf_history("SPY", period="3mo", interval="1d")
            if spy is not None and not spy.empty and len(spy) >= 63:
                stock_3mo = (cur / float(c.iloc[-63]) - 1) * 100 if len(c) >= 63 else 0
                spy_3mo = (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[-63]) - 1) * 100
                rs_diff = stock_3mo - spy_3mo
            else:
                rs_diff = 0
        except Exception:
            rs_diff = 0
        # 量增 (recent 5d vs 20d avg)
        vol_5d = float(v.iloc[-5:].mean())
        vol_20d_avg = float(v.iloc[-20:].mean())
        vol_ratio = vol_5d / vol_20d_avg if vol_20d_avg > 0 else 1
        # 距 200d MA
        if len(c) >= 200:
            ma_200 = float(c.rolling(200).mean().iloc[-1])
            ma_200_dist_pct = (cur / ma_200 - 1) * 100 if ma_200 > 0 else 0
        else:
            ma_200_dist_pct = 0
        # today_pct
        today_pct = (cur / float(c.iloc[-2]) - 1) * 100 if len(c) >= 2 and c.iloc[-2] > 0 else 0
        return {
            "current": round(cur, 2),
            "today_pct": round(today_pct, 2),
            "range_pct_6mo": round(range_pct, 2),
            "atr_pct_30d": round(atr_pct_30d, 2),
            "rs_3mo_diff_vs_spy": round(rs_diff, 2),
            "vol_ratio_recent": round(vol_ratio, 2),
            "ma_200d_dist_pct": round(ma_200_dist_pct, 2),
        }
    except Exception as e:
        print(f"[spec] {sid} metrics fail: {e}", flush=True)
        return {}


def _score_speculation(m: Dict) -> Dict:
    """投機股評分 — 強調 narrative + tight range + RS positive."""
    if not m:
        return {"score": 0, "label": "—", "reasons": []}
    score = 50
    reasons = []
    # tight range (整理時間長, 波動收縮)
    if m["range_pct_6mo"] <= 40:
        score += 10; reasons.append(f"✅ 6 個月窄幅 {m['range_pct_6mo']:.0f}% (盤整收縮)")
    elif m["range_pct_6mo"] > 80:
        score -= 5; reasons.append(f"⚠️ 6 個月範圍 {m['range_pct_6mo']:.0f}% 太寬")
    # ATR 小 → 等突破
    if m["atr_pct_30d"] <= 3.0:
        score += 5; reasons.append(f"✅ 30d ATR {m['atr_pct_30d']:.1f}% (波動收斂)")
    # RS 正向 (vs SPY)
    if m["rs_3mo_diff_vs_spy"] >= 5:
        score += 10; reasons.append(f"✅ 3 月相對 SPY +{m['rs_3mo_diff_vs_spy']:.1f}pp (強勢)")
    elif m["rs_3mo_diff_vs_spy"] >= 0:
        score += 5; reasons.append(f"➕ 3 月相對 SPY {m['rs_3mo_diff_vs_spy']:+.1f}pp")
    elif m["rs_3mo_diff_vs_spy"] <= -20:
        score -= 10; reasons.append(f"❌ 3 月跑輸 SPY {m['rs_3mo_diff_vs_spy']:.1f}pp")
    # 量增 (signal of accumulation)
    if m["vol_ratio_recent"] >= 1.3:
        score += 8; reasons.append(f"✅ 量增 {m['vol_ratio_recent']:.1f}x (吸籌中)")
    # 距 200d MA (站上 = stage 2, 跌破 = stage 4)
    if m["ma_200d_dist_pct"] > 0:
        score += 5; reasons.append(f"✅ 站上 200d MA")
    elif m["ma_200d_dist_pct"] < -30:
        score -= 8; reasons.append(f"⚠️ 距 200d MA {m['ma_200d_dist_pct']:.0f}% (深度回檔)")
    # 今日強勢 (突破)
    if m["today_pct"] >= 3:
        score += 5; reasons.append(f"✅ 今日 +{m['today_pct']:.1f}% (可能突破)")

    # 標籤
    score = max(0, min(100, score))
    if score >= 75:
        label = "🟢 SPEC-BUY"; emoji = "🟢"
    elif score >= 55:
        label = "🟡 SPEC-WATCH"; emoji = "🟡"
    else:
        label = "🔴 SPEC-AVOID"; emoji = "🔴"
    return {"score": score, "label": label, "emoji": emoji, "reasons": reasons}


def compute_speculation_picks(top_n: int = 10, min_score: int = 60) -> List[Dict]:
    """掃投機股 universe, 回 top N 評分."""
    universe = _flatten_universe()
    results = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_check_speculation_metrics, u["sym"]): u for u in universe}
        for fut in as_completed(futs):
            u = futs[fut]
            try:
                m = fut.result()
            except Exception:
                continue
            if not m:
                continue
            scored = _score_speculation(m)
            if scored["score"] < min_score:
                continue
            results.append({
                "symbol": u["sym"],
                "theme": u["theme"],
                **m,
                **scored,
            })
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results[:top_n]
