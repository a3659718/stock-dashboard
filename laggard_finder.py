"""
laggard_finder.py
強勢族群中的「落後股」分析。

核心概念：題材在輪動時，**還沒跟漲的個股**往往是好的進場點
(避免追高領漲股，挑跟風機會)。

策略:
  1. 找平均漲幅 >= +1.5% 的熱門族群
  2. 在這些族群裡，挑出今日漲幅明顯落後 (<= 族群平均 - 2%) 的個股
  3. 但這些股要有「跡象」：量比 > 0.8、不是大跌
  4. 用 Gemini 為每檔分析「跟漲機會」(高/中/低) + 1-2 句理由
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import data_sources as ds
import sector_pulse


# ---------------------------------------------------------------------------
# 落後股偵測
# ---------------------------------------------------------------------------
def find_tw_laggards(min_theme_avg: float = 1.5,
                      lag_threshold: float = 2.0,
                      max_themes: int = 5,
                      max_laggards_per_theme: int = 5) -> Dict:
    """找台股強勢族群裡的落後股。
    回傳: {theme_name: {avg_pct, leaders, laggards}}.
    """
    hot = sector_pulse.compute_hot_themes()
    themes_df = hot.get("themes")
    leaders_map = hot.get("leaders") or {}
    if themes_df is None or themes_df.empty:
        return {}

    info = ds.get_taiwan_stock_info()
    market_map = info.set_index("stock_id")["type"].to_dict()
    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}

    out: Dict[str, Dict] = {}
    seen_laggard_ids: set = set()

    for _, row in themes_df.head(max_themes).iterrows():
        theme = row["題材"]
        avg = float(row.get("平均%", 0) or 0)
        if avg < min_theme_avg:
            continue

        # 抓該題材所有股票的盤中 metrics
        members = sector_pulse.TW_THEMES.get(theme, [])
        if not members:
            continue
        quotes = sector_pulse.fetch_intraday_metrics(members, market_map)
        if quotes.empty:
            continue

        # 落後股篩選
        threshold = avg - lag_threshold
        lag = quotes.copy()
        lag = lag[
            (lag["今日%"].fillna(-99) < threshold)  # 漲幅明顯落後
            & (lag["今日%"].fillna(-99) > -3)        # 不是大跌的
            & (lag["量比"].fillna(0) >= 0.8)         # 量能不能太冷
        ]
        # 跨題材去重 (一檔只放在最熱題材)
        lag = lag[~lag["stock_id"].isin(seen_laggard_ids)]
        if lag.empty:
            continue

        lag = lag.sort_values("量比", ascending=False).head(max_laggards_per_theme)
        if "stock_name" not in lag.columns:
            lag["stock_name"] = lag["stock_id"].map(name_map).fillna("")

        out[theme] = {
            "theme_avg": round(avg, 2),
            "leaders": leaders_map.get(theme),
            "laggards": lag,
        }
        seen_laggard_ids.update(lag["stock_id"].tolist())

    return out


def find_us_laggards(min_sector_pct: float = 0.8,
                      lag_threshold: float = 1.5,
                      max_sectors: int = 3,
                      max_laggards_per_sector: int = 5) -> Dict:
    """找美股強勢板塊裡的落後股."""
    sectors = ds.fetch_sector_rotation()
    if sectors is None or sectors.empty or "1d_%" not in sectors.columns:
        return {}

    out: Dict[str, Dict] = {}
    seen_us: set = set()

    sectors_sorted = sectors.sort_values("1d_%", ascending=False).head(max_sectors)

    # 延遲 import 避免 circular
    try:
        import market_open_picks
        US_SECTOR_STOCKS = market_open_picks.US_SECTOR_STOCKS
    except Exception:
        US_SECTOR_STOCKS = {}

    for _, sec in sectors_sorted.iterrows():
        sym = sec["symbol"]
        sector_pct = float(sec.get("1d_%", 0))
        if sector_pct < min_sector_pct:
            continue
        members = US_SECTOR_STOCKS.get(sym, [])
        if not members:
            continue

        rows = []
        for s in members:
            if s in seen_us:
                continue
            try:
                import market_open_picks as mop
                m = mop._us_stock_metrics(s)
            except Exception:
                m = None
            if not m:
                continue
            rows.append(m)
        if not rows:
            continue
        df = pd.DataFrame(rows)
        threshold = sector_pct - lag_threshold
        df = df[
            (df["今日%"].fillna(-99) < threshold)
            & (df["今日%"].fillna(-99) > -3)
            & (df["量比"].fillna(0) >= 0.8)
        ]
        if df.empty:
            continue
        df = df.sort_values("量比", ascending=False).head(max_laggards_per_sector)
        leaders_df = pd.DataFrame(rows)
        leaders_df = leaders_df.sort_values("今日%", ascending=False).head(5)

        out[f"{sym} {sec.get('sector','')}"] = {
            "theme_avg": round(sector_pct, 2),
            "leaders": leaders_df,
            "laggards": df,
        }
        seen_us.update(df["symbol"].tolist())
    return out


# ---------------------------------------------------------------------------
# Gemini 分析跟漲機會
# ---------------------------------------------------------------------------
def analyze_laggards_with_gemini(laggards_data: Dict, market: str = "TW",
                                   model: str = "gemini-2.5-flash") -> Dict[str, Dict]:
    """為每檔落後股給「跟漲機會」評等 + 理由.
    回傳: {stock_id: {chance: "高/中/低", reason: "..."}}
    """
    try:
        import ai_analyzer as _ai
    except ImportError:
        return {}
    if not _ai.gemini_available():
        return {}
    if not laggards_data:
        return {}

    # 組 prompt
    blocks = []
    all_lag_ids: List[str] = []
    for theme, info in laggards_data.items():
        avg = info.get("theme_avg")
        leaders = info.get("leaders")
        laggards = info.get("laggards")
        if laggards is None or (hasattr(laggards, "empty") and laggards.empty):
            continue
        leaders_str = ""
        if leaders is not None and not leaders.empty:
            top3 = leaders.head(3)
            leaders_str = ", ".join(
                f"{r.get('stock_id','') or r.get('symbol','')} ({r.get('今日%')}%)"
                for _, r in top3.iterrows()
            )
        lag_str_parts = []
        for _, r in laggards.iterrows():
            sid = str(r.get("stock_id") or r.get("symbol") or "")
            nm = r.get("stock_name", "") or ""
            today_pct = r.get("今日%")
            ratio = r.get("量比")
            five = r.get("5日%")
            lag_str_parts.append(
                f"{sid} {nm} (今日 {today_pct}%, 量比 {ratio}x, 5d {five}%)"
            )
            all_lag_ids.append(sid)
        blocks.append(
            f"[{theme} 平均 +{avg}%]\n"
            f"領漲: {leaders_str}\n"
            f"落後: {chr(10).join('  - ' + p for p in lag_str_parts)}"
        )

    if not blocks:
        return {}

    market_label = "台股" if market == "TW" else "美股"
    prompt = f"""你是專業{market_label}分析師。以下是今日各個強勢族群裡「還沒跟漲」的個股。
請判斷每個落後股「跟漲機會」(高/中/低) 並給 1-2 句具體理由 (產品定位、客戶結構、技術面、籌碼面 等)。

避免空話。如果落後股屬於該題材但與龍頭差異大 (例如不是核心受惠者)，標「低」。
如果落後股就是該題材的供應鏈成員、籌碼還沒拉抬、有跟漲空間，標「高」。
中間情況標「中」。

請用嚴格 JSON 格式回應，key 是 stock_id，value 是 {{"chance": "高/中/低", "reason": "..."}}.
範例:
{{"3093": {{"chance": "高", "reason": "探針卡核心客戶結構與龍頭重疊，籌碼面還沒拉抬，跟漲空間明確"}},
 "8016": {{"chance": "低", "reason": "PCB 業務佔比不到 15%，主力為傳統電子代工，與板塊輪動相關性低"}}}}

不要加任何前後 markdown，只回 JSON。

待分析:

{chr(10).join(blocks)}"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 2000,
                "response_mime_type": "application/json",
            },
            safety_settings=_ai.get_safety_settings(),
        )
        text = (resp.text or "").strip()
        if not text:
            return {}
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        out = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out[str(k)] = {
                    "chance": str(v.get("chance", "中")),
                    "reason": str(v.get("reason", "")),
                }
        return out
    except Exception:
        return {}
