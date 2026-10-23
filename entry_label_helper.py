"""
entry_label_helper.py
輕量版入場標籤 — 給 US Top 10 / TW 族群龍頭批次加 entry_label.

跟 entry_evaluator.evaluate_entry 區別:
  - 不跑 Gemini AI (省 quota)
  - 不抓 peers (我們已知此股是族群 leader, 跳)
  - 只算技術 + RS + 估值 (3 個維度)
  - 速度: 單檔 ~1-2s (vs evaluate_entry ~5-10s)

API:
  quick_evaluate(symbol, market) -> dict {entry_label, entry_emoji, entry_score, entry_action}
  batch_evaluate(symbols, market) -> dict {symbol: result_dict}
"""
from __future__ import annotations

from typing import Dict, List, Optional
import data_sources as ds


# Bug fix: 原本用 @lru_cache 連失敗 (None) 一起快取 → 一次 yfinance 限流就讓那幾檔
# 在本 process 內永遠拿不到後綴 (詳見 entry_evaluator._tw_suffix_try 的註解)。
# 改成只快取成功值。
_SFX_CACHE: dict = {}


def _tw_suffix(stock_id: str) -> Optional[str]:
    """跟 entry_evaluator._tw_suffix_try 同邏輯, 試 .TW / .TWO."""
    hit = _SFX_CACHE.get(stock_id)
    if hit:
        return hit
    for sfx in [".TW", ".TWO"]:
        df = ds.fetch_yf_history(f"{stock_id}{sfx}", period="6mo", interval="1d")
        if df is not None and not df.empty and len(df) >= 60:
            _SFX_CACHE[stock_id] = sfx
            return sfx
    return None  # 不寫入快取


def _quick_snapshot(symbol: str, market: str) -> Optional[Dict]:
    """輕量技術 snapshot."""
    if market == "TW":
        sfx = _tw_suffix(symbol)
        if not sfx:
            return None
        ticker = f"{symbol}{sfx}"
    else:
        ticker = symbol
    daily = ds.fetch_yf_history(ticker, period="6mo", interval="1d")
    if daily is None or daily.empty or len(daily) < 20:
        return None
    try:
        close = daily["Close"].astype(float)
        vol = daily["Volume"].astype(float)
        high = daily["High"].astype(float)
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) >= 2 else last
        today_pct = (last / prev - 1) * 100 if prev > 0 else None
        # 量比
        if len(vol) >= 6:
            avg5 = float(vol.iloc[-6:-1].mean())
            vr = float(vol.iloc[-1]) / avg5 if avg5 > 0 else None
        else:
            vr = None
        # RSI(14)
        if len(close) >= 15:
            delta = close.diff()
            up = delta.clip(lower=0)
            dn = -delta.clip(upper=0)
            roll_up = up.ewm(alpha=1/14, min_periods=14).mean()
            roll_dn = dn.ewm(alpha=1/14, min_periods=14).mean()
            rs = roll_up / roll_dn.replace(0, 0.0001)
            rsi = float((100 - (100 / (1 + rs))).iloc[-1])
        else:
            rsi = None
        # MA20 / 60
        ma20_dist = None
        ma60_dist = None
        if len(close) >= 20:
            ma20 = float(close.rolling(20).mean().iloc[-1])
            ma20_dist = (last / ma20 - 1) * 100 if ma20 > 0 else None
        if len(close) >= 60:
            ma60 = float(close.rolling(60).mean().iloc[-1])
            ma60_dist = (last / ma60 - 1) * 100 if ma60 > 0 else None
        # 距 52w 高
        hi52 = float(high.max())
        from_52w = (last / hi52 - 1) * 100 if hi52 > 0 else None
        # 趨勢
        trend = None
        if ma20_dist is not None and ma60_dist is not None:
            if ma20_dist > 0 and ma60_dist > 0:
                trend = "uptrend"
            elif ma20_dist < 0 and ma60_dist < 0:
                trend = "downtrend"
            else:
                trend = "sideways"
        return {
            "today_pct": round(today_pct, 2) if today_pct is not None else None,
            "vol_ratio": round(vr, 2) if vr else None,
            "rsi14": round(rsi, 1) if rsi else None,
            "ma20_dist_pct": round(ma20_dist, 2) if ma20_dist is not None else None,
            "ma60_dist_pct": round(ma60_dist, 2) if ma60_dist is not None else None,
            "from_52w_high_pct": round(from_52w, 2) if from_52w is not None else None,
            "trend": trend,
            "ticker": ticker,
        }
    except Exception:
        return None


def _quick_rs(ticker: str, market: str) -> Optional[float]:
    """RS vs 大盤 (差分 pp)."""
    try:
        market_sym = "^TWII" if market == "TW" else "^GSPC"
        s = ds.fetch_yf_history(ticker, period="10d", interval="1d")
        m = ds.fetch_yf_history(market_sym, period="10d", interval="1d")
        if s is None or s.empty or len(s) < 6 or m is None or m.empty or len(m) < 6:
            return None
        s_pct = (float(s["Close"].iloc[-1]) / float(s["Close"].iloc[-6]) - 1) * 100
        m_pct = (float(m["Close"].iloc[-1]) / float(m["Close"].iloc[-6]) - 1) * 100
        return round(s_pct - m_pct, 2)
    except Exception:
        return None


def _quick_pe(symbol: str, market: str) -> Optional[float]:
    """快速取 PE. 台股用 stock_deep_analyzer, 美股用 fundamentals_us."""
    try:
        if market == "TW":
            try:
                import stock_deep_analyzer as sda
                pe_data = sda.compute_pe_vs_peers(symbol)
                return pe_data.get("stock_pe") if pe_data else None
            except Exception:
                return None
        else:
            try:
                import fundamentals_us as fu
                f = fu.fetch_us_fundamentals(symbol)
                return f.get("trailingPE")
            except Exception:
                return None
    except Exception:
        return None


def _label_from_score(score: int) -> Dict:
    """score 0-100 → label/emoji/action 對應."""
    if score >= 70:
        return {
            "entry_label": "BUY",
            "entry_emoji": "🟢",
            "entry_action": "加碼" if score >= 75 else "持有/小幅加碼",
        }
    if score >= 45:
        return {
            "entry_label": "WAIT",
            "entry_emoji": "🟡",
            "entry_action": "持平觀望",
        }
    return {
        "entry_label": "AVOID",
        "entry_emoji": "🔴",
        "entry_action": "減碼 1/3" if score >= 30 else "出場/停損",
    }


def quick_evaluate(symbol: str, market: str = "auto") -> Dict:
    """輕量入場評估. 回 dict, 失敗回 {entry_label: '—', entry_score: None, ...}.

    可批次跑 (US Top 10 / TW 族群龍頭 都 ok), 速度 ~1-2s/檔.
    """
    if market == "auto":
        # 簡化判斷
        s = (symbol or "").strip().upper()
        market = "TW" if s.isdigit() and 4 <= len(s) <= 5 else "US"
    if market not in ("TW", "US"):
        return {"entry_label": "—", "entry_emoji": "", "entry_score": None,
                "entry_action": "—"}

    snap = _quick_snapshot(symbol, market)
    if not snap:
        return {"entry_label": "—", "entry_emoji": "", "entry_score": None,
                "entry_action": "資料不足"}

    rs = _quick_rs(snap["ticker"], market)
    pe = _quick_pe(symbol, market)

    score = 50
    # 技術 (40 分權重)
    tp = snap.get("today_pct") or 0
    if tp >= 2.0:
        score += 8
    elif tp >= 0.5:
        score += 4
    elif tp <= -2.0:
        score -= 8
    elif tp <= -0.5:
        score -= 4
    trend = snap.get("trend")
    if trend == "uptrend":
        score += 8
    elif trend == "downtrend":
        score -= 10
    rsi = snap.get("rsi14")
    if rsi is not None:
        if rsi >= 75:
            score -= 5
        elif rsi <= 30:
            score += 4
    fh = snap.get("from_52w_high_pct")
    if fh is not None:
        # Bug fix: 這裡原本是 `fh > -3 → score -= 3` (追高風險), 但 entry_evaluator.py
        # 已經把同一個條件明確標成「Bug 1: 距 52w 高改 +3 (強勢突破)」並改成加分, 只在
        # RSI ≥ 80 時才扣。兩支對同一條件給出相反 6 分的評價, 而 actionable_picks 的
        # entry_score 取自本檔 (batch_evaluate), 又是 ai_confidence_scorer 算 orig_f 的
        # 基底 → 強勢突破股在 Top 10 排序裡反而被倒扣。改成與 entry_evaluator 一致。
        if fh > -3:
            if rsi is not None and rsi >= 80:
                score -= 2   # 高點 + RSI 超買
            else:
                score += 3   # 強勢突破
    vr = snap.get("vol_ratio")
    if vr and vr >= 1.5:
        score += 4
    elif vr and vr < 0.7:
        score -= 2

    # RS vs 大盤 (15 分)
    if rs is not None:
        if rs >= 1.5:
            score += 6
        elif rs <= -1.5:
            score -= 5

    # 估值 (10 分)
    if pe is not None and pe > 0:
        if 10 <= pe <= 25:
            score += 4
        elif pe > 40:
            score -= 4

    # soft cap (同 evaluate_entry)
    # Bug fix: entry_evaluator 已在「Bug 7: soft cap 反向 — 不再壓抑 > 75」把上方的
    # cap 移除、並把下方拉抬從 0.6 降到 0.3 (留住 AVOID 訊號), 本檔還留著舊寫法 →
    # 同一檔股票兩支算出來的 entry_score 會不一致, 好股票在這裡被壓分。對齊之。
    if score < 25:
        excess = 25 - score
        score += int(excess * 0.3)
    score = max(0, min(100, score))

    label_info = _label_from_score(score)
    return {
        "entry_label": label_info["entry_label"],
        "entry_emoji": label_info["entry_emoji"],
        "entry_score": score,
        "entry_action": label_info["entry_action"],
        "_pe": pe,
        "_rs": rs,
    }


def batch_evaluate(symbol_market_pairs: List, max_workers: int = 8) -> Dict[str, Dict]:
    """批次跑 quick_evaluate. symbol_market_pairs = [(sym, market), ...].
    回 {symbol: result_dict}.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(quick_evaluate, s, m): s for s, m in symbol_market_pairs}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                results[sym] = fut.result()
            except Exception:
                results[sym] = {"entry_label": "—", "entry_emoji": "", "entry_score": None}
    return results
