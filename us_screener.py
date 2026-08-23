"""
us_screener.py
美股推薦：技術突破 + 動能 + 新聞題材熱度 + Fear & Greed / 板塊輪動。

最後輸出 Top 5。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List

import pandas as pd
import streamlit as st

import data_sources as ds

# ---------------------------------------------------------------------------
# 候選池 (S&P 100 + 高熱度科技 / AI 個股，可在 secrets 自定 watchlist)
# ---------------------------------------------------------------------------
DEFAULT_UNIVERSE = [
    # Mega cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "AMD", "ADBE",
    "CRM", "ORCL", "INTC", "QCOM", "MU", "TXN", "ASML", "TSM",
    # AI / cloud / cyber
    "PLTR", "SNOW", "CRWD", "PANW", "NET", "DDOG", "MDB", "SMCI", "ARM", "MRVL",
    # Megacap non-tech
    "BRK-B", "JPM", "BAC", "V", "MA", "WMT", "COST", "PG", "JNJ", "UNH", "HD",
    "XOM", "CVX", "GE", "BA", "CAT", "DE", "LMT",
    # Consumer / momentum
    "NFLX", "DIS", "MCD", "SBUX", "NKE", "ABNB", "UBER", "SHOP", 
    # EV / energy
    "RIVN", "LCID", "ENPH", "FSLR", "OKLO", "CEG", "VST",
    # ETF benchmark
    "SPY", "QQQ", "IWM", "DIA",
]


def _watchlist() -> List[str]:
    custom = ds._secret("US_WATCHLIST", "").strip()
    if custom:
        wl = [s.strip().upper() for s in re.split(r"[,\s]+", custom) if s.strip()]
        return list(dict.fromkeys(wl))
    return DEFAULT_UNIVERSE


# ---------------------------------------------------------------------------
# 個股技術分數
# ---------------------------------------------------------------------------
def _ma_breakout_score(df: pd.DataFrame) -> Dict:
    """回傳: ma20_break / ma50_break / ma200_break / volume_ratio / score."""
    if df.empty or len(df) < 60:
        return {}
    close = df["Close"].astype(float)
    vol = df["Volume"].astype(float)
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    ma200 = close.rolling(200).mean() if len(df) >= 200 else None

    res = {
        "last": float(close.iloc[-1]),
        "ma20": float(ma20.iloc[-1]) if not math.isnan(ma20.iloc[-1]) else None,
        "ma50": float(ma50.iloc[-1]) if not math.isnan(ma50.iloc[-1]) else None,
        "ma200": float(ma200.iloc[-1]) if ma200 is not None and not math.isnan(ma200.iloc[-1]) else None,
        "ma20_break": bool(close.iloc[-1] > ma20.iloc[-1] and close.iloc[-2] <= ma20.iloc[-2]),
        "ma50_break": bool(close.iloc[-1] > ma50.iloc[-1] and close.iloc[-2] <= ma50.iloc[-2]),
    }
    avg5_vol = vol.iloc[-6:-1].mean()
    res["vol_ratio"] = float(vol.iloc[-1] / avg5_vol) if avg5_vol > 0 else None
    return res


def _momentum_metrics(df: pd.DataFrame, spy_df: pd.DataFrame) -> Dict:
    """漲幅、相對 SPY 強度。"""
    if df.empty or len(df) < 22:
        return {}
    close = df["Close"].astype(float)
    daily_pct = (close.iloc[-1] / close.iloc[-2] - 1) * 100
    five_pct = (close.iloc[-1] / close.iloc[-6] - 1) * 100 if len(close) >= 6 else None
    twenty_pct = (close.iloc[-1] / close.iloc[-21] - 1) * 100 if len(close) >= 21 else None

    rs = None
    if not spy_df.empty and len(spy_df) >= 22:
        spy_close = spy_df["Close"].astype(float)
        spy_20 = (spy_close.iloc[-1] / spy_close.iloc[-21] - 1) * 100
        # Bug fix: `spy_20 != 0` 是多餘且錯誤的防呆 — 下面是減法不是除法, 沒有除
        # 以 spy_20 的風險。若大盤 20 日漲跌幅剛好是 0% (盤整), 這個條件會讓 RS
        # 整個被丟掉變成 None, 而不是正確算出 twenty_pct - 0。拿掉這個多餘檢查。
        if twenty_pct is not None:
            rs = round(twenty_pct - spy_20, 2)

    return {
        "daily_pct": round(float(daily_pct), 2),
        "five_pct": round(float(five_pct), 2) if five_pct is not None else None,
        "twenty_pct": round(float(twenty_pct), 2) if twenty_pct is not None else None,
        "rs_vs_spy_20d": rs,
    }


# ---------------------------------------------------------------------------
# 新聞題材熱度
# ---------------------------------------------------------------------------
THEME_KEYWORDS = {
    "AI": ["AI", "artificial intelligence", "GPT", "LLM", "chatbot", "generative"],
    "Chips/Semi": ["chip", "semiconductor", "GPU", "wafer", "fab"],
    "Cloud": ["cloud", "AWS", "Azure", "data center"],
    "Cybersecurity": ["cyber", "security", "ransomware"],
    "EV/Battery": ["EV", "electric vehicle", "battery", "Tesla"],
    "Energy": ["oil", "OPEC", "natural gas", "LNG"],
        "Fed/Rates": ["Fed", "rate cut", "FOMC", "inflation", "CPI"],
    "Earnings": ["earnings", "guidance", "beats", "misses"],
}


def _theme_score_for(symbol: str, news_pool: List[Dict]) -> Dict:
    """根據新聞抓題材熱度。"""
    sym_news = [n for n in news_pool if symbol.upper() in (n.get("relatedTickers") or [])]
    sym_news.extend(ds.fetch_yahoo_news(symbol, max_n=4))
    titles = " ".join((n.get("title") or "") for n in sym_news).lower()
    themes = []
    for theme, kws in THEME_KEYWORDS.items():
        if any(k.lower() in titles for k in kws):
            themes.append(theme)
    return {"news_count": len(sym_news), "themes": themes, "news": sym_news[:3]}


# ---------------------------------------------------------------------------
# B13 修正: 完整的 ETF / ADR / 高相關股過濾
# ---------------------------------------------------------------------------
# 排除清單 — 純 ETF, 不該出現在「個股推薦」
_US_ETF_BLACKLIST = {
    # 大盤 ETF
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VTV", "VUG", "VEA", "VWO",
    # 板塊 ETF
    "XLK", "XLE", "XLF", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU", "XLRE", "XLC",
    # 三倍槓桿 ETF
    "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA", "UPRO", "SPXU", "FAS", "FAZ",
    # 主題 ETF
    "ARKK", "ARKW", "ARKG", "ARKF", "ARKQ", "SMH", "XSD", "IBB", "XBI",
    # 商品 / 債券 ETF
    "GLD", "SLV", "USO", "UNG", "TLT", "HYG", "LQD",
}

# 高度相關股清單 — 同一族群只取 1 檔避免 Top 5 重複曝險
# 用「代表性最強」的標的當 anchor, 其他列為其同類
_US_CORRELATED_GROUPS = [
    {"NVDA", "TSM", "ASML", "AVGO"},          # AI 半導體 (高度同向)
    {"MSFT", "GOOGL", "META", "AAPL"},        # mega-cap tech (相關係數 > 0.8)
    { "MARA", "RIOT"},          # 加密貨幣概念
    {"OKLO", "SMR", "CEG", "VST", "NEE"},     # 核電 / 公用事業
    {"PLTR", "AI", "BBAI", "SOUN"},           # AI 軟體
    {"IONQ", "RGTI", "QBTS"},                  # 量子運算
    {"AMD", "INTC", "MU"},                     # CPU/Memory
]


def _dedup_correlated(scored_rows: List[Dict], score_key: str = "score",
                       min_kept: int = 5) -> List[Dict]:
    """同一相關性 group 只保留分數最高的那檔, 避免推薦過度集中.

    B13 + M3 修正: 若 dedup 後不足 min_kept 檔, 把被砍掉的「同 group 次高分」
    依序補回, 直到湊滿 min_kept 或用完候選為止.
    防止「Top 10 都在 mega-cap tech group → dedup 後只剩 1 檔」的問題.
    """
    sorted_rows = sorted(scored_rows, key=lambda r: r.get(score_key, 0), reverse=True)
    kept = []
    deferred = []  # 被 dedup 砍掉的, 留作備援
    used_groups = []
    for row in sorted_rows:
        sym = row.get("symbol", "")
        my_group = None
        for g in _US_CORRELATED_GROUPS:
            if sym in g:
                my_group = g
                break
        if my_group is not None and my_group in used_groups:
            deferred.append(row)  # 先記下, 不足時補回
            continue
        kept.append(row)
        if my_group is not None:
            used_groups.append(my_group)

    # M3 fallback: 不足 min_kept 時補回 deferred (仍按分數)
    if len(kept) < min_kept and deferred:
        need = min_kept - len(kept)
        kept.extend(deferred[:need])
        # 重新排序確保高分在前
        kept.sort(key=lambda r: r.get(score_key, 0), reverse=True)
    return kept


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _score_row(sym: str, tech: Dict, mom: Dict, theme: Dict, df) -> Dict:
    """單檔評分 (技術突破 + 動能 + RS + 題材 + 過熱剎車). 核心 Top10 與新興突破共用.

    過熱剎車: 動能策略最怕追在噴出末端. 無基本面估值, 改用「技術過熱」扣分:
      ① 乖離 MA20 太遠 (追高) ② 20 日已大漲 (末端) ③ RSI 超買. 並標警示讓使用者看到原因。
    """
    score = 0.0
    reasons: List[str] = []
    if tech.get("ma20_break"):
        score += 1.5; reasons.append("突破 MA20")
    if tech.get("ma50_break"):
        score += 1.5; reasons.append("突破 MA50")
    if tech.get("vol_ratio") and tech["vol_ratio"] >= 1.5:
        score += 1.0; reasons.append(f"量比 {tech['vol_ratio']:.1f}x")
    if mom.get("daily_pct") and mom["daily_pct"] > 1:
        score += 0.5; reasons.append(f"當日 +{mom['daily_pct']:.1f}%")
    if mom.get("rs_vs_spy_20d") and mom["rs_vs_spy_20d"] > 0:
        score += min(2.0, mom["rs_vs_spy_20d"] / 5.0)
        reasons.append(f"RS+{mom['rs_vs_spy_20d']:.1f}")
    if theme["themes"]:
        score += 1.0 * len(theme["themes"])
        reasons.append(f"題材: {', '.join(theme['themes'])}")
    if theme["news_count"] >= 3:
        score += 1.5; reasons.append(f"新聞熱度高 ({theme['news_count']} 則)")
    elif theme["news_count"] >= 1:
        score += 0.5

    # 過熱剎車
    overheat: List[str] = []
    rsi_v = None
    if tech.get("ma20") and tech.get("last") and tech["ma20"] > 0:
        ext = (tech["last"] / tech["ma20"] - 1) * 100
        if ext > 20:
            score -= 2.0; overheat.append(f"乖離MA20 +{ext:.0f}%")
        elif ext > 12:
            score -= 1.0; overheat.append(f"乖離MA20 +{ext:.0f}%")
    if mom.get("twenty_pct") is not None and mom["twenty_pct"] > 40:
        score -= 1.5; overheat.append(f"20日已 +{mom['twenty_pct']:.0f}%")
    elif mom.get("twenty_pct") is not None and mom["twenty_pct"] > 25:
        score -= 0.5
    try:
        import indicators as _ind
        _rsi_s = _ind.rsi(df["Close"].astype(float))
        if _rsi_s is not None and len(_rsi_s):
            rsi_v = float(_rsi_s.iloc[-1])
            if rsi_v >= 80:
                score -= 1.5; overheat.append(f"RSI {rsi_v:.0f} 超買")
            elif rsi_v >= 72:
                score -= 0.5
    except Exception:
        pass
    if overheat:
        reasons.append("⚠️過熱: " + " / ".join(overheat))

    return {
        "symbol": sym,
        "last": tech.get("last"),
        "daily_%": mom.get("daily_pct"),
        "5d_%": mom.get("five_pct"),
        "20d_%": mom.get("twenty_pct"),
        "RS_20d": mom.get("rs_vs_spy_20d"),
        "MA20突破": "Y" if tech.get("ma20_break") else "",
        "MA50突破": "Y" if tech.get("ma50_break") else "",
        "量比": tech.get("vol_ratio"),
        "RSI": round(rsi_v, 1) if rsi_v is not None else None,
        "過熱警示": " / ".join(overheat) if overheat else "",
        "題材": ", ".join(theme["themes"]) if theme["themes"] else "",
        "近期新聞": theme["news"],
        "進場理由": " · ".join(reasons),
        "score": round(float(score), 2),
    }


def _expert_signal(symbol: str) -> tuple:
    """用現成 Finnhub 資料算「專家共識」加分: 內部人 Form 4 買進 + 分析師評等. 回 (bonus, label).

    不接 IB 跟單 (IB 無公開 API); 改用「內部人自己掏錢買 + 分析師偏多/上調」當專家背書。
    """
    bonus = 0.0
    tags: List[str] = []
    try:
        import analyst_insider_alert as _ai
        ins = _ai._fetch_insider_transactions(symbol, days_back=45) or []
        if ins:
            ceo = any(x.get("is_ceo_cfo") for x in ins)
            bonus += 2.0 if ceo else 1.2
            tags.append("CEO/CFO 買進" if ceo else f"內部人買進×{len(ins)}")
        rec = _ai._fetch_analyst_recommendations(symbol) or {}
        if rec:
            br = rec.get("buy_ratio_cur", 0) or 0
            if br >= 75:
                bonus += 1.0; tags.append(f"分析師偏多 {br:.0f}%")
            elif br >= 60:
                bonus += 0.5
            if (rec.get("buy_ratio_change_pp", 0) or 0) >= 5:
                bonus += 0.5; tags.append("分析師上調")
    except Exception:
        pass
    return round(bonus, 2), " · ".join(tags)


def _accumulation_signal(symbol: str, df=None) -> tuple:
    """量價吸籌訊號 — 台股「主力潛伏」的美股版, 純用 OHLCV, 零額外 API。
    抓「上漲日量能 > 下跌日量能 (主力買盤)」+「OBV 上升 (持續吸貨)」。回 (bonus, tag)。"""
    bonus = 0.0
    tags: List[str] = []
    try:
        if df is None or getattr(df, "empty", True):
            df = ds.fetch_yf_history(symbol, period="3mo", interval="1d")
        if df is None or df.empty or len(df) < 25:
            return 0.0, ""
        last = df.tail(20).copy()
        c = last["Close"].astype(float)
        v = last["Volume"].astype(float)
        chg = c.diff()
        up_vol = float(v[chg > 0].sum())
        dn_vol = float(v[chg < 0].sum())
        if dn_vol > 0:
            mfr = up_vol / dn_vol  # money-flow ratio: 上漲量能 / 下跌量能
            if mfr >= 2.0:
                bonus += 1.0; tags.append(f"買盤量能 {mfr:.1f}x")
            elif mfr >= 1.4:
                bonus += 0.5; tags.append(f"買盤量能 {mfr:.1f}x")
        # OBV 斜率 (近 20 日上升 → 量能持續流入)
        sign = chg.apply(lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0))
        obv = (sign * v).cumsum()
        if len(obv) >= 2 and obv.iloc[-1] > obv.iloc[0] and obv.iloc[-1] > float(obv.median()):
            bonus += 0.5
            tags.append("OBV↑" if tags else "OBV 上升")
    except Exception:
        return 0.0, ""
    return round(bonus, 2), " · ".join(tags)


def _options_flow_signal(symbol: str) -> tuple:
    """選擇權 flow: 近月 call/put 成交量比。call-heavy (P/C 低) → 大戶偏多布局。回 (bonus, tag)。
    用 yfinance 免費選擇權鏈; 部分標的/ETF 無選擇權 → graceful 回 0。"""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        exps = list(getattr(t, "options", []) or [])
        if not exps:
            return 0.0, ""
        ch = t.option_chain(exps[0])  # 近月
        call_vol = float(ch.calls["volume"].fillna(0).sum()) if "volume" in ch.calls else 0.0
        put_vol = float(ch.puts["volume"].fillna(0).sum()) if "volume" in ch.puts else 0.0
        if call_vol < 500:  # 量太小不具代表性
            return 0.0, ""
        pcr = (put_vol / call_vol) if put_vol > 0 else 0.0
        if pcr <= 0.6:
            return 1.0, f"選擇權偏多 P/C {pcr:.2f}"
        if pcr <= 0.8:
            return 0.5, f"選擇權偏多 P/C {pcr:.2f}"
    except Exception:
        return 0.0, ""
    return 0.0, ""


def _institutional_signal(symbol: str) -> tuple:
    """機構持股變化 (13F)。回 (bonus, tag)。
    注意: Finnhub institutional-ownership 為『付費端點』, 免費 tier 多半回 403/空 → graceful 0,
    僅在真的拿到資料時才加分 (淨增持 → 偏多)。升級付費後自動生效。"""
    try:
        import requests
        token = ds.get_finnhub_token()
        if not token:
            return 0.0, ""
        r = requests.get("https://finnhub.io/api/v1/stock/ownership",
                          params={"symbol": symbol, "limit": 20, "token": token}, timeout=10)
        if r.status_code != 200:
            return 0.0, ""
        data = (r.json() or {}).get("ownership", []) or []
        if not data:
            return 0.0, ""
        net = sum((x.get("change", 0) or 0) for x in data)  # 正 = 機構淨增持
        if net > 0:
            return 1.0, "機構增持"
    except Exception:
        return 0.0, ""
    return 0.0, ""


def _apply_expert_bonus(df_all, n_enrich: int = 20):
    """對技術評分前段的候選, 加「專家共識」分後重排 (只 enrich 前 n_enrich 檔, 省 Finnhub 速率).

    效果: 技術強的標的若同時有內部人買進 / 分析師偏多, 排名往前; 純技術沒專家背書的維持原位。
    """
    if df_all is None or getattr(df_all, "empty", True):
        return df_all
    df = df_all.copy()
    df["專家"] = ""
    # 大戶共識 = 內部人 Form4 + 分析師 + 量價吸籌 + 選擇權 flow + 機構增持(13F)
    _signals = (_expert_signal, _accumulation_signal,
                _options_flow_signal, _institutional_signal)
    # 昂貴(每檔額外 network round-trip)的訊號只跑排名最前的幾檔, 控制總執行時間,
    # 避免 15-20 檔 × 選擇權鏈/13F 查詢把 job 拖到 timeout。便宜的(內部人/分析師/吸籌)全跑。
    _HEAVY = {_options_flow_signal, _institutional_signal}
    _n_heavy = min(8, n_enrich)
    for pos, idx in enumerate(df.head(min(len(df), n_enrich)).index):
        try:
            sym = str(df.at[idx, "symbol"])
            total = 0.0
            parts: List[str] = []
            for fn in _signals:
                if fn in _HEAVY and pos >= _n_heavy:
                    continue  # 排名較後者省去昂貴查詢
                try:
                    b, lab = fn(sym)
                except Exception:
                    b, lab = 0.0, ""
                if b:
                    total += b
                    if lab:
                        parts.append(lab)
            total = min(total, 5.0)  # 大戶加分上限, 避免蓋過技術/動能本體
            if total:
                label = " · ".join(parts)
                df.at[idx, "score"] = round(float(df.at[idx, "score"]) + total, 2)
                df.at[idx, "專家"] = label
                if label:
                    df.at[idx, "進場理由"] = (str(df.at[idx, "進場理由"]) + " · 👑" + label).strip(" ·")
        except Exception:
            continue
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def run_us_recommendation(top_n: int = 5, dedup_correlated: bool = True) -> dict:
    """B13: dedup_correlated=True 時, 同一相關性族群只取分數最高的那檔."""
    syms = _watchlist()
    spy_df = ds.fetch_yf_history("SPY", period="3mo")
    fg = ds.fetch_fear_greed()
    sector = ds.fetch_sector_rotation()
    news_pool = ds.fetch_market_news_themes()

    rows = []
    for sym in syms:
        # B13: 用 ETF blacklist 取代寫死的 4 檔
        if sym in _US_ETF_BLACKLIST:
            continue
        df = ds.fetch_yf_history(sym, period="6mo")
        if df.empty or len(df) < 30:
            continue
        tech = _ma_breakout_score(df)
        mom = _momentum_metrics(df, spy_df)
        theme = _theme_score_for(sym, news_pool)

        rows.append(_score_row(sym, tech, mom, theme, df))

    if not rows:
        return {"top_picks": pd.DataFrame(), "fear_greed": fg, "sectors": sector, "news": news_pool}

    # B13 + M3: 同族群去重 (避免 Top 5 都是 AI 半導體), 但不足時自動補回
    if dedup_correlated:
        rows = _dedup_correlated(rows, score_key="score", min_kept=top_n)

    df_all = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    df_all = _apply_expert_bonus(df_all, n_enrich=max(top_n * 2, 15))  # 👑 專家共識加分後重排
    top_df = df_all.head(top_n).copy()

    # 補催化劑（美股 Top 5）
    try:
        import stock_catalyst
        records = []
        for _, r in top_df.iterrows():
            records.append({
                "stock_id": r.get("symbol", ""),
                "stock_name": r.get("symbol", ""),
                "今日%": r.get("daily_%"),
            })
        cat_map = stock_catalyst.annotate_picks_with_catalysts(records, market="US")
        top_df["催化劑"] = top_df["symbol"].astype(str).map(cat_map).fillna("")
    except Exception:
        pass

    # C: 對 top_picks 加 quick entry 評估 (入場標籤)
    try:
        import entry_label_helper as _el
        syms = top_df["symbol"].astype(str).tolist()
        pairs = [(s, "US") for s in syms]
        eval_map = _el.batch_evaluate(pairs, max_workers=8)
        top_df["入場標籤"] = top_df["symbol"].astype(str).map(
            lambda s: ((eval_map.get(s) or {}).get("entry_emoji", "") + " " +
                       (eval_map.get(s) or {}).get("entry_label", "—")).strip()
        )
        top_df["入場分數"] = top_df["symbol"].astype(str).map(
            lambda s: (eval_map.get(s) or {}).get("entry_score")
        )
    except Exception as _e:
        print(f"[us_screener] entry_label 計算失敗 (non-fatal): {_e}", flush=True)

    return {
        "top_picks": top_df,
        "all_scored": df_all,
        "fear_greed": fg,
        "sectors": sector,
        "news": news_pool,
    }


# ===========================================================================
# 新興突破掃描 (池外) — 獨立於核心 Top10
# ===========================================================================
# 設計: 核心 Top10 (run_us_recommendation) 維持精選池不動; 這裡用「更廣的池 + 嚴格流動性
#       過濾」掃出池外正在發動的標的, 沿用同一套 _score_row 評分 (含過熱剎車)。
#   候選池 = 內建擴大成分股清單 (S&P500/Nasdaq100 流動性高的) ∪ DEFAULT_UNIVERSE
#            ∪ 使用者自訂 US_EMERGING_UNIVERSE secret ∪ (可選) Finnhub 活躍榜。
#   「活躍」: Finnhub 免費版沒有乾淨的 most-active 端點, 故活躍度由「掃這個廣池後用
#            量比/動能排序」浮出; 要抓清單外小型股需付費資料源, 已留 _fetch_active_extra 擴充點。

EXTENDED_UNIVERSE = [
    # 更多半導體 / 設備
    "LRCX", "KLAC", "AMAT", "ADI", "NXPI", "MCHP", "ON", "MPWR", "SWKS", "TER", "ENTG", "WOLF",
    # 軟體 / 雲 / 資安
    "NOW", "INTU", "WDAY", "TEAM", "ZS", "S", "OKTA", "FTNT", "HUBS", "TWLO", "DOCU", "U", "PATH", "GTLB", "AI",
    # 網路 / 消費網路
    "PYPL", "SQ", "COIN", "HOOD", "ROKU", "PINS", "SNAP", "DASH", "RBLX", "SPOT", "MELI", "SE", "BABA", "PDD", "JD",
    # 生技 / 製藥
    "LLY", "MRNA", "REGN", "VRTX", "GILD", "BIIB", "AMGN", "ISRG", "DXCM", "ELV",
    # 工業 / 國防 / 能源
    "RTX", "NOC", "GD", "ETN", "PH", "EMR", "PWR", "SLB", "HAL", "OXY", "DVN", "MPC", "PSX", "LNG", "SMR",
    # 金融
    "GS", "MS", "WFC", "C", "SCHW", "BLK", "AXP", "COF", "PGR", "KKR", "APO",
    # 消費 / 其他動能
    "LULU", "DECK", "CMG", "ELF", "CAVA", "DKNG", "CELH", "WING", "ANF", "RDDT", "ASTS", "RKLB", "IONQ", "RGTI", "TEM",
]


def _fetch_active_extra() -> List[str]:
    """擴充點: 之後可接 Finnhub/付費的「當日活躍/漲幅榜」抓清單外小型股. 目前回 []."""
    return []


def _emerging_universe() -> List[str]:
    """組合廣池: 擴大成分股 + 預設精選 + 使用者自訂 + (可選) 活躍榜, 去重."""
    uni = list(EXTENDED_UNIVERSE) + list(DEFAULT_UNIVERSE)
    try:
        custom = ds._secret("US_EMERGING_UNIVERSE", "").strip()
        if custom:
            uni += [s.strip().upper() for s in re.split(r"[,\s]+", custom) if s.strip()]
    except Exception:
        pass
    try:
        uni += _fetch_active_extra()
    except Exception:
        pass
    # 去重 + 去掉 ETF
    seen, out = set(), []
    for s in uni:
        s = s.upper()
        if s and s not in seen and s not in _US_ETF_BLACKLIST:
            seen.add(s); out.append(s)
    return out


def run_emerging_breakout(top_n: int = 10, min_price: float = 3.0,
                          min_avg_vol: float = 500_000) -> dict:
    """池外新興突破掃描. 流動性過濾 (股價 ≥ min_price, 5日均量 ≥ min_avg_vol) 濾掉雞蛋水餃,
    沿用 _score_row 評分 (含過熱剎車), 標出「池外」(不在精選 DEFAULT_UNIVERSE 內) 的新標的。
    """
    spy_df = ds.fetch_yf_history("SPY", period="3mo")
    news_pool = ds.fetch_market_news_themes()
    core_set = set(DEFAULT_UNIVERSE)
    rows = []
    for sym in _emerging_universe():
        df = ds.fetch_yf_history(sym, period="6mo")
        if df is None or df.empty or len(df) < 60:
            continue
        # 流動性過濾 — 濾掉低價 / 低量 (雞蛋水餃 / 拉高出貨高風險)
        try:
            close = df["Close"].astype(float); vol = df["Volume"].astype(float)
            if float(close.iloc[-1]) < min_price:
                continue
            if float(vol.iloc[-6:-1].mean()) < min_avg_vol:
                continue
        except Exception:
            continue
        tech = _ma_breakout_score(df)
        mom = _momentum_metrics(df, spy_df)
        theme = _theme_score_for(sym, news_pool)
        if not tech or not mom:
            continue
        row = _score_row(sym, tech, mom, theme, df)
        row["池外"] = "🆕" if sym not in core_set else ""   # 標出非精選池的新標的
        rows.append(row)

    if not rows:
        return {"top_picks": pd.DataFrame(), "scanned": 0}
    rows = _dedup_correlated(rows, score_key="score", min_kept=top_n)
    df_all = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    df_all = _apply_expert_bonus(df_all, n_enrich=max(top_n * 2, 15))  # 👑 專家共識加分後重排
    return {
        "top_picks": df_all.head(top_n).copy(),
        "all_scored": df_all,
        "scanned": len(rows),
        "universe_size": len(_emerging_universe()),
    }
