"""
concept_finder.py
隱藏概念股探勘 — 從新聞反向挖「市場還沒發現」的題材受惠股.

問題: sector_pulse.TW_THEMES 是 hardcoded 名單 (像「矽光子」族群就那幾檔知名股),
      但有些公司實質有做該題材卻沒被歸進主流名單 — 等市場發現時已經漲爛.

解法:
  1. 給 keyword (如 "矽光子" / "AI 伺服器" / "低軌衛星")
  2. 掃近 30-60 天「所有台股新聞」(用 FinMind TaiwanStockNews 跟 TaiwanStockMomentousReview)
  3. 找新聞 title / summary 含該 keyword 的 stock_id, 統計被提及次數
  4. 對照 TW_THEMES, 標記 [已知 / 隱藏]
  5. 排序: 隱藏 + 提及次數高 = 最值得關注的隱藏概念股

對外接口:
  find_hidden_concept_stocks(keyword, days=60, max_scan=200) -> List[Dict]
  fmt_concept_finder_results(results) -> str  (給 dashboard 顯示用)
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional, Set

import pandas as pd
import streamlit as st

import data_sources as ds
import sector_pulse


# 預設常見題材關鍵字 (給 dashboard select 用)
PRESET_THEMES = [
    "矽光子",
    "AI 伺服器",
    "AI 邊緣",
    "AI PC",
    "低軌衛星",
    "電動車",
    "儲能",
    "重電",
    "散熱",
    "ABF 載板",
    "高頻高速",
    "PCB",
    "綠能",
    "氫能",
    "機器人",
    "無人機",
    "光通訊",
    "智慧醫療",
    "生技",
    "鴻海集團",
    "黃仁勳概念",
    "Apple 供應鏈",
    "META 供應鏈",
    "Tesla 供應鏈",
]


def _build_keyword_variants(keyword: str) -> List[str]:
    """把 keyword 變成幾個變體, 增加 match 機會.

    "矽光子" → ["矽光子"]
    "AI 伺服器" → ["AI 伺服器", "AI伺服器", "AI server"]
    "低軌衛星" → ["低軌衛星", "LEO", "Low-Earth Orbit"]
    """
    base = keyword.strip()
    variants = [base]
    # 移除空格版
    if " " in base:
        variants.append(base.replace(" ", ""))
    # 一些手動 alias
    alias_map = {
        "AI 伺服器": ["AI server", "AI Server"],
        "低軌衛星": ["LEO", "低軌道衛星"],
        "電動車": ["EV", "電動汽車"],
        "重電": ["重電族群", "電網"],
        "ABF 載板": ["載板", "ABF"],
        "PCB": ["印刷電路板"],
        "Apple 供應鏈": ["蘋果供應鏈"],
        "Tesla 供應鏈": ["特斯拉供應鏈"],
        "鴻海集團": ["鴻海"],
        "黃仁勳概念": ["NVIDIA 概念", "輝達"],
    }
    if base in alias_map:
        variants.extend(alias_map[base])
    return list(set(variants))


def _get_known_theme_stocks(keyword: str) -> Set[str]:
    """從 sector_pulse.TW_THEMES 找這個 keyword 對應的已知名單.

    match: 單向 `keyword in theme_name` (user 通常輸入較短較通用 keyword,
    希望去 match TW_THEMES 裡的具體 theme). 雙向 substring 會造成 user
    輸入長 keyword (含一段是 theme 名) 時, 反向 match 把不相關的 theme 拉進來,
    導致 known_stocks 被過度擴張 → hidden 數低估.

    e.g. keyword="生技醫療":
      - 單向: 不 match "生技" (因為 "生技醫療" not in "生技")
        ↑ 但 reverse match "生技" in "生技醫療" 會錯誤地拉進"生技"族群
      - 改成單向 keyword in theme_name 後不會誤拉
    """
    known: Set[str] = set()
    kw_lower = keyword.lower().strip()
    for theme_name, sids in sector_pulse.TW_THEMES.items():
        # 單向: 只當 keyword 是 theme_name 的子字串時 match
        # (例: "AI" → match "AI 伺服器" / "AI 邊緣")
        if kw_lower in theme_name.lower():
            known.update(str(s) for s in sids)
    return known


@st.cache_data(ttl=21600, show_spinner=False)  # 6 hr cache (新聞每幾小時就會變)
def find_hidden_concept_stocks(keyword: str, days: int = 60,
                                  max_scan: int = 200) -> List[Dict]:
    """從近 N 天新聞反向挖隱藏概念股.

    Args:
        keyword: 題材關鍵字, 如 "矽光子"
        days: 掃幾天新聞 (預設 60)
        max_scan: 最多掃幾檔 (流動性 top N, 避免 quota 爆)

    Returns:
        [
          {
            "stock_id": "1816",
            "name": "矽光科技",
            "industry": "電子零件",
            "mentions": 5,                # 被新聞提到次數
            "hidden": True / False,        # 不在 TW_THEMES 名單裡 = True
            "recent_news_titles": ["...", "..."],  # 最近 3 條提到的新聞
        }, ...
        ]
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    variants = _build_keyword_variants(keyword)
    known_stocks = _get_known_theme_stocks(keyword)

    # Universe: 取流動性 top N 台股
    try:
        info = ds.get_taiwan_stock_info()
        if "type" in info.columns:
            # 過濾 tse / tpex
            info = info[info["type"].isin(["twse", "tpex"])]
        # 取前 max_scan 檔 (FinMind 通常按市值/流動性排)
        universe = info.head(max_scan)
        name_map = universe.set_index("stock_id")["stock_name"].to_dict()
        industry_map = universe.set_index("stock_id").get(
            "industry_category", pd.Series()).to_dict()
    except Exception as e:
        print(f"[concept_finder] universe failed: {e}", flush=True)
        return []

    _today_tw = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()  # TPE 修正
    end_date = _today_tw.strftime("%Y-%m-%d")
    start_date = (_today_tw - dt.timedelta(days=days)).strftime("%Y-%m-%d")

    universe_sids = set(universe["stock_id"].astype(str).tolist())

    # 重要: 改用「一次抓全市場新聞」, 不再每檔 call 一次 (省 200x FinMind quota)
    # FinMind 不傳 data_id 會回所有股票的新聞 + Momentous, 然後 in-memory filter
    news_titles_by_sid: Dict[str, List[str]] = {}
    try:
        news_all = ds._finmind_get(
            "TaiwanStockNews", start_date=start_date, end_date=end_date,
        )
        if news_all is not None and not news_all.empty:
            for _, r in news_all.iterrows():
                sid = str(r.get("stock_id", "") or "")
                if not sid or sid not in universe_sids:
                    continue
                t = str(r.get("title", "") or "")
                if t:
                    news_titles_by_sid.setdefault(sid, []).append(t)
    except Exception as e:
        print(f"[concept_finder] TaiwanStockNews bulk failed: {e}", flush=True)

    # MomentousReview 也一次抓 (FinMind 欄位可能叫 Description / OperatingType, 多 fallback)
    try:
        moment_all = ds._finmind_get(
            "TaiwanStockMomentousReview", start_date=start_date, end_date=end_date,
        )
        if moment_all is not None and not moment_all.empty:
            content_col = None
            for col in ("title", "content", "Description", "OperatingType", "Topic"):
                if col in moment_all.columns:
                    content_col = col
                    break
            if content_col:
                for _, r in moment_all.iterrows():
                    sid = str(r.get("stock_id", "") or "")
                    if not sid or sid not in universe_sids:
                        continue
                    t = str(r.get(content_col, "") or "")
                    if t:
                        news_titles_by_sid.setdefault(sid, []).append(t)
    except Exception as e:
        print(f"[concept_finder] MomentousReview bulk failed: {e}", flush=True)

    # 對每檔 in-memory match keyword variants
    stock_mentions: Dict[str, Dict] = {}
    variants_lower = [v.lower() for v in variants]
    for sid, titles in news_titles_by_sid.items():
        matched_titles = []
        for t in titles:
            t_lower = t.lower()
            if any(v in t_lower for v in variants_lower):
                matched_titles.append(t[:120])
        if matched_titles:
            stock_mentions[sid] = {
                "stock_id": sid,
                "name": name_map.get(sid, ""),
                "industry": industry_map.get(sid, ""),
                "mentions": len(matched_titles),
                "hidden": sid not in known_stocks,
                "recent_news_titles": matched_titles[:3],
            }

    # 排序: 隱藏 + mentions 高 在前; 已知 + mentions 高 在後
    results = list(stock_mentions.values())
    results.sort(key=lambda x: (-int(x["hidden"]), -x["mentions"]))
    return results


def fmt_concept_finder_results_md(results, keyword):
    """格式化成 markdown 給 Streamlit 顯示."""
    if not results:
        return f"沒找到「{keyword}」相關的股票新聞."
    hidden = [r for r in results if r["hidden"]]
    known = [r for r in results if not r["hidden"]]
    lines = [f"### 「{keyword}」 探勘結果",
             f"找到 {len(results)} 檔有相關新聞 ({len(hidden)} 隱藏 / {len(known)} 已知)."]
    if hidden:
        lines.append("\n#### 🔎 隱藏概念股")
        for r in hidden[:10]:
            lines.append(f"- **{r['stock_id']} {r['name']}** ({r['mentions']} 次)")
    return "\n".join(lines)
