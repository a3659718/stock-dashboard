"""
holdings_analyzer.py
持倉清單每日 (盤後 15:00) 完整分析:

  per-stock:
    - 技術面: MA20, KD, MACD, 量比, 5/20 日漲跌
    - 籌碼面: 外資/投信 5 日累計、融資 30 日變化
    - 新聞面: 過去 7 天個股新聞摘要 (yfinance get_news)
    - Gemini 綜合判斷:
        action ∈ {持有, 加碼, 減碼, 出清}
        confidence (0-100)
        target_short  (1 週目標價)
        target_mid    (1 月目標價)
        stop_loss     (建議停損)
        reason        (1-2 句具體理由)
        risks         (主要風險)
    - 隔日漲機率 (Gemini)

對外:
  analyze_all_holdings()  → list[dict]
"""

from __future__ import annotations

import datetime as dt
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import pandas as pd

import chip_analyzer
import data_sources as ds
import holdings_store


# ---------------------------------------------------------------------------
# 技術指標
# ---------------------------------------------------------------------------
def _compute_technicals(df: pd.DataFrame) -> Dict:
    if df is None or df.empty or len(df) < 25:
        return {}
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float)

    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) >= 2 else last
    pct_today = (last / prev - 1) * 100 if prev > 0 else 0
    pct_5d = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0
    pct_20d = (last / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0

    ma5 = float(close.tail(5).mean())
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else None

    # KD (9, 3, 3)
    period = 9
    rsv_list = []
    for i in range(period - 1, len(close)):
        h = float(high.iloc[i - period + 1: i + 1].max())
        l = float(low.iloc[i - period + 1: i + 1].min())
        c = float(close.iloc[i])
        rsv = ((c - l) / (h - l)) * 100 if h > l else 50
        rsv_list.append(rsv)
    k_val = 50.0
    d_val = 50.0
    k_series = []
    d_series = []
    for r in rsv_list:
        k_val = (2 * k_val + r) / 3
        d_val = (2 * d_val + k_val) / 3
        k_series.append(k_val)
        d_series.append(d_val)
    kd_signal = ""
    if len(k_series) >= 2:
        if k_series[-1] > d_series[-1] and k_series[-2] <= d_series[-2]:
            kd_signal = "黃金交叉"
        elif k_series[-1] < d_series[-1] and k_series[-2] >= d_series[-2]:
            kd_signal = "死亡交叉"
        elif k_series[-1] > d_series[-1]:
            kd_signal = "K>D"
        else:
            kd_signal = "K<D"

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    macd_signal = ""
    if len(hist) >= 2:
        if hist.iloc[-1] > 0 and hist.iloc[-2] <= 0:
            macd_signal = "翻紅"
        elif hist.iloc[-1] < 0 and hist.iloc[-2] >= 0:
            macd_signal = "翻黑"
        elif hist.iloc[-1] > 0:
            macd_signal = "紅柱"
        else:
            macd_signal = "黑柱"

    # 量比 (今日量 / 5 日均量)
    vol_5d = float(vol.iloc[-6:-1].mean()) if len(vol) >= 6 else 0
    vol_ratio = float(vol.iloc[-1] / vol_5d) if vol_5d > 0 else 1

    # MA 狀態描述
    ma_status_parts = []
    if last >= ma20:
        ma_status_parts.append("站上MA20")
    else:
        ma_status_parts.append("跌破MA20")
    if ma60 is not None:
        if last >= ma60:
            ma_status_parts.append("站季線")
        else:
            ma_status_parts.append("跌破季線")

    return {
        "current": round(last, 2),
        "today_pct": round(pct_today, 2),
        "pct_5d": round(pct_5d, 2),
        "pct_20d": round(pct_20d, 2),
        "ma5": round(ma5, 2),
        "ma20": round(ma20, 2),
        "ma60": round(ma60, 2) if ma60 else None,
        "ma_status": " · ".join(ma_status_parts),
        "kd_k": round(k_series[-1], 2) if k_series else None,
        "kd_d": round(d_series[-1], 2) if d_series else None,
        "kd_signal": kd_signal,
        "macd_hist": round(float(hist.iloc[-1]), 4) if len(hist) else None,
        "macd_signal": macd_signal,
        "vol_ratio": round(vol_ratio, 2),
    }


# ---------------------------------------------------------------------------
# 新聞 (重用 yfinance)
# ---------------------------------------------------------------------------
_FETCH_NEWS_LOGGED_ERR = False


def _fetch_news(stock_id: str, name: str) -> List[Dict]:
    """抓近期該檔個股新聞 (yfinance get_news)."""
    global _FETCH_NEWS_LOGGED_ERR
    last_err = None
    try:
        import yfinance as yf
        for suffix in [".TW", ".TWO"]:
            try:
                t = yf.Ticker(f"{stock_id}{suffix}")
                news = t.news or []
                if news:
                    out = []
                    for n in news[:5]:
                        title = n.get("title") or ""
                        publisher = n.get("publisher") or ""
                        link = n.get("link") or ""
                        if title:
                            out.append({"title": title, "publisher": publisher, "link": link})
                    if out:
                        return out
            except Exception as _e:
                last_err = _e
                continue
    except Exception as _e:
        last_err = _e
    # 第一次失敗印一次 — 401/429 應該至少 log 一次, 不要永遠靜默
    if last_err is not None and not _FETCH_NEWS_LOGGED_ERR:
        print(f"[holdings_analyzer._fetch_news] {stock_id} {type(last_err).__name__}: {last_err}", flush=True)
        _FETCH_NEWS_LOGGED_ERR = True
    return []


# ---------------------------------------------------------------------------
# Gemini 判斷
# ---------------------------------------------------------------------------
def _gemini_holding_advice(item: Dict, tech: Dict, chip: Dict, news: List[Dict]) -> Dict:
    """Gemini 給單檔持倉的綜合建議."""
    try:
        import ai_analyzer as _ai
    except ImportError:
        return _fallback_advice(item, tech, chip)
    if not _ai.gemini_available():
        return _fallback_advice(item, tech, chip)

    sid = item["stock_id"]
    name = item.get("name", "")
    ep = item.get("entry_price")
    cur = tech.get("current", 0)

    inst = chip.get("institutional", {}) or {}
    fi = inst.get("Foreign_Investor", {}) or {}
    it = inst.get("Investment_Trust", {}) or {}
    margin = chip.get("margin", {}) or {}

    ep_str = f"進場 {ep}" if ep else "未填進場價"
    roi_str = ""
    if ep and ep > 0:
        roi = (cur / ep - 1) * 100
        roi_str = f"投報率 {roi:+.2f}%"

    news_str = ""
    if news:
        news_str = "\n  最近新聞:\n" + "\n".join(
            f"    - {n['title']} [{n['publisher']}]" for n in news[:3]
        )

    prompt = f"""你是台股專業券商分析師, 客戶持有 {sid} {name}, 請給每日交易建議.

【持倉狀況】
  {ep_str}
  現價 {cur} {roi_str}

【技術面】
  今日 {tech.get('today_pct',0):+.2f}% / 5日 {tech.get('pct_5d',0):+.2f}% / 20日 {tech.get('pct_20d',0):+.2f}%
  MA: {tech.get('ma_status','—')}
  KD: K={tech.get('kd_k')} D={tech.get('kd_d')} ({tech.get('kd_signal','')})
  MACD: {tech.get('macd_signal','')}
  量比 {tech.get('vol_ratio',1)}x

【籌碼面】
  外資 5 日累計 {fi.get('5d_total',0):+,} 張, 連續 {fi.get('consecutive_days',0)} 天
  投信 5 日累計 {it.get('5d_total',0):+,} 張
  融資 30 日變化 {margin.get('融資30日變化%',0):+.1f}%
{news_str}

請以嚴格 JSON 格式回應 (不加 markdown), 包含:
  - action: "持有" / "加碼" / "減碼" / "出清"
  - confidence: 0-100 (對 action 判斷的信心)
  - target_short: 短期目標價 (1 週合理價)
  - target_mid: 中期目標價 (1 月合理價)
  - stop_loss: 建議停損價
  - reason: 1-2 句具體理由 (技術 + 籌碼 + 新聞 綜合)
  - risks: 主要風險 (一句)
  - next_day_up_prob: 隔日上漲機率 (0-100)

範例:
{{"action":"持有","confidence":72,"target_short":625,"target_mid":660,"stop_loss":580,"reason":"站穩 MA20 量比 1.3 加上外資轉買, 動能仍在","risks":"美股盤前若大跌可能拖累","next_day_up_prob":58}}
"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel("gemini-2.5-flash")
        resp = m.generate_content(
            prompt,
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 600,
                "response_mime_type": "application/json",
            },
            safety_settings=_ai.get_safety_settings(),
        )
        text = (resp.text or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        d = json.loads(text)
        return {
            "action": str(d.get("action", "持有")),
            "confidence": int(d.get("confidence", 50) or 50),
            "target_short": float(d.get("target_short") or 0) or None,
            "target_mid": float(d.get("target_mid") or 0) or None,
            "stop_loss": float(d.get("stop_loss") or 0) or None,
            "reason": str(d.get("reason", "")),
            "risks": str(d.get("risks", "")),
            "next_day_up_prob": int(d.get("next_day_up_prob", 50) or 50),
        }
    except Exception as e:
        print(f"[holdings] Gemini failed for {sid}: {e}", flush=True)
        return _fallback_advice(item, tech, chip)


def _fallback_advice(item: Dict, tech: Dict, chip: Dict) -> Dict:
    """無 Gemini 時用 rule-based 判斷."""
    cur = tech.get("current", 0)
    pct_5d = tech.get("pct_5d", 0)
    pct_20d = tech.get("pct_20d", 0)
    ma_status = tech.get("ma_status", "")
    inst = chip.get("institutional", {}) or {}
    fi_5d = inst.get("Foreign_Investor", {}).get("5d_total", 0) or 0

    score = 0
    if "站上MA20" in ma_status: score += 1
    if "站季線" in ma_status: score += 1
    if pct_5d > 0: score += 1
    if pct_20d > 0: score += 1
    if fi_5d > 1000: score += 2
    if fi_5d < -3000: score -= 2

    if score >= 3:
        action = "持有"
        conf = 65
    elif score >= 1:
        action = "持有"
        conf = 50
    elif score >= -1:
        action = "減碼"
        conf = 55
    else:
        action = "出清"
        conf = 65

    return {
        "action": action,
        "confidence": conf,
        "target_short": round(cur * 1.03, 2) if cur else None,
        "target_mid": round(cur * 1.08, 2) if cur else None,
        "stop_loss": round(cur * 0.93, 2) if cur else None,
        "reason": f"技術 score {score} (rule-based 無 Gemini)",
        "risks": "—",
        "next_day_up_prob": min(max(50 + score * 5, 25), 75),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _analyze_one(item: Dict) -> Optional[Dict]:
    sid = item.get("stock_id", "")
    if not sid:
        return None
    name = item.get("name", "")
    # daily K
    df = None
    for suffix in [".TW", ".TWO"]:
        df = ds.fetch_yf_history(f"{sid}{suffix}", period="3mo", interval="1d")
        if df is not None and not df.empty:
            break
    if df is None or df.empty:
        return None
    tech = _compute_technicals(df)
    if not tech:
        return None
    # chip
    try:
        chip = chip_analyzer.fetch_chip_data(sid, days=10)
    except Exception:
        chip = {}
    # news
    try:
        news = _fetch_news(sid, name)
    except Exception:
        news = []
    # gemini
    advice = _gemini_holding_advice(item, tech, chip, news)

    return {
        "stock_id": sid,
        "name": name,
        "entry_price": item.get("entry_price"),
        "shares": item.get("shares"),
        "note": item.get("note", ""),
        "tech": tech,
        "chip": {
            "fi_5d": (chip.get("institutional", {}).get("Foreign_Investor", {}).get("5d_total", 0)) if chip else 0,
            "it_5d": (chip.get("institutional", {}).get("Investment_Trust", {}).get("5d_total", 0)) if chip else 0,
            "fi_consec": (chip.get("institutional", {}).get("Foreign_Investor", {}).get("consecutive_days", 0)) if chip else 0,
            "margin_30d_pct": (chip.get("margin", {}).get("融資30日變化%", 0)) if chip else 0,
        },
        "news": news,
        "advice": advice,
    }


def analyze_all_holdings(max_workers: int = 5) -> List[Dict]:
    """掃 holdings 清單, 對每檔做完整分析."""
    items = holdings_store.load_holdings()
    if not items:
        return []
    out: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_analyze_one, item) for item in items]
        for f in as_completed(futures):
            r = f.result()
            if r:
                out.append(r)
    # 維持原始 holdings 順序
    order = {it["stock_id"]: i for i, it in enumerate(items)}
    out.sort(key=lambda x: order.get(x["stock_id"], 999))
    return out
