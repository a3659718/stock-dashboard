"""
news_picks.py
台股「消息面 + 成長動能」Top 10 推薦。

策略：
  1) 對熱門題材股池 (sector_pulse.TW_THEMES) 評估每檔的 K 線健康度
  2) 加分項：MA20 之上、KD 黃金交叉、MACD 翻紅、量比 > 1.3、5d 漲幅 0~15% (剛起漲)
  3) 扣分項：已大漲 (5d 漲幅 > 20%)、跌破 MA20、量縮
  4) 可選 boost：題材近期有正面新聞 (FinMind TaiwanStockNews)
  5) 排序取 Top 10
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List

import pandas as pd
import streamlit as st

import data_sources as ds
import sector_pulse
import tw_screener as tw


@st.cache_data(ttl=900, show_spinner=False)
def fetch_taiwan_stock_news(stock_id: str, days: int = 14) -> pd.DataFrame:
    """FinMind 個股新聞 (若 dataset 不可用則回空)."""
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        df = ds._finmind_get("TaiwanStockNews", data_id=stock_id,
                              start_date=start, end_date=end)
        return df if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _kline_score(daily: pd.DataFrame) -> tuple:
    """評 K 線健康度。回傳 (score, reasons[])."""
    if daily.empty or len(daily) < 30:
        return 0.0, ["資料不足"]
    c = daily["close"].astype(float)
    h = daily["high"].astype(float) if "high" in daily.columns else c
    l = daily["low"].astype(float) if "low" in daily.columns else c
    v = daily["Trading_Volume"].astype(float) if "Trading_Volume" in daily.columns else None

    last = float(c.iloc[-1])
    prev = float(c.iloc[-2])
    daily_pct = (last / prev - 1) * 100 if prev else 0
    five_pct = (last / float(c.iloc[-6]) - 1) * 100 if len(c) >= 6 else 0
    twenty_pct = (last / float(c.iloc[-21]) - 1) * 100 if len(c) >= 21 else 0

    ma20 = c.rolling(20).mean()
    ma60 = c.rolling(60).mean() if len(c) >= 60 else None

    score = 0.0
    reasons: List[str] = []

    # MA 結構
    if not pd.isna(ma20.iloc[-1]) and last > float(ma20.iloc[-1]):
        score += 1.5; reasons.append("站上月線")
    if ma60 is not None and not pd.isna(ma60.iloc[-1]) and last > float(ma60.iloc[-1]):
        score += 1.0; reasons.append("站上季線")

    # 起漲位置 — 漲幅尚未過大
    if 0 < five_pct < 8:
        score += 2.0; reasons.append(f"5d {five_pct:+.1f}% 起漲位")
    elif 8 <= five_pct < 15:
        score += 1.0; reasons.append(f"5d {five_pct:+.1f}% 動能中")
    elif five_pct >= 20:
        score -= 1.5; reasons.append(f"5d 漲幅過大 {five_pct:.1f}%")

    # 月漲幅未過熱
    if 0 < twenty_pct < 25:
        score += 0.5; reasons.append(f"20d {twenty_pct:+.1f}%")

    # 量能配合
    if v is not None and len(v) >= 6:
        avg5_vol = v.iloc[-6:-1].mean()
        if avg5_vol > 0:
            ratio = float(v.iloc[-1] / avg5_vol)
            if 1.3 <= ratio <= 5.0:
                score += 1.0; reasons.append(f"量比 {ratio:.1f}x")
            elif ratio > 5.0:
                score += 0.3; reasons.append(f"量爆 {ratio:.1f}x (注意)")
            elif ratio < 0.6:
                score -= 1.0; reasons.append(f"量縮 {ratio:.1f}x")

    # KD 黃金交叉 (即時)
    try:
        k, d = tw._kd_series(c, h, l)
        if not pd.isna(k.iloc[-1]) and not pd.isna(d.iloc[-1]):
            if k.iloc[-2] <= d.iloc[-2] and k.iloc[-1] > d.iloc[-1] and k.iloc[-1] < 80:
                score += 1.5; reasons.append("KD 黃交")
    except Exception:
        pass

    # MACD 翻紅
    try:
        dif, macd, hist = tw._macd_series(c)
        if not pd.isna(hist.iloc[-1]) and not pd.isna(hist.iloc[-2]):
            if hist.iloc[-2] <= 0 and hist.iloc[-1] > 0:
                score += 1.5; reasons.append("MACD 翻紅")
    except Exception:
        pass

    # 不能被空頭壓制
    if not pd.isna(ma20.iloc[-1]) and last < float(ma20.iloc[-1]) * 0.95:
        score -= 1.5; reasons.append("跌破月線 5%")

    return round(max(0.0, score), 1), reasons


def run_news_growth_picks(top_n: int = 10, themes_filter: List[str] = None) -> dict:
    """跨熱門題材池 → K 線健康度評分 → Top 10.

    Returns:
        {
          "picks": pd.DataFrame (top picks 或 empty),
          "diagnostic": str (人類可讀的執行摘要 + 失敗原因),
          "stats": {n_universe, n_daily_fetched, n_with_score, n_picks}
        }
    """
    diagnostic_lines = []
    stats = {"n_universe": 0, "n_daily_fetched": 0, "n_with_score": 0, "n_picks": 0}

    # Step 1: 抓股票清單
    info = ds.get_taiwan_stock_info()
    if info.empty:
        return {
            "picks": pd.DataFrame(),
            "diagnostic": (
                "❌ FinMind 取台股清單失敗 (空 DataFrame).\n"
                "可能原因: (1) FINMIND_TOKEN 過期 → 到 finmindtrade.com 重新生成;\n"
                "         (2) FinMind 服務暫時不可用;\n"
                "         (3) Streamlit cache 卡了舊空結果 → 點下方「強制清 cache」按鈕"
            ),
            "stats": stats,
        }
    diagnostic_lines.append(f"✅ Step 1: FinMind 取台股清單 OK ({len(info)} 檔)")

    info = ds.filter_tradeable_stocks(info)
    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}
    diagnostic_lines.append(f"   → filter_tradeable 後 {len(name_map)} 檔可交易")

    # Step 2: 配對熱門題材池
    candidates: Dict[str, str] = {}
    for theme, ids in sector_pulse.TW_THEMES.items():
        if themes_filter and theme not in themes_filter:
            continue
        for sid in ids:
            if sid in name_map and sid not in candidates:
                candidates[sid] = theme
    stats["n_universe"] = len(candidates)
    if not candidates:
        return {
            "picks": pd.DataFrame(),
            "diagnostic": (
                "\n".join(diagnostic_lines) + "\n"
                "❌ Step 2: 沒匹配到熱門題材池.\n"
                "可能原因: sector_pulse.TW_THEMES 是空的, 或所有題材股都被 filter_tradeable 過濾掉."
            ),
            "stats": stats,
        }
    diagnostic_lines.append(f"✅ Step 2: 配對熱門題材池 {len(candidates)} 檔候選")

    # Step 3: 抓日線
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=120)).strftime("%Y-%m-%d")
    daily_all = ds._fetch_universe(
        "TaiwanStockPrice", list(candidates.keys()), start, end, max_workers=5
    )
    if daily_all.empty:
        return {
            "picks": pd.DataFrame(),
            "diagnostic": (
                "\n".join(diagnostic_lines) + "\n"
                "❌ Step 3: FinMind 抓日線資料失敗 (空 DataFrame).\n"
                "可能原因: (1) FinMind quota 用完 (每小時 600 calls);\n"
                "         (2) FinMind 服務暫時不可用;\n"
                "         (3) Token 仍有效但 TaiwanStockPrice dataset 需付費 Sponsor 等級"
            ),
            "stats": stats,
        }
    stats["n_daily_fetched"] = daily_all["stock_id"].nunique() if "stock_id" in daily_all.columns else 0
    diagnostic_lines.append(
        f"✅ Step 3: 抓日線 {stats['n_daily_fetched']} 檔 ({len(daily_all)} rows)"
    )
    if "max" in daily_all.columns and "high" not in daily_all.columns:
        daily_all = daily_all.rename(columns={"max": "high", "min": "low"})

    # Step 4: K 線評分
    rows = []
    n_zero_score = 0
    for sid, g in daily_all.groupby("stock_id"):
        g = g.sort_values("date")
        score, reasons = _kline_score(g)
        if score <= 0:
            n_zero_score += 1
            continue
        last = float(g["close"].iloc[-1])
        rows.append({
            "代號": sid,
            "名稱": name_map.get(sid, ""),
            "題材": candidates.get(sid, ""),
            "現價": round(last, 2),
            "5d%": round((last / float(g["close"].iloc[-6]) - 1) * 100, 2) if len(g) >= 6 else None,
            "20d%": round((last / float(g["close"].iloc[-21]) - 1) * 100, 2) if len(g) >= 21 else None,
            "score": score,
            "理由": " · ".join(reasons),
        })
    stats["n_with_score"] = len(rows)
    diagnostic_lines.append(
        f"✅ Step 4: K 線評分 — {len(rows)} 檔正分 / {n_zero_score} 檔零分被濾掉"
    )

    if not rows:
        return {
            "picks": pd.DataFrame(),
            "diagnostic": (
                "\n".join(diagnostic_lines) + "\n"
                "⚠️ Step 4 結果: 全部候選都被 K 線健康度篩選掉了 (score ≤ 0).\n"
                "可能原因: 大盤整體弱勢 (大多數股票跌破月線 / KD 死叉等), 或評分門檻太嚴."
            ),
            "stats": stats,
        }

    picks = pd.DataFrame(rows).sort_values("score", ascending=False).head(top_n).reset_index(drop=True)
    stats["n_picks"] = len(picks)
    diagnostic_lines.append(f"✅ Step 5: 排序並取 top {len(picks)}")

    # 補催化劑
    try:
        import stock_catalyst
        records = []
        for _, r in picks.iterrows():
            records.append({
                "stock_id": str(r.get("代號", "")),
                "stock_name": r.get("名稱", ""),
                "今日%": None,
            })
        cat_map = stock_catalyst.annotate_picks_with_catalysts(records, market="TW")
        picks["催化劑"] = picks["代號"].astype(str).map(cat_map).fillna("")
    except Exception as _e:
        diagnostic_lines.append(f"⚠️ Step 6 (催化劑) 失敗 (non-fatal): {_e}")

    return {
        "picks": picks,
        "diagnostic": "\n".join(diagnostic_lines),
        "stats": stats,
    }
