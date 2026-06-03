"""
stage_analysis.py
Stan Weinstein Stage Analysis (30 週 MA = 150 trading days ≈ 30 weeks).

4 個階段:
  Stage 1 (Basing 底部整理): 30W MA 平緩, 股價在 MA 上下震盪
  Stage 2 (Advancing 上升期) — 唯一買進點: 30W MA 上升 + 股價 > MA + 量增突破
  Stage 3 (Topping 頂部): MA 趨緩, 股價 vs MA 距離縮小
  Stage 4 (Declining 下跌期) — 完全避開: 30W MA 下降 + 股價 < MA

API:
  classify_stage(symbol, market="auto") -> Dict
  stage_emoji(stage) -> str
"""
from __future__ import annotations

from typing import Dict, Optional


def classify_stage(symbol: str, market: str = "auto") -> Dict:
    """回 {"stage": 1-4, "label": str, "ma30w": float, "ma30w_slope_pct": float, ...}."""
    out = {
        "stage": None, "label": "Unknown",
        "current": None, "ma30w": None,
        "ma30w_slope_pct": None,
        "price_above_ma": None,
        "vol_increase": None,
        "advice": "—",
    }
    try:
        import data_sources as ds
        # 偵測 market
        if market == "auto":
            import re as _re_st; market = "TW" if (symbol.isdigit() or _re_st.match(r"^\d{4}[A-Z]$", symbol)) else "US"
        # 抓 1y daily 至少 150 個交易日
        if market == "TW":
            df = ds.fetch_yf_history(f"{symbol}.TW", period="1y", interval="1d")
            if df is None or df.empty:
                df = ds.fetch_yf_history(f"{symbol}.TWO", period="1y", interval="1d")
        else:
            df = ds.fetch_yf_history(symbol, period="1y", interval="1d")
        if df is None or df.empty or len(df) < 160:
            return out

        c = df["Close"].astype(float)
        v = df["Volume"].astype(float)
        cur = float(c.iloc[-1])
        # 30 週 MA ≈ 150 日 (一週 5 個交易日)
        ma_30w = float(c.rolling(150).mean().iloc[-1])
        ma_30w_prev = float(c.rolling(150).mean().iloc[-21])  # 4 週前
        # MA 斜率 (4 週變化 %)
        slope_pct = (ma_30w / ma_30w_prev - 1) * 100 if ma_30w_prev > 0 else 0
        # 股價 vs MA
        price_above = cur > ma_30w
        price_dist_pct = (cur / ma_30w - 1) * 100 if ma_30w > 0 else 0
        # 量增 (最近 4 週均量 vs 4 個月前)
        vol_recent = float(v.iloc[-20:].mean())
        vol_old = float(v.iloc[-80:-20].mean()) if len(v) >= 80 else vol_recent
        vol_increase = vol_recent / vol_old if vol_old > 0 else 1

        out.update({
            "current": round(cur, 2),
            "ma30w": round(ma_30w, 2),
            "ma30w_slope_pct": round(slope_pct, 2),
            "price_above_ma": price_above,
            "price_dist_pct": round(price_dist_pct, 2),
            "vol_increase": round(vol_increase, 2),
        })

        # 分類規則
        if slope_pct > 1.0 and price_above:
            # Stage 2: 上升期
            out["stage"] = 2
            if vol_increase >= 1.3:
                out["label"] = "🟢 Stage 2 突破期 (買進區)"
                out["advice"] = "突破 + 量增, 進場時機"
            else:
                out["label"] = "🟢 Stage 2 上升期 (持有)"
                out["advice"] = "上升趨勢, 持有/拉回找買點"
        elif slope_pct < -1.0 and not price_above:
            # Stage 4: 下跌期
            out["stage"] = 4
            out["label"] = "🔴 Stage 4 下跌期 (避開)"
            out["advice"] = "下跌趨勢, 完全避開不接刀"
        elif slope_pct > 0.5 and price_above and price_dist_pct > 15:
            # Stage 3: 頂部 (漲多)
            out["stage"] = 3
            out["label"] = "🟡 Stage 3 頂部 (準備出場)"
            out["advice"] = "漲多, 留意轉弱訊號, 分批停利"
        elif abs(slope_pct) <= 1.0:
            # Stage 1: 整理
            if price_above:
                out["stage"] = 1
                out["label"] = "🟡 Stage 1 底部整理 (等突破)"
                out["advice"] = "整理階段, 等待 Stage 2 突破訊號再進場"
            else:
                # 接近 Stage 4
                out["stage"] = 1
                out["label"] = "🟡 Stage 1 整理 (留意往下)"
                out["advice"] = "整理偏弱, 不急進場"
        else:
            out["stage"] = 1
            out["label"] = "🟡 Stage 1 整理"
            out["advice"] = "趨勢不明確, 觀望"
    except Exception as e:
        print(f"[stage_analysis] {symbol} failed: {e}", flush=True)
    return out


def stage_emoji(stage: Optional[int]) -> str:
    return {1: "🟡", 2: "🟢", 3: "🟠", 4: "🔴"}.get(stage or 0, "⚪")
