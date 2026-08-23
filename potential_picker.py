"""
potential_picker.py
台股潛力股推薦 + 目標價估算 (用 Gemini)。

用在三個推播時段:
  - us_open (22:00 台北 美股開盤後 30 分): 推薦次日台股可關注的潛力股
  - us_close (06:00 台北 美股收盤後 2h): 推薦今日台股開盤前可進場的標的
  - holiday_news (假日 22:30 台北): 推薦復盤後可關注的潛力股

每支股票包含:
  - 進場區間 / 目標價 / 停損點
  - 預期報酬 / 停損距離
  - 上漲機率 (高/中/低)
  - 持有時間建議
  - 推薦理由
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
# 抓單檔股票價位資訊 (給 Gemini 計算目標價用)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def _compute_stock_levels(stock_id: str, market_type: str = "twse") -> Optional[Dict]:
    """從 yfinance 抓股價，計算 MA、20d/60d 高低、近期表現."""
    suffix = ".TWO" if market_type == "tpex" else ".TW"
    df = ds.fetch_yf_history(f"{stock_id}{suffix}", period="3mo", interval="1d")
    if df.empty or len(df) < 20:
        return None
    try:
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        last = float(close.iloc[-1])
        if last <= 0:
            return None
        return {
            "current": round(last, 2),
            "ma20": round(float(close.iloc[-20:].mean()), 2),
            "ma60": round(float(close.iloc[-60:].mean()), 2) if len(close) >= 60 else None,
            "high_20d": round(float(high.iloc[-20:].max()), 2),
            "low_20d": round(float(low.iloc[-20:].min()), 2),
            "high_60d": round(float(high.iloc[-60:].max()), 2) if len(high) >= 60 else None,
            "pct_5d": round((last / float(close.iloc[-6]) - 1) * 100, 2) if len(close) >= 6 else 0,
            "pct_20d": round((last / float(close.iloc[-21]) - 1) * 100, 2) if len(close) >= 21 else 0,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Gemini 選股 + 目標價
# ---------------------------------------------------------------------------
def _gemini_pick_with_targets(candidates: List[Dict], macro_context: str = "",
                                top_n: int = 5,
                                model: str = "gemini-2.5-flash") -> List[Dict]:
    """讓 Gemini 從 candidates 挑 top_n 並給目標價."""
    try:
        import ai_analyzer as _ai
    except ImportError:
        return []
    if not _ai.gemini_available() or not candidates:
        return []

    blocks = []
    for c in candidates:
        blocks.append(
            f"{c['stock_id']} {c.get('name','')} ({c.get('theme','')}): "
            f"現 {c.get('current')}, MA20 {c.get('ma20')}, "
            f"20d高 {c.get('high_20d')}, 20d低 {c.get('low_20d')}, "
            f"5d {c.get('pct_5d', 0):+.1f}%, 20d {c.get('pct_20d', 0):+.1f}%"
        )

    prompt = f"""你是專業台股分析師。請從下列候選池選 {top_n} 支「最有潛力」的個股，每支提供進場/目標/停損價。

【今日市場 macro 背景】
{macro_context if macro_context else '(無特殊事件)'}

【候選池】
{chr(10).join(blocks)}

選擇標準（依重要性）:
1. 配合美股對應板塊強勢，受惠聯動效應
2. 還沒大漲 (5d 漲幅 < 10% 為佳，避免追高)
3. 站穩 MA20，技術面健康（不在下跌趨勢中）
4. 題材新穎或主流（AI、半導體、低軌衛星、無人機等）

目標價估算邏輯:
- 短線目標: 20 日新高 + ATR×1.5 (約 +5~10%)
- 波段目標: 60 日新高 (約 +10~20%)
- 停損: MA20 下方 3% 或 20 日低點

請用嚴格 JSON list 格式回應 (不要 markdown):
[
  {{
    "stock_id": "6669",
    "name": "緯穎",
    "theme": "AI 伺服器",
    "current": 2480,
    "entry_low": 2400,
    "entry_high": 2480,
    "target_price": 2750,
    "target_pct": 11,
    "stop_loss": 2300,
    "stop_pct": -7,
    "win_prob": "高",
    "hold_period": "波段 1-2 月",
    "reason": "AI Server ODM Direct，受惠 NVIDIA Blackwell 出貨延續"
  }}
]

只回 JSON list，不要任何前後文字 / markdown。"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={
                "temperature": 0.4,
                "max_output_tokens": 2500,
                "response_mime_type": "application/json",
            },
            safety_settings=_ai.get_safety_settings(),
        )
        text = (resp.text or "").strip()
        if not text:
            return []
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        if isinstance(data, list):
            # Bug fix: Gemini 是自由生成 JSON, 數值型欄位的型別/合理性完全沒有
            # 保證 (可能給字串、可能停損高於進場價等亂序資料). 這些價位會被
            # actionable_picks 直接拿去顯示/推播/算 R:R/算部位大小, 壞資料
            # 沒被擋下的話輕則顯示錯誤價位, 重則讓 dashboard 某個分頁因型別
            # 不合直接崩潰。這裡做最後一道清理防線再回傳。
            cleaned = [_validate_gemini_pick(x) for x in data[:top_n]]
            return [c for c in cleaned if c]
        return []
    except Exception:
        return []


def _validate_gemini_pick(item: Dict) -> Optional[Dict]:
    """驗證 + 清理 Gemini 回傳的單筆 pick.

    數值型欄位一律 try 轉 float, 轉不了 (型別錯) 或不合理 (NaN/inf) 就清成 None,
    不讓壞資料一路帶到下游才爆炸。另外做「多頭進場合理性」檢查: 停損 < 進場
    區間 <= 目標價, 任一不合理就把整組價位清掉 (而非顯示/推播明顯錯亂的價位),
    但保留股票代號/名稱/理由等其餘欄位, 讓使用者仍看得到這檔股票, 只是價位
    標記為「資料異常」不顯示。
    """
    if not isinstance(item, dict):
        return None
    sid = str(item.get("stock_id", "") or "").strip()
    if not sid:
        return None

    def _num(key):
        v = item.get(key)
        try:
            f = float(v)
            if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
                return None
            return f
        except (TypeError, ValueError):
            return None

    current = _num("current")
    entry_low = _num("entry_low")
    entry_high = _num("entry_high")
    target_price = _num("target_price")
    stop_loss = _num("stop_loss")

    levels_ok = (
        entry_low is not None and entry_high is not None and
        target_price is not None and stop_loss is not None and
        stop_loss < entry_low <= entry_high < target_price
    )
    if not levels_ok:
        entry_low = entry_high = target_price = stop_loss = None

    out = dict(item)
    out["stock_id"] = sid
    out["name"] = str(item.get("name", "") or "")
    out["current"] = current
    out["entry_low"] = entry_low
    out["entry_high"] = entry_high
    out["target_price"] = target_price
    out["stop_loss"] = stop_loss
    if not levels_ok:
        out["_levels_invalid"] = True
    return out


# ---------------------------------------------------------------------------
# 候選池構建 (依 context 決定要看哪些題材)
# ---------------------------------------------------------------------------
def _build_candidates(themes: List[str], max_candidates: int = 40) -> List[Dict]:
    """根據題材 list 收集候選股 + 補上價位資訊.
    FinMind 失敗時 graceful return []，不要炸掉整個 process.
    """
    try:
        info = ds.get_taiwan_stock_info()
    except Exception as e:
        print(f"[potential_picker] FinMind get_taiwan_stock_info failed: {e}", flush=True)
        return []
    if info is None or info.empty:
        print("[potential_picker] taiwan_stock_info is empty", flush=True)
        return []
    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}
    market_map = info.set_index("stock_id")["type"].to_dict() if "type" in info.columns else {}

    seen: set = set()
    pool: List[Dict] = []
    for theme in themes:
        for sid in sector_pulse.TW_THEMES.get(theme, []):
            if sid in seen:
                continue
            seen.add(sid)
            pool.append({
                "stock_id": sid,
                "name": name_map.get(sid, ""),
                "theme": theme,
                "_market_type": market_map.get(sid, "twse"),
            })

    # 抓 levels (限制最多 max_candidates 避免太久)
    enriched: List[Dict] = []
    for c in pool[:max_candidates]:
        levels = _compute_stock_levels(c["stock_id"], c["_market_type"])
        if not levels:
            continue
        # 過濾: 漲過頭 / 大跌的不要
        if levels["pct_5d"] > 18:
            continue
        if levels["pct_20d"] < -15:
            continue
        c.update(levels)
        enriched.append(c)
    return enriched


# ---------------------------------------------------------------------------
# 對外 API
# ---------------------------------------------------------------------------
def find_picks_from_us_sectors(us_sectors_df: pd.DataFrame,
                                  macro_context: str = "",
                                  top_n: int = 5,
                                  min_us_pct: float = 0.4) -> List[Dict]:
    """根據強勢的美股板塊，找對應 TW 題材的潛力股.
    任何 step 失敗 → return [] 而非 raise，避免炸掉上層推播流程.
    """
    try:
        try:
            import market_open_picks as mop
        except ImportError:
            return []

        if us_sectors_df is None or us_sectors_df.empty:
            themes = list(sector_pulse.TW_THEMES.keys())[:8]  # fallback
        else:
            strong = us_sectors_df[us_sectors_df["1d_%"] > min_us_pct]
            themes = set()
            for _, r in strong.iterrows():
                sym = r.get("symbol", "")
                themes.update(mop.US_SECTOR_TO_TW_THEMES.get(sym, []))
            if not themes:
                themes = list(sector_pulse.TW_THEMES.keys())[:8]
            themes = list(themes)

        candidates = _build_candidates(themes, max_candidates=40)
        if not candidates:
            return []
        return _gemini_pick_with_targets(candidates, macro_context, top_n)
    except Exception as e:
        print(f"[potential_picker] find_picks_from_us_sectors failed: {e}", flush=True)
        return []


def find_picks_for_holiday(macro_context: str = "", top_n: int = 5) -> List[Dict]:
    """假日推播用 — 從所有熱門題材池選潛力股.
    任何 step 失敗 → return [].
    """
    try:
        themes = list(sector_pulse.TW_THEMES.keys())[:12]
        candidates = _build_candidates(themes, max_candidates=50)
        if not candidates:
            return []
        return _gemini_pick_with_targets(candidates, macro_context, top_n)
    except Exception as e:
        print(f"[potential_picker] find_picks_for_holiday failed: {e}", flush=True)
        return []

# 註: 已拿掉 _exclude_messy_chip — 籌碼資料 T+1 落後, 容易錯過外資出貨後反彈動能
# 如將來要恢復可參考 chip_filter.fetch_messy_map()
