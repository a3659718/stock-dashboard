"""
emerging_themes.py
偵測「正在萌芽 (但還沒被排行榜抓到)」的族群 — 真正能賺的訊號.

核心觀察:
  當你看到 sector_pulse 的「熱門題材排行」進前 5 時, leader 通常已經漲了 5-10%,
  那是落後指標. 萌芽期反而藏在這些 leading indicator:

  1. Smart money flow (法人卡位): 同族群 N 檔股票同步出現「外資 ≥ 連 3 日買超」
     或「投信 5d 累積 > 5000 張」, 但族群的「平均%」還在 -2% ~ +3% 區間.
     法人是先動的, 散戶看到漲幅排行才進來.

  2. 個股先動 (price-volume divergence): 族群裡有 1-2 檔已經放量突破
     (今日% > 4% 且量比 > 2 且 5日% > 8%), 但族群其他成員還沒跟. 這通常是
     「資金正在從 leader 擴散到落後股」的早期訊號.

  3. RS 突破 (Relative Strength): 族群成員平均收盤 / 加權指數比值 (RS),
     由 5 日前 < 1 變成現在 > 1. 表示資金開始往這族群輪.

任一條件命中 ≥ 1 分, 三條全中就是「萌芽中」高機率訊號.

對外接口:
  find_emerging_themes(top_n=5) -> List[Dict]
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

import data_sources as ds
import sector_pulse


def _fetch_twii_close_series(days: int = 10) -> Optional[pd.Series]:
    """抓加權指數收盤 (做 RS 計算用)."""
    try:
        df = ds.fetch_yf_history("^TWII", period="1mo", interval="1d")
        if df is None or df.empty or len(df) < days:
            return None
        return df["Close"].astype(float).tail(days).reset_index(drop=True)
    except Exception:
        return None


def _theme_avg_close(stock_ids: List[str], days: int = 10) -> Optional[pd.Series]:
    """算族群成員平均收盤序列 (做 RS 計算用).

    每檔抓 daily 收盤, 對齊後取平均. 失敗或樣本 < 3 檔回 None.
    """
    series_list = []
    for sid in stock_ids:
        for suffix in [".TW", ".TWO"]:
            try:
                df = ds.fetch_yf_history(f"{sid}{suffix}", period="1mo", interval="1d")
                if df is None or df.empty or len(df) < days:
                    continue
                series_list.append(df["Close"].astype(float).tail(days).reset_index(drop=True))
                break
            except Exception:
                continue
    if len(series_list) < 3:
        return None
    df = pd.concat(series_list, axis=1)
    # 每檔股自己 normalize 到 1 (0 日收盤為基期), 再取平均 — 避免高價股主導
    df_norm = df.div(df.iloc[0]).fillna(method="ffill")
    return df_norm.mean(axis=1)


def _compute_rs_breakout(theme_ids: List[str]) -> Optional[Dict]:
    """RS 是否「剛突破 1」: 5 日前 RS < 1 但今日 RS > 1.

    Returns: {"rs_today": ..., "rs_5d_ago": ..., "breakout": bool}
    """
    twii = _fetch_twii_close_series(days=10)
    theme_avg = _theme_avg_close(theme_ids, days=10)
    if twii is None or theme_avg is None:
        return None
    if len(twii) != len(theme_avg) or len(twii) < 6:
        return None
    # 兩條 normalize 到 1, 比值 = RS
    twii_n = twii / float(twii.iloc[0])
    theme_n = theme_avg / float(theme_avg.iloc[0])
    rs = theme_n / twii_n
    rs_today = float(rs.iloc[-1])
    rs_5d_ago = float(rs.iloc[-6]) if len(rs) >= 6 else None
    if rs_5d_ago is None:
        return None
    breakout = rs_5d_ago < 1.0 and rs_today > 1.0
    return {
        "rs_today": round(rs_today, 4),
        "rs_5d_ago": round(rs_5d_ago, 4),
        "breakout": breakout,
        "rs_change_pct": round((rs_today / rs_5d_ago - 1) * 100, 2),
    }


def _check_smart_money_flow(theme_ids: List[str], min_buyers: int = 3) -> Optional[Dict]:
    """同族群 ≥ min_buyers 檔出現法人卡位 (外資連 3 日買超 / 投信 5d > 1000 張).

    Returns: {"buyer_count": N, "buyers": [...], "score": 0-3}
    """
    try:
        import chip_analyzer
    except ImportError:
        return None
    fi_buyers = []
    it_buyers = []
    both = []  # 雙法人都進場 — 最強訊號
    for sid in theme_ids[:30]:  # 取最多 30 檔 (避免 quota 爆)
        try:
            chip = chip_analyzer.fetch_chip_data(str(sid), days=10)
            if not chip:
                continue
            inst = chip.get("institutional") or {}
            fi = inst.get("Foreign_Investor") or {}
            it = inst.get("Investment_Trust") or {}
            fi_consec = fi.get("consecutive_days", 0) or 0
            fi_5d = fi.get("5d_total", 0) or 0
            it_5d = it.get("5d_total", 0) or 0
            fi_in = fi_consec >= 3 and fi_5d > 0
            it_in = it_5d >= 1000
            if fi_in:
                fi_buyers.append(sid)
            if it_in:
                it_buyers.append(sid)
            if fi_in and it_in:
                both.append(sid)
        except Exception:
            continue
    score = 0
    if len(both) >= 2:
        score += 2
    if len(fi_buyers) >= min_buyers:
        score += 1
    if len(it_buyers) >= min_buyers:
        score += 1
    if score == 0:
        return None
    return {
        "fi_buyers": fi_buyers[:6],
        "it_buyers": it_buyers[:6],
        "both": both[:5],
        "score": score,
    }


def _check_price_volume_divergence(quotes_df, theme_ids: List[str]) -> Optional[Dict]:
    """族群裡有人放量突破, 但族群整體還沒跟.

    條件:
      - 有 ≥ 1 檔 今日% > 4 且 量比 > 2 且 5日% > 8
      - 但族群平均% < 3%
    """
    if quotes_df is None or quotes_df.empty:
        return None
    sub = quotes_df[quotes_df["stock_id"].isin(theme_ids)]
    if sub.empty:
        return None
    avg_today = sub["今日%"].mean() if "今日%" in sub.columns else 0
    if pd.isna(avg_today):
        avg_today = 0
    if avg_today > 3:
        return None  # 已經漲完了, 不算萌芽
    leaders = sub[
        (sub.get("今日%", 0).fillna(0) > 4)
        & (sub.get("量比", 0).fillna(0) > 2)
        & (sub.get("5日%", 0).fillna(0) > 8)
    ].sort_values("今日%", ascending=False)
    if leaders.empty:
        return None
    return {
        "avg_today": round(float(avg_today), 2),
        "leaders_count": len(leaders),
        "leaders": leaders.head(3).to_dict("records"),
    }


def find_emerging_themes(top_n: int = 5,
                          smart_money_min_buyers: int = 3) -> List[Dict]:
    """掃所有 TW_THEMES 找萌芽中的族群.

    回傳: [{theme, score, stage, reasons[], leading_stocks[],
           smart_money: {...}, divergence: {...}, rs: {...}}, ...]
    score 排序高→低. 至少要命中 1 個 leading indicator 才入選.
    """
    # 1. 先抓所有 theme 成員的最新 quote (一次)
    try:
        info = ds.get_taiwan_stock_info()
        market_map = info.set_index("stock_id")["type"].to_dict()
        all_ids = sorted({sid for ids in sector_pulse.TW_THEMES.values() for sid in ids if sid in market_map})
        quotes = sector_pulse.fetch_intraday_metrics(all_ids, market_map)
    except Exception as e:
        print(f"[emerging_themes] quotes failed: {e}", flush=True)
        return []
    if quotes is None or quotes.empty:
        return []

    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}

    candidates: List[Dict] = []
    for theme, sids in sector_pulse.TW_THEMES.items():
        # 限制只看市場上有資料的 sid
        valid_sids = [s for s in sids if s in market_map]
        if len(valid_sids) < 4:
            continue

        rs = _compute_rs_breakout(valid_sids)
        smart = _check_smart_money_flow(valid_sids, min_buyers=smart_money_min_buyers)
        divergence = _check_price_volume_divergence(quotes, valid_sids)

        # 任一個 leading indicator 命中才算候選
        if not (rs or smart or divergence):
            continue

        score = 0.0
        reasons: List[str] = []

        if rs and rs.get("breakout"):
            score += 3
            reasons.append(
                f"RS 突破 ({rs['rs_5d_ago']:.3f} → {rs['rs_today']:.3f}, "
                f"{rs['rs_change_pct']:+.2f}%)"
            )
        elif rs and rs.get("rs_change_pct", 0) > 1.5:
            score += 1
            reasons.append(f"RS 走高 {rs['rs_change_pct']:+.2f}%")

        if smart:
            score += smart["score"]
            if smart.get("both"):
                reasons.append(f"外資+投信雙進場 {len(smart['both'])} 檔: {', '.join(smart['both'][:3])}")
            elif smart.get("fi_buyers"):
                reasons.append(f"外資連 3 日進場 {len(smart['fi_buyers'])} 檔: {', '.join(smart['fi_buyers'][:3])}")
            if smart.get("it_buyers") and not smart.get("both"):
                reasons.append(f"投信卡位 {len(smart['it_buyers'])} 檔")

        if divergence:
            score += 2
            ld = divergence["leaders"]
            ld_str = ", ".join(
                f"{r.get('stock_id')} ({r.get('今日%')}%)" for r in ld[:2]
            )
            reasons.append(
                f"個股先動 (族群均 {divergence['avg_today']}% 但 {divergence['leaders_count']} 檔噴: {ld_str})"
            )

        # 已經很熱的不算萌芽 — 用 quote 平均過濾
        sub = quotes[quotes["stock_id"].isin(valid_sids)]
        avg_5d = float(sub["5日%"].mean()) if "5日%" in sub.columns and not sub.empty else 0
        if pd.isna(avg_5d):
            avg_5d = 0
        if avg_5d > 8:
            # 5d 平均已 > 8% → 算「成熟期」, 萌芽分數打折
            score *= 0.4

        # stage 判斷
        if score >= 5:
            stage = "萌芽 (高訊號)"
        elif score >= 3:
            stage = "萌芽中"
        elif score >= 1.5:
            stage = "醞釀"
        else:
            continue  # 訊號太弱 skip

        # leader stocks (放量先動的)
        leading_stocks = []
        if divergence:
            for r in divergence.get("leaders", [])[:3]:
                sid = str(r.get("stock_id", ""))
                leading_stocks.append({
                    "stock_id": sid,
                    "name": name_map.get(sid, ""),
                    "today_pct": r.get("今日%"),
                    "5d_pct": r.get("5日%"),
                    "vol_ratio": r.get("量比"),
                })

        candidates.append({
            "theme": theme,
            "score": round(score, 2),
            "stage": stage,
            "reasons": reasons,
            "leading_stocks": leading_stocks,
            "smart_money": smart,
            "divergence": divergence,
            "rs": rs,
            "avg_today": round(float(sub["今日%"].mean()) if "今日%" in sub.columns and not sub.empty else 0, 2),
            "avg_5d": round(avg_5d, 2),
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


# ===========================================================================
# 美股版: 11 個 sector ETF, RS vs SPY 突破 + 成員放量
# ===========================================================================
US_SECTOR_ETFS = {
    "XLK": ("Technology", ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL"]),
    "XLF": ("Financials", ["JPM", "BRK-B", "V", "MA", "BAC"]),
    "XLV": ("Health Care", ["LLY", "UNH", "JNJ", "ABBV", "MRK"]),
    "XLY": ("Consumer Discretionary", ["AMZN", "TSLA", "HD", "MCD", "BKNG"]),
    "XLP": ("Consumer Staples", ["PG", "KO", "WMT", "COST", "PEP"]),
    "XLE": ("Energy", ["XOM", "CVX", "COP", "EOG", "SLB"]),
    "XLI": ("Industrials", ["GE", "RTX", "CAT", "HON", "UNP"]),
    "XLB": ("Materials", ["LIN", "SHW", "APD", "ECL", "FCX"]),
    "XLU": ("Utilities", ["NEE", "SO", "DUK", "CEG", "AEP"]),
    "XLRE": ("Real Estate", ["PLD", "AMT", "EQIX", "WELL", "DLR"]),
    "XLC": ("Communication", ["META", "GOOGL", "GOOG", "NFLX", "DIS"]),
}


def _us_etf_rs_breakout(etf: str) -> Optional[Dict]:
    """sector ETF vs SPY 的 RS 突破: 5 日前 < 1 但今日 > 1."""
    try:
        etf_df = ds.fetch_yf_history(etf, period="1mo", interval="1d")
        spy_df = ds.fetch_yf_history("SPY", period="1mo", interval="1d")
        if etf_df is None or spy_df is None or etf_df.empty or spy_df.empty:
            return None
        if len(etf_df) < 6 or len(spy_df) < 6:
            return None
        etf_close = etf_df["Close"].astype(float).tail(10).reset_index(drop=True)
        spy_close = spy_df["Close"].astype(float).tail(10).reset_index(drop=True)
        if len(etf_close) != len(spy_close):
            return None
        etf_n = etf_close / float(etf_close.iloc[0])
        spy_n = spy_close / float(spy_close.iloc[0])
        rs = etf_n / spy_n
        rs_today = float(rs.iloc[-1])
        rs_5d_ago = float(rs.iloc[-6])
        breakout = rs_5d_ago < 1.0 and rs_today > 1.0
        return {
            "rs_today": round(rs_today, 4),
            "rs_5d_ago": round(rs_5d_ago, 4),
            "breakout": breakout,
            "rs_change_pct": round((rs_today / rs_5d_ago - 1) * 100, 2),
        }
    except Exception:
        return None


def _us_member_divergence(members: List[str]) -> Optional[Dict]:
    """sector ETF 主要成員: 有 >= 1 檔放量突破但 sector 平均還沒大漲."""
    try:
        rows = []
        for sym in members:
            df = ds.fetch_yf_history(sym, period="2mo", interval="1d")
            if df is None or df.empty or len(df) < 6:
                continue
            close = df["Close"].astype(float)
            vol = df["Volume"].astype(float)
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            chg = (last / prev - 1) * 100 if prev else 0
            r5 = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0
            avg5_vol = vol.iloc[-6:-1].mean() if len(vol) >= 6 else 0
            ratio = float(vol.iloc[-1] / avg5_vol) if avg5_vol > 0 else 0
            rows.append({
                "symbol": sym, "today_pct": round(chg, 2),
                "5d_pct": round(r5, 2), "vol_ratio": round(ratio, 2),
                "current": round(last, 2),
            })
        if len(rows) < 3:
            return None
        avg_today = sum(r["today_pct"] for r in rows) / len(rows)
        if avg_today > 2.5:
            return None
        leaders = [r for r in rows
                   if r["today_pct"] > 2.5 and r["vol_ratio"] > 1.8 and r["5d_pct"] > 5]
        if not leaders:
            return None
        leaders.sort(key=lambda x: x["today_pct"], reverse=True)
        return {
            "avg_today": round(avg_today, 2),
            "leaders_count": len(leaders),
            "leaders": leaders[:3],
        }
    except Exception:
        return None


def find_us_emerging_sectors(top_n: int = 5) -> List[Dict]:
    """掃 11 個 sector ETF, 找 RS 突破 + 成員放量背離 的萌芽 sector."""
    candidates: List[Dict] = []
    for etf, (label, members) in US_SECTOR_ETFS.items():
        rs = _us_etf_rs_breakout(etf)
        div = _us_member_divergence(members)
        if not (rs or div):
            continue
        score = 0.0
        reasons: List[str] = []
        if rs and rs.get("breakout"):
            score += 3
            reasons.append(
                f"RS vs SPY 突破 ({rs['rs_5d_ago']:.3f} -> {rs['rs_today']:.3f}, "
                f"{rs['rs_change_pct']:+.2f}%)"
            )
        elif rs and rs.get("rs_change_pct", 0) > 1.5:
            score += 1
            reasons.append(f"RS 走高 {rs['rs_change_pct']:+.2f}%")
        if div:
            score += 2
            ld_str = ", ".join(
                f"{r['symbol']} ({r['today_pct']}%)" for r in div["leaders"][:2]
            )
            reasons.append(
                f"成員先動 (sector 均 {div['avg_today']}% 但 {div['leaders_count']} 檔噴: {ld_str})"
            )
        if score < 1.5:
            continue
        if score >= 4:
            stage = "萌芽 (高訊號)"
        elif score >= 2.5:
            stage = "萌芽中"
        else:
            stage = "醞釀"
        leading_stocks = []
        if div:
            for r in div.get("leaders", [])[:3]:
                leading_stocks.append({
                    "stock_id": r["symbol"], "name": "",
                    "today_pct": r["today_pct"], "5d_pct": r["5d_pct"],
                    "vol_ratio": r["vol_ratio"],
                })
        candidates.append({
            "theme": f"{etf} {label}",
            "score": round(score, 2), "stage": stage, "reasons": reasons,
            "leading_stocks": leading_stocks, "rs": rs, "divergence": div,
            "smart_money": None,
            "avg_today": (div or {}).get("avg_today", 0), "avg_5d": 0,
        })
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


def fmt_emerging_themes_block(emerging: List[Dict]) -> List[str]:
    """格式化萌芽族群成 TG HTML lines."""
    if not emerging:
        return []
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)
    out = ["", "<b>🌱 萌芽族群 (還沒上排行榜)</b>"]
    for e in emerging:
        theme = _esc(e.get("theme", ""))
        score = e.get("score", 0)
        stage = _esc(e.get("stage", ""))
        avg_today = e.get("avg_today", 0)
        avg_5d = e.get("avg_5d", 0)
        sign_t = "+" if avg_today and avg_today > 0 else ""
        sign_5 = "+" if avg_5d and avg_5d > 0 else ""
        out.append(
            f"<b>{theme}</b> · 分數 {score} · {stage} · "
            f"族群均 今日 {sign_t}{avg_today}% / 5d {sign_5}{avg_5d}%"
        )
        for r in e.get("reasons", [])[:3]:
            out.append(f"   ✓ {_esc(r)}")
        for s in e.get("leading_stocks", [])[:2]:
            sid = _esc(s.get("stock_id", ""))
            nm = _esc(s.get("name", ""))
            tp = _esc(s.get("today_pct", ""))
            vr = _esc(s.get("vol_ratio", ""))
            # US 版 leading_stocks 沒有 stock_name (空字串), 避免雙空白
            name_part = f" {nm}" if nm and str(nm).strip() else ""
            out.append(f"     <code>{sid}</code>{name_part}  今日 {tp}% · 量比 {vr}x")
    return out
