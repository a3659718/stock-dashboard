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

import copy
import datetime as dt
import json
import re
import threading
import time
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import data_sources as ds


# ---------------------------------------------------------------------------
# B10 修正: dual-cache 機制
# ---------------------------------------------------------------------------
# 問題: @st.cache_data 在 ThreadPoolExecutor 內呼叫會觸發
#       "missing ScriptRunContext" warning, 且 cache 命中率不穩.
# 解法: 多包一層 in-process TTL cache (用內建 dict + thread lock 實作,
#       不依賴 cachetools 外部套件), 任何 context 都能正常用.
#       Streamlit context 仍享有 @st.cache_data 的好處 (跨 session 共享).
_CHIP_LOCAL_CACHE: Dict[tuple, tuple] = {}  # key=(sid, days), value=(timestamp, data)
_CHIP_CACHE_TTL = 900  # 15 分鐘, 與 @st.cache_data 一致
_CHIP_CACHE_LOCK = threading.Lock()


def _local_cache_get(key: tuple):
    """H1 修正: 回傳 deepcopy, 避免 caller 修改污染下次取出的 dict."""
    with _CHIP_CACHE_LOCK:
        v = _CHIP_LOCAL_CACHE.get(key)
        if v is None:
            return None
        ts, data = v
        if time.time() - ts > _CHIP_CACHE_TTL:
            _CHIP_LOCAL_CACHE.pop(key, None)
            return None
    # 在 lock 外做 deepcopy (data 是 nested dict, 不會太大)
    return copy.deepcopy(data) if data is not None else None


def _local_cache_set(key: tuple, data):
    """H1 修正: 存入也 deepcopy, 確保即使 caller 之後修改自己的 reference 也不影響 cache."""
    # 在 lock 外 deepcopy, 減少 lock contention
    stored = copy.deepcopy(data) if data is not None else None
    with _CHIP_CACHE_LOCK:
        _CHIP_LOCAL_CACHE[key] = (time.time(), stored)
        # 防止 cache 無限增長 (上限 500 entry, LRU-ish)
        if len(_CHIP_LOCAL_CACHE) > 500:
            oldest = sorted(_CHIP_LOCAL_CACHE.items(), key=lambda x: x[1][0])[:50]
            for k, _ in oldest:
                _CHIP_LOCAL_CACHE.pop(k, None)


# ---------------------------------------------------------------------------
# 抓單檔籌碼資料 (對外接口)
# ---------------------------------------------------------------------------
def _is_main_streamlit_thread() -> bool:
    """偵測目前是否在 streamlit ScriptRunner thread.
    若不是 (例如 ThreadPoolExecutor worker thread / 排程 cron),
    呼叫 @st.cache_data 會發出 warning, 此時應該 bypass.
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


def fetch_chip_data(stock_id: str, days: int = 30) -> Dict:
    """彙整單檔股票的籌碼/法人/融資融券資料.

    B10 + M2 修正:
      1. 先查 in-process TTL cache (任何 context 都有效)
      2. 沒命中時:
         - 若在 streamlit main thread → 走 @st.cache_data 版本 (跨 session 共享)
         - 若在 worker thread / cron  → 直接呼叫 raw impl (避開 streamlit warning)
      3. 結果寫入 local cache 供下次跨 thread 共用
    """
    key = (str(stock_id), int(days))
    cached = _local_cache_get(key)
    if cached is not None:
        return cached
    if _is_main_streamlit_thread():
        data = _fetch_chip_data_impl_st(stock_id, days)
    else:
        # Worker thread / non-streamlit context: 跳過 streamlit cache
        data = _fetch_chip_data_impl_raw(stock_id, days)
    _local_cache_set(key, data)
    return data


@st.cache_data(ttl=900, show_spinner=False)
def _fetch_chip_data_impl_st(stock_id: str, days: int = 30) -> Dict:
    """Streamlit-cached 版本, 給 main thread 用 (跨 session 共享)."""
    return _fetch_chip_data_impl_raw(stock_id, days)


def _fetch_chip_data_impl_raw(stock_id: str, days: int = 30) -> Dict:
    """實際抓取實作 — 純函式, 沒 streamlit 裝飾, 可在任何 thread 安全呼叫."""
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()  # TPE 修正
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")

    # 1) 三大法人
    inst_df = ds._finmind_get_one(
        "TaiwanStockInstitutionalInvestorsBuySell", stock_id, start, end
    )
    inst_summary = {}
    # B15 修正: 檢查 name 欄位存在 (FinMind 偶爾回傳缺欄位的 dataset)
    if not inst_df.empty and "name" in inst_df.columns:
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
        # B1 修正: 30 日前餘額若 <= 0 直接給 None, 不要 fallback 成 1
        # (fallback 成 1 會讓 cur/1 變成 cur*100 - 100 % 的假爆增訊號)
        if "MarginPurchaseTodayBalance" in margin_df.columns:
            try:
                cur_margin = int(last.get("MarginPurchaseTodayBalance", 0))
            except (TypeError, ValueError):
                cur_margin = 0
            try:
                old_margin = int(first.get("MarginPurchaseTodayBalance", 0))
            except (TypeError, ValueError):
                old_margin = 0
            margin_summary["融資餘額"] = cur_margin
            if old_margin > 0:
                margin_summary["融資30日變化%"] = round((cur_margin / old_margin - 1) * 100, 1)
            else:
                margin_summary["融資30日變化%"] = None  # 30 日前無融資, 不可比較
        for col in ["ShortSaleTodayBalance", "ShortSaleAfterBalance"]:
            if col in margin_df.columns:
                try:
                    cur_short = int(last.get(col, 0))
                except (TypeError, ValueError):
                    cur_short = 0
                try:
                    old_short = int(first.get(col, 0))
                except (TypeError, ValueError):
                    old_short = 0
                margin_summary["融券餘額"] = cur_short
                if old_short > 0:
                    margin_summary["融券30日變化%"] = round((cur_short / old_short - 1) * 100, 1)
                else:
                    margin_summary["融券30日變化%"] = None
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
        # B2 修正: 量比的分母改用「不含當日」的 20 日均量, 避免自己稀釋自己
        # (原本 v.tail(20).mean() 包含 iloc[-1], 爆量時量比會被低估約 5-10%)
        ref_vol = v.iloc[-21:-1] if len(v) >= 21 else v.iloc[:-1]
        avg20_ex_today = float(ref_vol.mean()) if len(ref_vol) > 0 else 0.0
        price_summary["20日均量"] = int(avg20_ex_today) if avg20_ex_today > 0 else 0
        # H3 修正: 明確輸出「20日均量(張)」, 給 chip_filter 用, 避免下游
        # 用啟發式判斷單位. FinMind Trading_Volume 是「股」, 1 張 = 1000 股.
        price_summary["20日均量_張"] = int(avg20_ex_today / 1000) if avg20_ex_today > 0 else 0
        price_summary["量比"] = round(float(v.iloc[-1]) / avg20_ex_today, 2) if avg20_ex_today > 0 else 0
        # 量價背離: 累計漲幅 > 0 但近 5 日量縮 → 背離訊號
        if len(c) >= 6:
            recent_vol = v.iloc[-5:].mean()
            prev_vol = v.iloc[-15:-5].mean() or 1
            price_summary["近5日量能比"] = round(float(recent_vol / prev_vol), 2)

    # 4) 衍生指標: 券資比 + 法人共識 + 籌碼健康度
    derived = {
        "短券資比%": calc_short_margin_ratio(margin_summary),
        "法人共識": calc_chip_consensus(inst_summary),
        "籌碼健康度": calc_chip_health_score(inst_summary, margin_summary, price_summary),
    }

    return {
        "stock_id": stock_id,
        "institutional": inst_summary,
        "margin": margin_summary,
        "price": price_summary,
        "derived": derived,
    }


# ---------------------------------------------------------------------------
# 衍生籌碼指標 — 給 upside_screener / actionable_picks 共用
# ---------------------------------------------------------------------------
def calc_short_margin_ratio(margin_summary: Dict) -> Optional[float]:
    """券資比 = 融券餘額 / 融資餘額 * 100.
    > 30% 警示「軋空可能」, 也代表空方鎖籌, 反而是潛在上漲動能.
    """
    if not margin_summary:
        return None
    short_bal = margin_summary.get("融券餘額")
    margin_bal = margin_summary.get("融資餘額")
    if not short_bal or not margin_bal or margin_bal <= 0:
        return None
    return round(short_bal / margin_bal * 100, 2)


def calc_chip_consensus(inst_summary: Dict) -> Dict:
    """三大法人方向一致性指標.
    回傳 {direction: 'bullish'/'bearish'/'mixed'/'neutral', score: 0-3, detail: str}.
    score = 同向方數 (3 = 全部一致, 1 = 只有 1 家同向, 0 = 雜訊).
    自營商以「自行買賣」為準, 避險不計 (那是權證對沖).
    """
    if not inst_summary:
        return {"direction": "neutral", "score": 0, "detail": "(無法人資料)"}

    parties = []
    fi = inst_summary.get("Foreign_Investor") or {}
    it = inst_summary.get("Investment_Trust") or {}
    ds = inst_summary.get("Dealer_self") or {}  # 自行買賣
    if fi:
        parties.append(("外資", fi.get("5d_total", 0) or 0))
    if it:
        parties.append(("投信", it.get("5d_total", 0) or 0))
    if ds:
        parties.append(("自營", ds.get("5d_total", 0) or 0))
    if not parties:
        return {"direction": "neutral", "score": 0, "detail": "(無資料)"}

    n_bull = sum(1 for _, v in parties if v > 0)
    n_bear = sum(1 for _, v in parties if v < 0)
    if n_bull > n_bear:
        direction = "bullish"
        score = n_bull
    elif n_bear > n_bull:
        direction = "bearish"
        score = n_bear
    else:
        direction = "mixed"
        score = 0
    detail = " · ".join(f"{name}{'+' if v >= 0 else ''}{v:,}" for name, v in parties)
    return {"direction": direction, "score": int(score), "detail": detail}


def calc_chip_health_score(inst_summary: Dict, margin_summary: Dict,
                            price_summary: Dict) -> int:
    """0-100 的籌碼健康度分數. 越高代表「主力進場 + 散戶散去」越明顯.

    +30 法人 5d 共識買超 (3 家同向)
    +20 法人 5d 共識買超 (2 家同向)
    +10 投信連續買超 ≥ 3 天
    +15 融資 30 日減少 ≥ 5% (散戶散去)
    -10 融資 30 日增加 ≥ 15% (散戶接刀)
    +10 融券 ≥ 融資 25% (軋空潛能)
    +10 量增價漲 (今日%>1 且 量比>1.2)
    """
    score = 50  # 基準分
    consensus = calc_chip_consensus(inst_summary)
    if consensus["direction"] == "bullish":
        score += 30 if consensus["score"] >= 3 else 20 if consensus["score"] >= 2 else 10
    elif consensus["direction"] == "bearish":
        score -= 30 if consensus["score"] >= 3 else 20 if consensus["score"] >= 2 else 10

    it = (inst_summary or {}).get("Investment_Trust") or {}
    consec = it.get("consecutive_days", 0)
    if consec >= 3:
        score += 10
    elif consec <= -3:
        score -= 10

    margin_chg = (margin_summary or {}).get("融資30日變化%")
    if margin_chg is not None:
        if margin_chg <= -5:
            score += 15
        elif margin_chg >= 15:
            score -= 10

    short_ratio = calc_short_margin_ratio(margin_summary or {})
    if short_ratio is not None and short_ratio >= 25:
        score += 10  # 軋空潛能

    today_pct = (price_summary or {}).get("今日%", 0) or 0
    vol_ratio = (price_summary or {}).get("量比", 0) or 0
    if today_pct > 1 and vol_ratio > 1.2:
        score += 10
    elif today_pct < -1 and vol_ratio > 1.5:
        score -= 15  # 放量下跌 = 主力出貨警示

    return int(max(0, min(100, score)))


def _count_consecutive(net_list: List[float]) -> int:
    """回傳「連續買超天數」(若連續為負則回負值).
    B6 修正:
      - 前置過濾 NaN (FinMind 偶爾回空值)
      - 對 net == 0 (買賣相抵) 視為「中性」延續方向, 不打斷連續計算
      - 若全部都是 0 或無資料, 回 0
    """
    if not net_list:
        return 0
    # 過濾 NaN
    clean = [v for v in net_list if pd.notna(v)]
    if not clean:
        return 0
    # 從最近一筆找出明確方向 (跳過 0)
    direction = 0
    for v in reversed(clean):
        if v > 0:
            direction = 1
            break
        elif v < 0:
            direction = -1
            break
    if direction == 0:
        return 0  # 全部都是 0
    days = 0
    for v in reversed(clean):
        if v == 0:
            days += 1  # 0 視為延續, 不打斷
            continue
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
            f"融資 {margin.get('融資餘額','—'):,} ({(margin.get('融資30日變化%') or 0):+.1f}% 30d) · "
            f"融券 {margin.get('融券餘額','—'):,} ({(margin.get('融券30日變化%') or 0):+.1f}% 30d)"
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
