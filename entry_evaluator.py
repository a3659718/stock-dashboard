"""
entry_evaluator.py
個股入場評估 — 輸入股票代號, 評估「當下能否入場」.

支援台股 (4 碼) 跟美股 (英文 ticker), 自動偵測.

評估維度:
  1. 個股當下: 漲跌% / 量比 / RSI / 距 MA20/60 / 距 52w high/low
  2. 同族群表現: 取 5 檔 peer + sector 均漲 + 上漲家數比
  3. 相對大盤 RS: 個股 5d% / TWII or SPY 5d%
  4. 基本面: PE / 估值標籤 + EPS (台股: YoY + 月營收, 美股: forward PE / PEG / marketCap)
  5. 結論: BUY / WAIT / AVOID + 多條理由 + 0-100 score

API:
  evaluate_entry(symbol, market="auto") -> dict (見回傳結構)
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

import data_sources as ds


# ---------------------------------------------------------------------------
# 市場偵測
# ---------------------------------------------------------------------------
def detect_market(symbol: str) -> str:
    """4-5 位數字 → TW (含 ETF 00xx); 4 數字+1 字母 → TW 興櫃; 其他 → US.

    M1 fix: 興櫃股 (e.g., "1234A") 被 isdigit() 擋掉, 加 regex 支援.
    """
    import re as _re
    s = (symbol or "").strip().upper()
    if not s:
        return "UNKNOWN"
    if s.isdigit() and 4 <= len(s) <= 5:
        return "TW"
    # 興櫃: 4 數字 + 1 大寫字母 (e.g., 1234A)
    if _re.match(r"^\d{4}[A-Z]$", s):
        return "TW"
    return "US"


from functools import lru_cache


@lru_cache(maxsize=512)
def _tw_suffix_try(stock_id: str) -> Optional[str]:
    """台股 yfinance 後綴: 上市 .TW / 上櫃 .TWO. 試到拿到非空 df 為止.

    M3 fix: lru_cache 避免一次評估內被呼叫 14+ 次都重打 yfinance.
    cache 跨 evaluate_entry 共用, 同個股 5d 內結果穩定 (上市/上櫃不會變).
    """
    import data_sources as _ds
    for sfx in [".TW", ".TWO"]:
        df = _ds.fetch_yf_history(f"{stock_id}{sfx}", period="6mo", interval="1d")
        if df is not None and not df.empty and len(df) >= 60:
            return sfx
    return None


# ---------------------------------------------------------------------------
# 個股本身的指標 (technical snapshot)
# ---------------------------------------------------------------------------
def _stock_snapshot(symbol: str, market: str) -> Dict:
    """抓個股 6mo daily + today intraday, 算 today_pct / vol_ratio / RSI /
    距 MA20 / 距 MA60 / 距 52w high/low.

    回傳 dict (含 None 值表示未抓到), 不 raise.
    """
    out = {
        "symbol": symbol, "market": market,
        "current": None, "prev_close": None,
        "today_pct": None, "vol_ratio": None,
        "rsi14": None, "ma20_dist_pct": None, "ma60_dist_pct": None,
        "from_52w_high_pct": None, "from_52w_low_pct": None,
        "trend": None,  # "uptrend" / "downtrend" / "sideways"
    }
    # yfinance ticker 補後綴
    if market == "TW":
        sfx = _tw_suffix_try(symbol)
        if not sfx:
            return out
        ticker = f"{symbol}{sfx}"
    else:
        ticker = symbol

    daily = ds.fetch_yf_history(ticker, period="6mo", interval="1d")
    if daily is None or daily.empty or len(daily) < 20:
        return out
    try:
        import pandas as pd
        close = daily["Close"].astype(float)
        vol = daily["Volume"].astype(float)
        high = daily["High"].astype(float)
        low = daily["Low"].astype(float)

        last = float(close.iloc[-1])
        prev = float(close.iloc[-2]) if len(close) >= 2 else last
        out["current"] = round(last, 2)
        out["prev_close"] = round(prev, 2)
        # M6 fix: prev<=0 是「沒昨收資料」(新上市/yf 異常), 設 None 不要混淆「持平」
        out["today_pct"] = round((last / prev - 1) * 100, 2) if prev > 0 else None

        # 量比 (今日量 / 過去 5 日均量)
        if len(vol) >= 6:
            avg5 = float(vol.iloc[-6:-1].mean())
            if avg5 > 0:
                out["vol_ratio"] = round(float(vol.iloc[-1]) / avg5, 2)

        # RSI(14)
        if len(close) >= 15:
            delta = close.diff()
            up = delta.clip(lower=0)
            dn = -delta.clip(upper=0)
            roll_up = up.ewm(alpha=1/14, min_periods=14).mean()
            roll_dn = dn.ewm(alpha=1/14, min_periods=14).mean()
            rs = roll_up / roll_dn.replace(0, 0.0001)
            rsi_series = 100 - (100 / (1 + rs))
            out["rsi14"] = round(float(rsi_series.iloc[-1]), 1)

        # 距 MA20 / MA60
        if len(close) >= 20:
            ma20 = float(close.rolling(20).mean().iloc[-1])
            out["ma20_dist_pct"] = round((last / ma20 - 1) * 100, 2) if ma20 > 0 else 0
        if len(close) >= 60:
            ma60 = float(close.rolling(60).mean().iloc[-1])
            out["ma60_dist_pct"] = round((last / ma60 - 1) * 100, 2) if ma60 > 0 else 0

        # 52w high/low (6 個月內近似, 嚴格 52w 要更長)
        hi52 = float(high.max())
        lo52 = float(low.min())
        out["from_52w_high_pct"] = round((last / hi52 - 1) * 100, 2) if hi52 > 0 else 0
        out["from_52w_low_pct"] = round((last / lo52 - 1) * 100, 2) if lo52 > 0 else 0

        # 趨勢: MA20 > MA60 = uptrend
        if out["ma20_dist_pct"] is not None and out["ma60_dist_pct"] is not None:
            if out["ma20_dist_pct"] > 0 and out["ma60_dist_pct"] > 0:
                out["trend"] = "uptrend"
            elif out["ma20_dist_pct"] < 0 and out["ma60_dist_pct"] < 0:
                out["trend"] = "downtrend"
            else:
                out["trend"] = "sideways"
    except Exception as e:
        print(f"[entry_eval] {ticker} snapshot 計算失敗: {e}", flush=True)
    return out


# ---------------------------------------------------------------------------
# 同族群 peers (取 5 檔)
# ---------------------------------------------------------------------------
def _tw_peers(stock_id: str) -> Dict:
    """台股 peers: 用 FinMind industry_category 取同產業前 5 檔 (按市值近似不可用,
    這裡取同產業前 5 檔)."""
    out = {"sector": None, "peers": [], "sector_avg_pct": None, "up_ratio": None}
    try:
        info = ds.get_taiwan_stock_info()
        if info is None or info.empty:
            return out
        row = info[info["stock_id"].astype(str) == str(stock_id)]
        if row.empty:
            return out
        ind = str(row.iloc[0].get("industry_category", "") or "")
        if not ind:
            return out
        out["sector"] = ind
        peers_df = info[info["industry_category"] == ind]
        peers_df = peers_df[peers_df["stock_id"].astype(str) != str(stock_id)]
        peer_ids = peers_df["stock_id"].astype(str).head(20).tolist()  # 抓 20 檔, 取漲幅前 5

        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_quick_pct, pid, "TW"): pid for pid in peer_ids}
            for f in as_completed(futs):
                r = f.result()
                if r:
                    results.append(r)
        # 排序按今日% 降冪取 top 5
        results.sort(key=lambda x: x.get("today_pct", -999), reverse=True)
        out["peers"] = results[:5]
        # 整族群均漲 + 上漲比率
        all_pct = [r["today_pct"] for r in results if r.get("today_pct") is not None]
        if all_pct:
            out["sector_avg_pct"] = round(sum(all_pct) / len(all_pct), 2)
            out["up_ratio"] = round(sum(1 for p in all_pct if p > 0) / len(all_pct), 2)
    except Exception as e:
        print(f"[entry_eval] tw_peers {stock_id} 失敗: {e}", flush=True)
    return out


def _us_peers(ticker: str) -> Dict:
    """美股 peers: 用 yfinance ticker.info 取 sector, 對該 sector 的 ETF + 同 sector
    熱門個股 (寫死 mapping). 簡化版."""
    out = {"sector": None, "peers": [], "sector_avg_pct": None, "up_ratio": None}
    try:
        import fundamentals_us as fu
        f = fu.fetch_us_fundamentals(ticker)
        sec = f.get("sector") or ""
        out["sector"] = sec
        # 簡化的 sector → peer list mapping (用戶常見大型股)
        SECTOR_PEERS = {
            "Technology":          ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMD", "AVGO", "ORCL"],
            "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "T", "VZ"],
            "Consumer Cyclical":   ["AMZN", "TSLA", "HD", "NKE", "MCD", "SBUX"],
            "Financial Services":  ["JPM", "BAC", "WFC", "GS", "MS", "V", "MA"],
            "Healthcare":          ["UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK"],
            "Energy":              ["XOM", "CVX", "COP", "SLB", "OXY"],
            "Industrials":         ["CAT", "BA", "GE", "HON", "UPS", "RTX"],
            "Consumer Defensive":  ["WMT", "PG", "KO", "PEP", "COST"],
            "Real Estate":         ["AMT", "PLD", "CCI", "EQIX"],
            "Utilities":           ["NEE", "DUK", "SO", "AEP"],
            "Basic Materials":     ["LIN", "APD", "SHW", "FCX"],
        }
        peer_list = SECTOR_PEERS.get(sec, [])
        peer_list = [p for p in peer_list if p != ticker.upper()][:8]
        if not peer_list:
            return out
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_quick_pct, p, "US"): p for p in peer_list}
            for fut in as_completed(futs):
                r = fut.result()
                if r:
                    results.append(r)
        results.sort(key=lambda x: x.get("today_pct", -999), reverse=True)
        out["peers"] = results[:5]
        all_pct = [r["today_pct"] for r in results if r.get("today_pct") is not None]
        if all_pct:
            out["sector_avg_pct"] = round(sum(all_pct) / len(all_pct), 2)
            out["up_ratio"] = round(sum(1 for p in all_pct if p > 0) / len(all_pct), 2)
    except Exception as e:
        print(f"[entry_eval] us_peers {ticker} 失敗: {e}", flush=True)
    return out


def _quick_pct(stock_id: str, market: str) -> Optional[Dict]:
    """快速抓單檔今日漲跌 + 量比 (供 peers 用)."""
    try:
        if market == "TW":
            sfx = _tw_suffix_try(stock_id)
            if not sfx:
                return None
            ticker = f"{stock_id}{sfx}"
        else:
            ticker = stock_id
        df = ds.fetch_yf_history(ticker, period="10d", interval="1d")
        if df is None or df.empty or len(df) < 2:
            return None
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        pct = (last / prev - 1) * 100 if prev > 0 else 0
        avg5 = float(vol.iloc[-6:-1].mean()) if len(vol) >= 6 else 0
        vr = float(vol.iloc[-1]) / avg5 if avg5 > 0 else None
        return {
            "stock_id": stock_id,
            "current": round(last, 2),
            "today_pct": round(pct, 2),
            "vol_ratio": round(vr, 2) if vr else None,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 相對大盤 RS
# ---------------------------------------------------------------------------
def _market_rs(stock_snap: Dict, market: str) -> Optional[float]:
    """個股 5d% − 大盤 5d% — 相對強度差分 (pp).

    H2 fix: 改用差分而非比值, 避免大盤接近 0 時失真 / 符號反向.
       >  0  pp : 跑贏大盤 (個股強)
       =  0  pp : 跟大盤同步
       <  0  pp : 跑輸大盤
    使用方式: rs > 1.5 強跑贏 / rs > 0 跑贏 / rs < -1.5 強跑輸.
    """
    try:
        market_sym = "^TWII" if market == "TW" else "^GSPC"
        m_df = ds.fetch_yf_history(market_sym, period="10d", interval="1d")
        if m_df is None or m_df.empty or len(m_df) < 6:
            return None
        m_close = m_df["Close"].astype(float)
        m_5d_pct = (float(m_close.iloc[-1]) / float(m_close.iloc[-6]) - 1) * 100
        # 個股 5d
        ticker_sym = stock_snap.get("symbol")
        if market == "TW":
            sfx = _tw_suffix_try(ticker_sym)
            if not sfx:
                return None
            ticker_sym = f"{ticker_sym}{sfx}"
        s_df = ds.fetch_yf_history(ticker_sym, period="10d", interval="1d")
        if s_df is None or s_df.empty or len(s_df) < 6:
            return None
        s_close = s_df["Close"].astype(float)
        s_5d_pct = (float(s_close.iloc[-1]) / float(s_close.iloc[-6]) - 1) * 100
        # 差分 (pp), 無除 0 風險, 符號正確
        return round(s_5d_pct - m_5d_pct, 2)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 基本面 (PE/EPS)
# ---------------------------------------------------------------------------
def _fundamentals(symbol: str, market: str) -> Dict:
    """抓基本面: 台股用 stock_deep_analyzer / FinMind, 美股用 yfinance."""
    out = {
        "pe": None, "pe_label": "—",
        "forward_pe": None, "peg": None,
        "eps": None, "eps_yoy_pct": None, "revenue_yoy_pct": None,
        "marketcap": "—", "industry": None,
    }
    if market == "TW":
        try:
            import stock_deep_analyzer as sda
            pe_data = sda.compute_pe_vs_peers(symbol)
            if pe_data:
                out["pe"] = pe_data.get("stock_pe")
                out["industry"] = pe_data.get("industry")
                out["pe_label"] = pe_data.get("valuation_label", "—")
            # EPS YoY 從 fundamental_metrics 抓
            # Bug fix: compute_financial_summary 不存在, 改用 fetch_fundamental_metrics
            try:
                fin = sda.fetch_fundamental_metrics(symbol)
                if fin:
                    out["eps"] = fin.get("eps_latest") or fin.get("latest_eps")
                    out["eps_yoy_pct"] = fin.get("eps_yoy_pct") or fin.get("eps_yoy")
                    out["revenue_yoy_pct"] = fin.get("revenue_yoy_pct") or fin.get("revenue_yoy")
            except Exception:
                pass
        except Exception as e:
            print(f"[entry_eval] tw fundamentals {symbol} 失敗: {e}", flush=True)
    else:
        try:
            import fundamentals_us as fu
            f = fu.fetch_us_fundamentals(symbol)
            out["pe"] = f.get("trailingPE")
            out["pe_label"] = fu.fmt_pe_label(f.get("trailingPE"))
            out["forward_pe"] = f.get("forwardPE")
            out["peg"] = f.get("pegRatio")
            out["eps"] = f.get("trailingEps")
            out["industry"] = f.get("industry")
            out["marketcap"] = fu.fmt_marketcap(f.get("marketCap"))
            # 新增: 美股財報日 + EPS / 營收 YoY
            out["earnings_date"] = f.get("earningsDate")
            # earningsGrowth / revenueGrowth 是小數 (0.25 = +25%), 轉百分比
            eg = f.get("earningsGrowth")
            rg = f.get("revenueGrowth")
            out["eps_yoy_pct"] = round(float(eg) * 100, 1) if eg is not None else None
            out["revenue_yoy_pct"] = round(float(rg) * 100, 1) if rg is not None else None
        except Exception as e:
            print(f"[entry_eval] us fundamentals {symbol} 失敗: {e}", flush=True)
    return out


# ---------------------------------------------------------------------------
# Gemini AI 結論 (新)
# ---------------------------------------------------------------------------
def _ai_verdict(symbol: str, snap: Dict, peers: Dict, rs, fund: Dict, verdict: Dict) -> Optional[str]:
    """用 Gemini 整合所有資料給 2-3 句結論. 失敗回 None, 不 raise."""
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return None
        # 摘要 context
        ctx_lines = [
            f"股票: {symbol}",
            f"當下漲跌: {snap.get('today_pct'):+.2f}% (現價 {snap.get('current')})" if snap.get('today_pct') is not None else f"當下價格: {snap.get('current')}",
        ]
        if snap.get("trend"):
            ctx_lines.append(f"趨勢: {snap['trend']} | RSI {snap.get('rsi14')} | 距 52w 高 {snap.get('from_52w_high_pct')}%")
        if peers.get("sector"):
            ctx_lines.append(
                f"族群 ({peers['sector']}): 均漲 {peers.get('sector_avg_pct')}% / 上漲比 {peers.get('up_ratio')}"
            )
        if rs is not None:
            ctx_lines.append(f"相對大盤 RS: {rs:+.2f}pp")
        if fund.get("pe"):
            ctx_lines.append(f"PE {fund['pe']} / Forward PE {fund.get('forward_pe')} / PEG {fund.get('peg')}")
        if fund.get("eps_yoy_pct") is not None:
            ctx_lines.append(f"EPS YoY {fund['eps_yoy_pct']:+.1f}%")
        if fund.get("revenue_yoy_pct") is not None:
            ctx_lines.append(f"營收 YoY {fund['revenue_yoy_pct']:+.1f}%")
        if fund.get("earnings_date"):
            ctx_lines.append(f"下次財報: {fund['earnings_date']}")
        ctx_lines.append(f"系統評分: {verdict.get('score')}/100 → {verdict.get('verdict')}")

        ctx = "\n".join(ctx_lines)
        prompt = (
            "你是專業股票分析師. 根據以下資料, 用繁體中文白話給我可行動的進出場建議。\n"
            "嚴格照這個格式回 (每行一項, 精簡, 不要再列一堆數據):\n"
            "結論: <BUY/WAIT/AVOID 一句話, 現在能不能進場>\n"
            "進場時機: <現在就進/拉回到某價位再進/突破某價位再進, 給具體參考價>\n"
            "目標價: <短期 + 中期 2 個目標價位, 或說明依據 (阻力/前高/R:R)>\n"
            "停損: <具體停損價位或條件, 例如跌破 MA20 / 某價>\n"
            "出場時機: <達標分批出 / 跌破關鍵位出 / 訊號轉弱出>\n"
            "關鍵風險或催化劑: <一句>\n\n"
            f"{ctx}"
        )
        # 直接呼叫 google.generativeai (ai_analyzer 沒通用 chat 函式)
        try:
            import google.generativeai as genai
            genai.configure(api_key=ai_analyzer.get_gemini_key())
            model = genai.GenerativeModel("gemini-2.0-flash-exp")
            resp = model.generate_content(prompt)
            text = (resp.text or "").strip()
            return text if text else None
        except Exception as e:
            print(f"[entry_eval] Gemini call failed: {e}", flush=True)
            return None
    except Exception as e:
        print(f"[entry_eval] AI verdict failed: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# 結論引擎
# ---------------------------------------------------------------------------
def _verdict(snap: Dict, peers: Dict, rs: Optional[float], fund: Dict) -> Dict:
    """依各維度評分 + 給出 BUY/WAIT/AVOID 結論. 回 dict.

    Gap 3 修 7 bugs:
      1. 距 52w 高 < -3% 改 +3 (突破訊號)
      2. 今日漲跌 ±2% 權重 ±10 → ±5 (短線雜訊不壓過趨勢)
      3. RSI 超買門檻 75 → 80, 配合量比看
      4. PE → PEG fallback (高成長股不誤罰)
      5. 趨勢對稱 (downtrend -10 → -8)
      6. 加籌碼面 (法人連買/賣) — 台股關鍵
      7. soft cap 反向 — 不壓 > 75 的好股票
    """
    import math
    reasons: List[str] = []
    score = 50

    # Bug 2: 今日漲跌權重減半
    tp = snap.get("today_pct") or 0
    if tp >= 2.0:
        score += 5; reasons.append(f"✅ 今日強漲 +{tp:.2f}%")
    elif tp >= 0.5:
        score += 2; reasons.append(f"➕ 小幅紅 +{tp:.2f}%")
    elif tp <= -2.0:
        score -= 5; reasons.append(f"❌ 今日大跌 {tp:.2f}%")
    elif tp <= -0.5:
        score -= 2; reasons.append(f"➖ 小幅黑 {tp:.2f}%")

    vr = snap.get("vol_ratio")
    if vr and vr >= 1.5:
        score += 5; reasons.append(f"✅ 量增 {vr:.1f}x (資金關注)")
    elif vr and vr < 0.7:
        score -= 3; reasons.append(f"➖ 量縮 {vr:.1f}x")

    # Bug 5: 趨勢對稱
    trend = snap.get("trend")
    if trend == "uptrend":
        score += 8; reasons.append("✅ 多頭排列")
    elif trend == "downtrend":
        score -= 8; reasons.append("❌ 空頭排列")

    # Bug 3: RSI 超買門檻 75 → 80, 配合量比
    rsi = snap.get("rsi14")
    if rsi is not None:
        if rsi >= 80 and vr and vr >= 1.5:
            score -= 5; reasons.append(f"⚠️ RSI {rsi:.0f}+放量 (回檔風險高)")
        elif rsi >= 80:
            score -= 2; reasons.append(f"⚠️ RSI {rsi:.0f} 偏高")
        elif rsi <= 30:
            score += 4; reasons.append(f"💡 RSI {rsi:.0f} 超賣, 反彈機會")

    # Bug 1: 距 52w 高改 +3 (強勢突破)
    fh = snap.get("from_52w_high_pct")
    if fh is not None:
        if fh > -3:
            if rsi and rsi >= 80:
                score -= 2; reasons.append(f"⚠️ 高點 {fh:.1f}% + RSI 超買")
            else:
                score += 3; reasons.append(f"✅ 距高點 {fh:.1f}% (強勢突破)")
        elif fh < -30:
            reasons.append(f"📉 距高點 {fh:.1f}% (深度回檔)")

    sap = peers.get("sector_avg_pct")
    upr = peers.get("up_ratio")
    if sap is not None and upr is not None:
        if sap >= 1.5 and upr >= 0.6:
            score += 8; reasons.append(f"✅ 同族群齊漲 均+{sap:.2f}% / 上漲 {upr*100:.0f}%")
        elif sap <= -1.0:
            score -= 8; reasons.append(f"❌ 同族群同步走弱 均{sap:+.2f}%")

    if rs is not None:
        if rs >= 1.5:
            score += 6; reasons.append(f"✅ 相對大盤強 (+{rs:.2f}pp)")
        elif rs <= -1.5:
            score -= 5; reasons.append(f"➖ 跑輸大盤 ({rs:+.2f}pp)")

    # Bug 4: PE → PEG fallback
    pe = fund.get("pe")
    peg = fund.get("peg")
    if peg is not None and 0 < peg < 100:
        if peg <= 1.0:
            score += 5; reasons.append(f"✅ PEG {peg:.2f} (合理成長估值)")
        elif peg > 3:
            score -= 4; reasons.append(f"⚠️ PEG {peg:.2f} 偏高")
    elif pe is not None and pe > 0:
        if 10 <= pe <= 25:
            score += 4; reasons.append(f"✅ PE {pe:.1f} 合理")
        elif pe > 50:  # 40 → 50 (高成長股 40+ 常見)
            score -= 4; reasons.append(f"⚠️ PE {pe:.1f} 偏高")
    eyoy = fund.get("eps_yoy_pct")
    if eyoy is not None and not (isinstance(eyoy, float) and math.isnan(eyoy)):
        if eyoy >= 30:
            score += 5; reasons.append(f"✅ EPS YoY +{eyoy:.1f}% 高成長")
        elif eyoy <= -20:
            score -= 5; reasons.append(f"❌ EPS YoY {eyoy:.1f}% 衰退")

    # Bug 6 (新): 加籌碼面 (snap 可提供 foreign_streak_days)
    inst_streak = snap.get("foreign_streak_days") or 0
    inst_5d_pct = snap.get("foreign_5d_pct_outstanding") or 0
    if inst_streak >= 5 and inst_5d_pct > 0.5:
        score += 6; reasons.append(f"✅ 外資連買 {inst_streak} 日 ({inst_5d_pct:.1f}% 流通)")
    elif inst_streak <= -5 and inst_5d_pct < -0.5:
        score -= 6; reasons.append(f"❌ 外資連賣 {abs(inst_streak)} 日")
    main_bnet = snap.get("main_broker_net_pct") or 0
    if main_bnet >= 1.0:
        score += 3; reasons.append(f"✅ 主力券商買超 +{main_bnet:.1f}%")
    elif main_bnet <= -1.0:
        score -= 3; reasons.append(f"➖ 主力券商賣超 {main_bnet:.1f}%")

    # Bug 7: soft cap 反向 — 不再壓抑 > 75, AVOID 拉抬縮小
    if score < 25:
        excess = 25 - score
        score += int(excess * 0.3)  # 從 0.6 → 0.3, 留 AVOID 訊號

    # === 結論 ===
    score = max(0, min(100, score))
    if score >= 70:
        verdict = "BUY"
        v_emoji = "🟢"
    elif score >= 45:
        verdict = "WAIT"
        v_emoji = "🟡"
    else:
        verdict = "AVOID"
        v_emoji = "🔴"

    # 持倉決策建議 (給已有持倉的人, 跟 verdict 是進場決策不一樣)
    if score >= 75:
        position_action = "加碼"
        pa_emoji = "🟢⬆️"
        pa_detail = "強勢動能, 可考慮加碼 (建議分批)"
    elif score >= 60:
        position_action = "持有/小幅加碼"
        pa_emoji = "🟢"
        pa_detail = "趨勢延續, 持有為主, 拉回可小幅加碼"
    elif score >= 45:
        position_action = "持平觀望"
        pa_emoji = "🟡"
        pa_detail = "訊號參半, 暫不調整部位"
    elif score >= 30:
        position_action = "減碼 1/3"
        pa_emoji = "🟠⬇️"
        pa_detail = "弱勢訊號, 建議減碼 1/3 降低風險"
    else:
        position_action = "出場/停損"
        pa_emoji = "🔴⬇️⬇️"
        pa_detail = "多項負面訊號, 建議出場或設緊停損"

    return {
        "verdict": verdict,
        "verdict_emoji": v_emoji,
        "score": score,
        "reasons": reasons,
        # 新增: 持倉決策
        "position_action": position_action,
        "position_emoji": pa_emoji,
        "position_detail": pa_detail,
    }


# ---------------------------------------------------------------------------
# Gemini AI 結論 (新)
# ---------------------------------------------------------------------------
def _ai_verdict(symbol, snap, peers, rs, fund, verdict):
    """用 Gemini 整合所有資料給 2-3 句結論. 失敗回 None, 不 raise."""
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return None
        ctx_lines = [f"股票: {symbol}"]
        if snap.get('today_pct') is not None:
            ctx_lines.append(f"當下漲跌: {snap['today_pct']:+.2f}% (現價 {snap.get('current')})")
        else:
            ctx_lines.append(f"當下價格: {snap.get('current')}")
        if snap.get("trend"):
            ctx_lines.append(f"趨勢: {snap['trend']} | RSI {snap.get('rsi14')} | 距 52w 高 {snap.get('from_52w_high_pct')}%")
        if peers.get("sector"):
            ctx_lines.append(f"族群 ({peers['sector']}): 均漲 {peers.get('sector_avg_pct')}% / 上漲比 {peers.get('up_ratio')}")
        if rs is not None:
            ctx_lines.append(f"相對大盤 RS: {rs:+.2f}pp")
        if fund.get("pe"):
            ctx_lines.append(f"PE {fund['pe']} / Forward PE {fund.get('forward_pe')} / PEG {fund.get('peg')}")
        if fund.get("eps_yoy_pct") is not None:
            ctx_lines.append(f"EPS YoY {fund['eps_yoy_pct']:+.1f}%")
        if fund.get("revenue_yoy_pct") is not None:
            ctx_lines.append(f"營收 YoY {fund['revenue_yoy_pct']:+.1f}%")
        if fund.get("earnings_date"):
            ctx_lines.append(f"下次財報: {fund['earnings_date']}")
        ctx_lines.append(f"系統評分: {verdict.get('score')}/100 → {verdict.get('verdict')}")

        ctx = "\n".join(ctx_lines)
        prompt = (
            "你是專業股票分析師. 根據以下資料, 用繁體中文白話給我可行動的進出場建議。\n"
            "嚴格照這個格式回 (每行一項, 精簡, 不要再列一堆數據):\n"
            "結論: <BUY/WAIT/AVOID 一句話, 現在能不能進場>\n"
            "進場時機: <現在就進/拉回到某價位再進/突破某價位再進, 給具體參考價>\n"
            "目標價: <短期 + 中期 2 個目標價位, 或說明依據 (阻力/前高/R:R)>\n"
            "停損: <具體停損價位或條件, 例如跌破 MA20 / 某價>\n"
            "出場時機: <達標分批出 / 跌破關鍵位出 / 訊號轉弱出>\n"
            "關鍵風險或催化劑: <一句>\n\n"
            f"{ctx}"
        )
        try:
            import google.generativeai as genai
            genai.configure(api_key=ai_analyzer.get_gemini_key())
            model = genai.GenerativeModel("gemini-2.0-flash-exp")
            resp = model.generate_content(prompt)
            text = (resp.text or "").strip()
            return text if text else None
        except Exception as e:
            print(f"[entry_eval] Gemini call failed: {e}", flush=True)
            return None
    except Exception as e:
        print(f"[entry_eval] AI verdict failed: {e}", flush=True)
        return None


# 主入口
def evaluate_entry(symbol, market="auto"):
    """完整評估 (含 AI). 回 dict."""
    try:
        m = detect_market(symbol)
        snap = _stock_snapshot(symbol, m)
        snap = _enrich_chip_signal(snap, symbol, m)  # Bug 6 fix
        peers = _tw_peers(symbol) if m == "TW" else _us_peers(symbol)
        rs = _market_rs(snap, m)
        fund = _fundamentals(symbol, m)
        v = _verdict(snap, peers, rs, fund)
        ai_text = _ai_verdict(symbol, snap, peers, rs, fund, v)
        return {
            "symbol": symbol,
            "market": m,
            "snap": snap,
            "peers": peers,
            "rs": {"market_rs_pp": rs, "market_label": "TWII" if m == "TW" else "SPY"},
            "fundamentals": fund,
            "verdict": v,
            "ai_summary": ai_text,
        }
    except Exception as e:
        return {"symbol": symbol, "err": f"{type(e).__name__}: {e}"}


def _enrich_chip_signal(snap: Dict, symbol: str, market: str) -> Dict:
    """為 _verdict Bug 6 補籌碼面資料 — 抓外資連買/賣天數 + 5d 累積 % of 流通.
    台股才有, 美股 skip.
    """
    if market != "TW":
        return snap
    try:
        import datetime as _dt
        import data_sources as _ds
        end = _dt.date.today().strftime("%Y-%m-%d")
        start = (_dt.date.today() - _dt.timedelta(days=20)).strftime("%Y-%m-%d")
        df = _ds.fetch_institutional_universe((symbol,), start, end)
        if df is None or df.empty or "name" not in df.columns:
            return snap
        foreign = df[df["name"].astype(str).str.contains("Foreign|外資", na=False, regex=True)]
        if foreign.empty or "buy" not in foreign.columns:
            return snap
        foreign = foreign.copy()
        foreign["net_lots"] = (
            foreign["buy"].astype(float) - foreign["sell"].astype(float)
        ) / 1000.0
        daily_net = foreign.groupby("date")["net_lots"].sum().sort_index()
        if len(daily_net) < 5:
            return snap
        net_5d = daily_net.tail(5)
        if (net_5d > 0).all():
            streak = 5
        elif (net_5d < 0).all():
            streak = -5
        else:
            streak = 0
        snap["foreign_streak_days"] = streak
        cum_5d = net_5d.sum()
        # Bug fix: 原本固定除以 50000 張股本, 對股本非 5 萬張的股票 % 嚴重失準.
        #          改抓實際流通張數 (fetch_shares_outstanding 回張, 單位一致), 抓不到才退回 50000 proxy.
        _lots = 50000.0
        try:
            _so = _ds.fetch_shares_outstanding((symbol,))
            if _so.get(symbol, 0) and _so[symbol] > 0:
                _lots = float(_so[symbol])
        except Exception:
            pass
        snap["foreign_5d_pct_outstanding"] = round(cum_5d / _lots * 100, 2) if abs(cum_5d) > 0 else 0
    except Exception as _e:
        print(f"[entry_eval] enrich chip {symbol} failed: {_e}", flush=True)
    return snap


def quick_evaluate(symbol, market="TW"):
    """輕量版 (無 AI). 回 {entry_label, entry_emoji, entry_score, entry_action}."""
    try:
        m = detect_market(symbol)
        snap = _stock_snapshot(symbol, m)
        snap = _enrich_chip_signal(snap, symbol, m)
        peers = _tw_peers(symbol) if m == "TW" else _us_peers(symbol)
        rs = _market_rs(snap, m)
        fund = _fundamentals(symbol, m)
        v = _verdict(snap, peers, rs, fund)
        return {
            "entry_label": v.get("verdict"),
            "entry_emoji": v.get("verdict_emoji"),
            "entry_score": v.get("score"),
            "entry_action": v.get("position_action"),
        }
    except Exception:
        return {"entry_label": "—", "entry_emoji": "", "entry_score": None, "entry_action": "—"}
