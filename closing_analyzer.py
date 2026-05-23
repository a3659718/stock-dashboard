"""
closing_analyzer.py
盤後 15:00 收盤分析:

1) analyze_foreign_dumping() — 外資出貨嫌疑 top N
   依據 (越多分數越高):
     - 5 日累計外資賣超張數
     - 連續賣超天數 (consecutive_days)
     - 今日下跌 + 放量 (放量下跌 = 出貨)
     - 開高走低 (long upper shadow + 收黑)
     - 投信也跟賣 (兩大法人同方向)
     - 融資增加 (散戶接刀)

2) pick_next_day_breakout() — 整理結束 + 蓄勢待發 top 3
   依據 (越多分數越高):
     - 近 5-10 日橫盤波動小
     - 量縮整理 (5d 量 < 20d 量, 量能枯竭等噴)
     - 接近 MA20 上方 (~3% 內)
     - MA20 趨勢往上
     - 今日收紅 + 量略增 (轉強訊號)
     - 5/20 日漲跌幅在「健康整理」區間
     - 法人最近 5 天淨買 (bonus)

兩個都會丟給 Gemini 做最後排序 + 給 1 行理由。
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
import sector_pulse


# ---------------------------------------------------------------------------
# 1. 外資出貨偵測
# ---------------------------------------------------------------------------
def _score_dumping(chip: Dict) -> Tuple[float, Dict]:
    """從 chip_data 算出貨分數. 0 表示沒嫌疑."""
    inst = chip.get("institutional", {})
    margin = chip.get("margin", {})
    price = chip.get("price", {})

    score = 0.0
    reason_bits: List[str] = []

    # 外資 5 日累計賣超
    fi = inst.get("Foreign_Investor", {})
    fi_5d = fi.get("5d_total", 0) or 0
    fi_today = fi.get("today", 0) or 0
    consec = fi.get("consecutive_days", 0) or 0

    if fi_5d < -3000:  # 賣超 3000 張以上
        score += 2 + min(abs(fi_5d) / 8000, 3)
        reason_bits.append(f"外資5日賣 {abs(fi_5d):,}張")
    elif fi_5d < -1000:
        score += 1
        reason_bits.append(f"外資5日賣 {abs(fi_5d):,}張")

    if consec <= -3:  # 連續 3 天以上賣
        score += min(abs(consec), 7) * 0.7
        reason_bits.append(f"連續{abs(consec)}天賣")

    # 投信跟賣 (加重)
    it = inst.get("Investment_Trust", {})
    it_5d = it.get("5d_total", 0) or 0
    if fi_5d < -1000 and it_5d < -200:
        score += 1.5
        reason_bits.append(f"投信也賣 {abs(it_5d):,}張")

    # 今日下跌
    today_pct = price.get("今日%", 0) or 0
    vol_ratio = price.get("量比", 1) or 1
    if today_pct < -1.5:
        score += min(abs(today_pct) * 0.4, 2.5)
        if vol_ratio > 1.3:  # 放量下跌
            score += 2
            reason_bits.append(f"放量下跌 {today_pct:.1f}% 量比{vol_ratio:.1f}")
        else:
            reason_bits.append(f"下跌 {today_pct:.1f}%")

    # 融資增 (散戶接刀)
    margin_30d = margin.get("融資30日變化%", 0) or 0
    if margin_30d > 15 and fi_5d < -1000:
        score += 1
        reason_bits.append(f"融資+{margin_30d:.0f}%(散戶接刀)")

    return round(score, 2), {"reasons": " · ".join(reason_bits[:4])}


_FETCH_ONE_CHIP_LOGGED_ERR = False


def _fetch_one_chip(sid: str, name: str, days: int = 10) -> Optional[Dict]:
    """單檔抓籌碼 + score."""
    global _FETCH_ONE_CHIP_LOGGED_ERR
    try:
        chip = chip_analyzer.fetch_chip_data(sid, days=days)
        if not chip:
            return None
        score, ctx = _score_dumping(chip)
        if score < 1.5:  # 分數太低, 不入候選
            return None
        return {
            "stock_id": sid,
            "name": name,
            "score": score,
            "reasons": ctx["reasons"],
            "chip": {
                "fi_5d": chip.get("institutional", {}).get("Foreign_Investor", {}).get("5d_total", 0),
                "it_5d": chip.get("institutional", {}).get("Investment_Trust", {}).get("5d_total", 0),
                "fi_consec": chip.get("institutional", {}).get("Foreign_Investor", {}).get("consecutive_days", 0),
                "today_pct": chip.get("price", {}).get("今日%", 0),
                "vol_ratio": chip.get("price", {}).get("量比", 1),
                # 修正: 我把 chip_analyzer 的 None 意義 (資料缺失) 在此 fallback 為 0,
                # 避免下面 format spec :+.0f 對 None 炸 TypeError.
                "margin_30d": chip.get("margin", {}).get("融資30日變化%") or 0,
            },
        }
    except Exception as _e:
        # 第一次失敗印一次, 之後沉默 (避免 spam log) — universe 80 檔走完全部 noise 太多
        if not _FETCH_ONE_CHIP_LOGGED_ERR:
            print(f"[closing_analyzer._fetch_one_chip] {sid} {type(_e).__name__}: {_e}", flush=True)
            _FETCH_ONE_CHIP_LOGGED_ERR = True
        return None


def analyze_foreign_dumping(top_n: int = 5, max_scan: int = 80) -> List[Dict]:
    """掃 top max_scan 流動性最佳的台股, 找外資出貨嫌疑 top_n."""
    try:
        uni = sector_pulse.universe_with_industry(top_n=max_scan)
    except Exception as e:
        print(f"[closing_analyzer] universe failed: {e}", flush=True)
        return []
    if uni is None or uni.empty:
        return []

    name_map = uni.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in uni.columns else {}
    sids = uni["stock_id"].tolist()

    candidates: List[Dict] = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            ex.submit(_fetch_one_chip, sid, name_map.get(sid, ""), 10): sid
            for sid in sids
        }
        for f in as_completed(futures):
            r = f.result()
            if r:
                candidates.append(r)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top10 = candidates[:10]
    if not top10:
        return []

    # 丟給 Gemini 排序 + 給結論
    return _gemini_finalize_dumping(top10, top_n)


def _gemini_finalize_dumping(cands: List[Dict], top_n: int) -> List[Dict]:
    """請 Gemini 從候選中挑 top_n + 給 1 句理由 + 信心度."""
    try:
        import ai_analyzer as _ai
    except ImportError:
        return [{"stock_id": c["stock_id"], "name": c["name"],
                 "reason": c["reasons"], "confidence": min(int(c["score"] * 8), 95)}
                for c in cands[:top_n]]
    if not _ai.gemini_available():
        return [{"stock_id": c["stock_id"], "name": c["name"],
                 "reason": c["reasons"], "confidence": min(int(c["score"] * 8), 95)}
                for c in cands[:top_n]]

    blocks = []
    for c in cands:
        chip = c["chip"]
        blocks.append(
            f"- {c['stock_id']} {c['name']}: 外資5日 {chip['fi_5d']:+,}張, "
            f"連續{chip['fi_consec']}天, 投信5日 {chip['it_5d']:+,}張, "
            f"今日 {chip['today_pct']:+.1f}% 量比 {chip['vol_ratio']}, "
            f"融資30日 {chip['margin_30d']:+.0f}% / score={c['score']}"
        )

    prompt = (
        f"以下是 {len(cands)} 檔台股盤後籌碼, 請挑出 {top_n} 檔最像「外資出貨」的, "
        f"並給每檔一句具體理由 + 信心度 (0-100).\n\n"
        f"判斷重點: (1) 連續賣超 + (2) 放量下跌 + (3) 投信跟賣 + (4) 融資反向增加.\n\n"
        f"用嚴格 JSON 回 [{{stock_id, reason, confidence}}], 不要 markdown.\n\n"
        f"範例: [{{\"stock_id\":\"3017\",\"reason\":\"外資賣8000張連7天且量比1.8放量下跌\",\"confidence\":82}}]\n\n"
        f"候選:\n" + "\n".join(blocks)
    )

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel("gemini-2.5-flash")
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.3, "max_output_tokens": 1200,
                               "response_mime_type": "application/json"},
            safety_settings=_ai.get_safety_settings(),
        )
        text = (resp.text or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        # 補回 name
        name_map = {c["stock_id"]: c["name"] for c in cands}
        out = []
        for d in data[:top_n]:
            sid = str(d.get("stock_id", ""))
            out.append({
                "stock_id": sid,
                "name": name_map.get(sid, ""),
                "reason": str(d.get("reason", "")),
                "confidence": int(d.get("confidence", 50) or 50),
            })
        return out
    except Exception as e:
        print(f"[closing_analyzer] Gemini dumping failed: {e}", flush=True)
        return [{"stock_id": c["stock_id"], "name": c["name"],
                 "reason": c["reasons"], "confidence": min(int(c["score"] * 8), 95)}
                for c in cands[:top_n]]


# ---------------------------------------------------------------------------
# 2. 整理結束 + 隔天上漲機率高 top 3
# ---------------------------------------------------------------------------
def _score_breakout(df: pd.DataFrame) -> Tuple[float, Dict]:
    """從 daily K 線算「整理待噴」分數."""
    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    vol = df["Volume"].astype(float)

    if len(close) < 25:
        return 0.0, {}

    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])

    # MA20 + 趨勢
    ma20 = float(close.tail(20).mean())
    ma20_5d_ago = float(close.iloc[-25:-5].mean()) if len(close) >= 25 else ma20
    ma20_uptrend = ma20 > ma20_5d_ago

    # 距 MA20
    ma20_dist = abs(last - ma20) / ma20 if ma20 > 0 else 0

    # 量縮整理: 5d 量 / 20d 量
    vol_5d_avg = float(vol.tail(5).mean())
    vol_20d_avg = float(vol.tail(20).mean())
    vol_compression = vol_5d_avg / vol_20d_avg if vol_20d_avg > 0 else 1

    # 近 5 日波動 vs 近 20 日波動 (橫盤指標)
    range_5d = float(high.tail(5).max() - low.tail(5).min())
    range_20d = float(high.tail(20).max() - low.tail(20).min())
    range_ratio = range_5d / range_20d if range_20d > 0 else 1

    # 今日: 收紅 + 量略增 (轉強訊號)
    today_pct = (last / prev - 1) * 100 if prev > 0 else 0
    today_vol = float(vol.iloc[-1])
    avg_5d_vol = float(vol.iloc[-6:-1].mean())
    today_vol_ratio = today_vol / avg_5d_vol if avg_5d_vol > 0 else 1

    # 5/20 日漲跌
    pct_5d = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0
    pct_20d = (last / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0

    score = 0.0

    # MA20 趨勢 (重要)
    if ma20_uptrend:
        score += 2.0
    # 站上 MA20
    if last >= ma20:
        score += 1.5
    # 接近 MA20 (~3% 內 = 沒漲過頭)
    if ma20_dist < 0.03:
        score += 2.0
    elif ma20_dist < 0.06:
        score += 1.0
    # 量縮 (重要)
    if vol_compression < 0.80:
        score += 2.0
    elif vol_compression < 0.95:
        score += 1.0
    elif vol_compression > 1.5:
        score -= 1.0  # 量太大代表已噴, 不算「整理待噴」
    # 5 日橫盤
    if range_ratio < 0.45:
        score += 2.0
    elif range_ratio < 0.6:
        score += 1.0
    # 今日轉強
    if 0.3 < today_pct < 3.5 and today_vol_ratio > 1.15:
        score += 1.5
    # 整理區間 (5/20 日健康)
    if -3 <= pct_5d <= 4:
        score += 1.0
    if -8 <= pct_20d <= 12:
        score += 0.5

    metrics = {
        "current": round(last, 2),
        "ma20": round(ma20, 2),
        "today_pct": round(today_pct, 2),
        "pct_5d": round(pct_5d, 2),
        "pct_20d": round(pct_20d, 2),
        "vol_compression": round(vol_compression, 2),
        "range_compression": round(range_ratio, 2),
        "ma20_dist_pct": round(ma20_dist * 100, 2),
        "ma20_uptrend": bool(ma20_uptrend),
        "today_vol_ratio": round(today_vol_ratio, 2),
    }
    return round(score, 2), metrics


_FETCH_ONE_BREAKOUT_LOGGED_ERR = False


def _fetch_one_breakout(sid: str, name: str, market_type: str) -> Optional[Dict]:
    global _FETCH_ONE_BREAKOUT_LOGGED_ERR
    suffix = ".TWO" if market_type == "tpex" else ".TW"
    try:
        df = ds.fetch_yf_history(f"{sid}{suffix}", period="3mo", interval="1d")
        if df is None or df.empty or len(df) < 25:
            return None
        score, metrics = _score_breakout(df)
        if score < 5.5:
            return None
        return {
            "stock_id": sid,
            "name": name,
            "score": score,
            "metrics": metrics,
        }
    except Exception as _e:
        if not _FETCH_ONE_BREAKOUT_LOGGED_ERR:
            print(f"[closing_analyzer._fetch_one_breakout] {sid} {type(_e).__name__}: {_e}", flush=True)
            _FETCH_ONE_BREAKOUT_LOGGED_ERR = True
        return None


def pick_next_day_breakout(top_n: int = 3, max_scan: int = 150) -> List[Dict]:
    """掃 top max_scan, 找整理待噴 top_n."""
    try:
        uni = sector_pulse.universe_with_industry(top_n=max_scan)
    except Exception as e:
        print(f"[closing_analyzer] universe failed: {e}", flush=True)
        return []
    if uni is None or uni.empty:
        return []

    name_map = uni.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in uni.columns else {}
    market_map = uni.set_index("stock_id")["type"].to_dict() if "type" in uni.columns else {}
    sids = uni["stock_id"].tolist()

    candidates: List[Dict] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            ex.submit(_fetch_one_breakout, sid, name_map.get(sid, ""), market_map.get(sid, "twse")): sid
            for sid in sids
        }
        for f in as_completed(futures):
            r = f.result()
            if r:
                candidates.append(r)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top10 = candidates[:10]
    if not top10:
        return []

    # 註: 已拿掉自動籌碼過濾 — 籌碼 T+1 落後, 易錯過反彈動能 (台玻案例)
    return _gemini_finalize_breakout(top10, top_n)


def _gemini_finalize_breakout(cands: List[Dict], top_n: int) -> List[Dict]:
    """請 Gemini 從 10 檔候選挑 top_n + 一句理由 + 隔日上漲機率."""
    try:
        import ai_analyzer as _ai
    except ImportError:
        return _fallback_breakout(cands, top_n)
    if not _ai.gemini_available():
        return _fallback_breakout(cands, top_n)

    blocks = []
    for c in cands:
        m = c["metrics"]
        blocks.append(
            f"- {c['stock_id']} {c['name']}: "
            f"現價{m['current']} (今日{m['today_pct']:+.2f}%), "
            f"MA20={m['ma20']} (距{m['ma20_dist_pct']:.1f}%, 趨勢{'↑' if m['ma20_uptrend'] else '→'}), "
            f"量縮比{m['vol_compression']:.2f}, 5日範圍/20日={m['range_compression']:.2f}, "
            f"5日{m['pct_5d']:+.1f}%/20日{m['pct_20d']:+.1f}%, "
            f"今日量比{m['today_vol_ratio']}, score={c['score']}"
        )

    prompt = (
        f"下面是 {len(cands)} 檔技術面「整理結束 + 蓄勢待發」候選台股。"
        f"請挑出 {top_n} 檔「明日上漲機率最高」, 給每檔:\n"
        f"  - up_prob (隔日收紅機率, 0-100)\n"
        f"  - reason (1 句具體技術 + 籌碼理由, 不超過 30 字)\n"
        f"  - target_pct (合理的隔日漲幅預期, 0.5-5%)\n\n"
        f"判斷重點:\n"
        f"  - 量縮 + 横盤 + 今日轉強 = 隔日易續漲\n"
        f"  - MA20 上升 + 接近 MA20 = 在支撐位等動能\n"
        f"  - 距離 MA20 太遠 (>5%) 反而是已噴, 不選\n"
        f"  - 5/20 日漲跌幅在合理範圍 (沒漲過頭也沒崩盤)\n\n"
        f"用嚴格 JSON 回 [{{stock_id, up_prob, target_pct, reason}}], 不加 markdown.\n\n"
        f"範例: [{{\"stock_id\":\"2330\",\"up_prob\":72,\"target_pct\":1.8,"
        f"\"reason\":\"連續5日量縮整理在 MA20 附近, 今日放量收紅突破短期高\"}}]\n\n"
        f"候選:\n" + "\n".join(blocks)
    )

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel("gemini-2.5-flash")
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.3, "max_output_tokens": 1500,
                               "response_mime_type": "application/json"},
            safety_settings=_ai.get_safety_settings(),
        )
        text = (resp.text or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        if not isinstance(data, list):
            return _fallback_breakout(cands, top_n)

        meta_map = {c["stock_id"]: c for c in cands}
        out = []
        for d in data[:top_n]:
            sid = str(d.get("stock_id", ""))
            base = meta_map.get(sid, {})
            metrics = base.get("metrics", {})
            out.append({
                "stock_id": sid,
                "name": base.get("name", ""),
                "current": metrics.get("current"),
                "today_pct": metrics.get("today_pct"),
                "up_prob": int(d.get("up_prob", 50) or 50),
                "target_pct": float(d.get("target_pct", 1.0) or 1.0),
                "reason": str(d.get("reason", "")),
            })
        return out
    except Exception as e:
        print(f"[closing_analyzer] Gemini breakout failed: {e}", flush=True)
        return _fallback_breakout(cands, top_n)


def _fallback_breakout(cands: List[Dict], top_n: int) -> List[Dict]:
    """無 Gemini 時用 rule-based 排序."""
    out = []
    for c in cands[:top_n]:
        m = c["metrics"]
        out.append({
            "stock_id": c["stock_id"],
            "name": c["name"],
            "current": m.get("current"),
            "today_pct": m.get("today_pct"),
            "up_prob": min(int(c["score"] * 7), 88),
            "target_pct": 1.5,
            "reason": (
                f"{'量縮' if m['vol_compression'] < 0.85 else '量平'}橫盤"
                f"({m['range_compression']:.2f}), "
                f"距MA20 {m['ma20_dist_pct']:.1f}%"
                f"{'↑' if m['ma20_uptrend'] else ''}"
            ),
        })
    return out
