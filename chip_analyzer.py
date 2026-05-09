"""
chip_analyzer.py
籌碼 / 主力換手分析。

訊號:
  1. 三大法人連續買賣超 (持續買 vs 突然賣)
  2. 投信 vs 外資 vs 自營商方向是否一致
  3. 融資餘額 (散戶熱度) + 融券餘額 (空頭壓力)
  4. 量價關係 (放量上漲 = 健康；放量下跌 = 出貨；縮量整理 = 等待)
  5. 開高走低 / 上下影線 形態 (籌碼換手訊號)

Gemini 整合:
  - 給每檔股票判斷:
    - 主力動向 (進場 / 出貨 / 持平)
    - 換手機率 (0-100%, 主力是否在洗籌碼)
    - 操作建議 (出清 / 持有 / 加碼觀察)
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import data_sources as ds


# ---------------------------------------------------------------------------
# 抓單檔籌碼資料
# ---------------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def fetch_chip_data(stock_id: str, days: int = 30) -> Dict:
    """彙整單檔股票的籌碼/法人/融資融券資料.

    @st.cache_data ttl=900 (15 分鐘): emerging_themes / sector_pulse / closing_analyzer
    都會密集呼叫此函式對相同 sid 重複叫, 不 cache 會撞 FinMind rate limit.
    """
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")

    # 1) 三大法人
    inst_df = ds._finmind_get_one(
        "TaiwanStockInstitutionalInvestorsBuySell", stock_id, start, end
    )
    inst_summary = {}
    if not inst_df.empty:
        inst_df["date"] = pd.to_datetime(inst_df["date"])
        inst_df["net"] = inst_df["buy"].astype(float) - inst_df["sell"].astype(float)
        for name, group in inst_df.groupby("name"):
            g = group.sort_values("date")
            def _safe_int(v, default=0):
                try:
                    if pd.isna(v):
                        return default
                    return int(v)
                except Exception:
                    return default
            inst_summary[name] = {
                "30d_total": _safe_int(g["net"].sum()),
                "5d_total": _safe_int(g.tail(5)["net"].sum()),
                "today": _safe_int(g.iloc[-1]["net"]) if len(g) else 0,
                "consecutive_days": _count_consecutive(g["net"].tolist()),
            }

    # 2) 融資融券
    margin_df = ds._finmind_get_one(
        "TaiwanStockMarginPurchaseShortSale", stock_id, start, end
    )
    margin_summary = {}
    if not margin_df.empty:
        margin_df["date"] = pd.to_datetime(margin_df["date"])
        margin_df = margin_df.sort_values("date")
        last = margin_df.iloc[-1]
        first = margin_df.iloc[0]
        if "MarginPurchaseTodayBalance" in margin_df.columns:
            cur_margin = int(last.get("MarginPurchaseTodayBalance", 0))
            old_margin = int(first.get("MarginPurchaseTodayBalance", 0)) or 1
            margin_summary["融資餘額"] = cur_margin
            margin_summary["融資30日變化%"] = round((cur_margin / old_margin - 1) * 100, 1) if old_margin else 0
        for col in ["ShortSaleTodayBalance", "ShortSaleAfterBalance"]:
            if col in margin_df.columns:
                cur_short = int(last.get(col, 0))
                old_short = int(first.get(col, 0)) or 1
                margin_summary["融券餘額"] = cur_short
                margin_summary["融券30日變化%"] = round((cur_short / old_short - 1) * 100, 1) if old_short else 0
                break

    # 3) 量價 (FinMind daily)
    daily = ds._finmind_get_one("TaiwanStockPrice", stock_id, start, end)
    price_summary = {}
    if not daily.empty:
        if "max" in daily.columns and "high" not in daily.columns:
            daily = daily.rename(columns={"max": "high", "min": "low"})
        daily["date"] = pd.to_datetime(daily["date"])
        daily = daily.sort_values("date")
        c = daily["close"].astype(float)
        v = daily["Trading_Volume"].astype(float)
        price_summary["close"] = round(float(c.iloc[-1]), 2)
        price_summary["20d漲跌%"] = round((c.iloc[-1] / c.iloc[0] - 1) * 100, 2) if len(c) >= 2 else 0
        price_summary["5d漲跌%"] = round((c.iloc[-1] / c.iloc[-6] - 1) * 100, 2) if len(c) >= 6 else 0
        price_summary["今日%"] = round((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2) if len(c) >= 2 else 0
        price_summary["今日量"] = int(v.iloc[-1])
        price_summary["20日均量"] = int(v.tail(20).mean())
        price_summary["量比"] = round(float(v.iloc[-1] / v.tail(20).mean()), 2) if v.tail(20).mean() > 0 else 0
        # 量價背離: 累計漲幅 > 0 但近 5 日量縮 → 背離訊號
        if len(c) >= 6:
            recent_vol = v.iloc[-5:].mean()
            prev_vol = v.iloc[-15:-5].mean() or 1
            price_summary["近5日量能比"] = round(float(recent_vol / prev_vol), 2)

    return {
        "stock_id": stock_id,
        "institutional": inst_summary,
        "margin": margin_summary,
        "price": price_summary,
    }


def _count_consecutive(net_list: List[float]) -> int:
    """回傳「連續買超天數」(若連續為負則回負值)."""
    if not net_list:
        return 0
    direction = 1 if net_list[-1] > 0 else -1
    days = 0
    for v in reversed(net_list):
        if (v > 0 and direction > 0) or (v < 0 and direction < 0):
            days += 1
        else:
            break
    return days * direction


# ---------------------------------------------------------------------------
# Gemini 批次分析
# ---------------------------------------------------------------------------
def analyze_chips_batch(stock_ids: List[str], stock_names: Dict[str, str] = None,
                          model: str = "gemini-2.5-flash") -> Dict[str, Dict]:
    """為多檔股票一次做籌碼分析.
    回傳 {stock_id: {direction, change_prob, recommendation, reason}}.
    """
    try:
        import ai_analyzer as _ai
    except ImportError:
        return {}
    if not _ai.gemini_available() or not stock_ids:
        return {}

    # 收集每檔資料
    chip_data: Dict[str, Dict] = {}
    for sid in stock_ids[:15]:  # 最多 15 檔避免 prompt 過長
        try:
            chip_data[sid] = fetch_chip_data(sid, days=30)
        except Exception:
            continue
    if not chip_data:
        return {}

    # 組 prompt blocks
    blocks = []
    for sid, data in chip_data.items():
        nm = (stock_names or {}).get(sid, "")
        inst = data.get("institutional", {})
        margin = data.get("margin", {})
        price = data.get("price", {})

        inst_str_parts = []
        for who, info in inst.items():
            zh = {"Foreign_Investor": "外資", "Investment_Trust": "投信",
                   "Dealer_self": "自營(自行)", "Dealer_Hedging": "自營(避險)"}.get(who, who)
            inst_str_parts.append(f"{zh} 30d {info['30d_total']:+,} 5d {info['5d_total']:+,} 連續{info['consecutive_days']}d")
        inst_str = " · ".join(inst_str_parts) if inst_str_parts else "(無法人資料)"

        margin_str = (
            f"融資 {margin.get('融資餘額','—'):,} ({margin.get('融資30日變化%',0):+.1f}% 30d) · "
            f"融券 {margin.get('融券餘額','—'):,} ({margin.get('融券30日變化%',0):+.1f}% 30d)"
            if margin else "(無融資融券資料)"
        )

        price_str = (
            f"收 {price.get('close','—')} 今日 {price.get('今日%',0):+.2f}% · "
            f"5d {price.get('5d漲跌%',0):+.2f}% · 20d {price.get('20d漲跌%',0):+.2f}% · "
            f"量比 {price.get('量比','—')}x"
            if price else "(無量價)"
        )

        blocks.append(
            f"### {sid} {nm}\n"
            f"  法人: {inst_str}\n"
            f"  融資券: {margin_str}\n"
            f"  量價: {price_str}"
        )

    prompt = f"""你是專業籌碼面分析師。下面是 {len(chip_data)} 檔台股的法人 / 融資融券 / 量價資料。
請為每檔判斷：

1. **direction** (主力動向): "進場" / "出貨" / "持平" / "換手"
2. **change_prob** (主力換手機率, 0-100): 主力洗籌碼的可能性
3. **recommendation**: "加碼" / "持有觀察" / "減碼" / "出清" / "不進場"
4. **reason** (1-2 句具體理由)

判斷依據:
- 法人連續買超 + 融資減 + 量增價漲 → 主力進場 (recommendation: 加碼/持有)
- 法人賣超 + 融資增 + 開高走低 → 主力出貨給散戶 (recommendation: 減碼/出清)
- 法人方向不一致 + 量大價平 → 籌碼換手 (recommendation: 持有觀察)
- 融券大增 + 融資減 → 主力倒貨給空方 (recommendation: 不進場)

請用嚴格 JSON 格式回應 {{stock_id: {{direction, change_prob, recommendation, reason}}}}。
範例:
{{"2330": {{"direction":"進場","change_prob":25,"recommendation":"加碼","reason":"外資+投信連續7天買超且融資減少，主力顯然在吸籌"}},
  "3017": {{"direction":"出貨","change_prob":75,"recommendation":"減碼","reason":"法人賣超3天加上融資暴增200%，散戶接刀風險高"}}}}

不要加任何前後 markdown，只回 JSON。

待分析個股:

{chr(10).join(blocks)}"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 2500,
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
                    "direction": str(v.get("direction", "")),
                    "change_prob": int(v.get("change_prob", 0) or 0),
                    "recommendation": str(v.get("recommendation", "")),
                    "reason": str(v.get("reason", "")),
                }
        return out
    except Exception as e:
        print(f"[chip_analyzer] analyze_chips_batch failed: {e}", flush=True)
        return {}
