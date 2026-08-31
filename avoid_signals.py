"""
avoid_signals.py
反向 / 避開訊號偵測.

目的: 跟所有偏多訊號 (強勢族群、催化劑、股票候選等) 對等地做空頭警示,
讓推播不會永遠只給「該買什麼」, 也提醒「該避開什麼」.

判斷邏輯 (TW 為主, 任 2 條成立即列入):
  A. 外資 / 投信 連 N 日賣超 (N >= 5)
  B. 跌破月線 (close < 20MA)
  C. 量價背離 (近 5 日下跌 + 量比 > 1.3 = 放量下跌)
  D. 法人下修 (yfinance / news 找 "下修" "降評" 關鍵字)
  E. 高融資 + 法人賣 (散戶接刀)

對外接口:
    find_avoid_picks(top_n=5) -> List[Dict]
    每筆: {stock_id, name, score, reasons, current, today_pct}
"""

from __future__ import annotations

from typing import Dict, List, Optional

import data_sources as ds


def _score_avoid(chip: Dict) -> tuple:
    """對一檔股的 chip + price 資料打「該避開」分數.
    回 (score, reasons[])
    """
    score = 0.0
    reasons: List[str] = []

    inst = chip.get("institutional") or {}
    fi = inst.get("Foreign_Investor") or {}
    it = inst.get("Investment_Trust") or {}
    price = chip.get("price") or {}
    margin = chip.get("margin") or {}

    # A. 外資連續賣超
    fi_consec = fi.get("consecutive_days", 0) or 0
    fi_5d = fi.get("5d_total", 0) or 0
    if fi_consec >= 5 and fi_5d < -3000:
        score += 3
        reasons.append(f"外資連賣 {fi_consec} 日 ({fi_5d:,}張)")
    elif fi_5d < -2000:
        score += 1.5
        reasons.append(f"外資 5 日賣超 {abs(fi_5d):,}張")

    # 投信跟賣 加重
    it_5d = it.get("5d_total", 0) or 0
    if it_5d < -500 and fi_5d < 0:
        score += 1.5
        reasons.append(f"投信也賣 {abs(it_5d):,}張")

    # B. 中期走弱 (近 20 日跌幅)
    # Bug fix (2026-08): chip_analyzer 寫入的 price key 只有
    # close / 今日% / 5d漲跌% / 20d漲跌% / 量比 / 今日量 / 20日均量(_張)。
    # 「距 20MA %」「5日%」「收盤」這三個字串在整個 repo 只出現在本檔, 沒有任何地方寫入
    # → 這兩條判斷永遠拿 0 分 (滿分從 11 掉到 8), current 也恆為 None,
    #   害 market_open_picks 的 `if sid and cur:` 恆為 False → avoid_pick 一筆都沒進 signal_tracker。
    # 20MA 距離 chip_analyzer 沒有提供, 改用它有的 5d/20d 漲跌幅來近似「跌破月線」的意思。
    ma20_diff = price.get("20d漲跌%")
    try:
        if ma20_diff is not None and float(ma20_diff) < -2:
            score += 2
            reasons.append(f"近 20 日 {float(ma20_diff):.1f}% (中期走弱)")
    except (TypeError, ValueError):
        pass

    # C. 放量下跌
    today_pct = price.get("今日%", 0) or 0
    vol_ratio = price.get("量比", 1) or 1
    try:
        today_pct = float(today_pct)
        vol_ratio = float(vol_ratio)
        if today_pct < -2 and vol_ratio > 1.3:
            score += 2
            reasons.append(f"放量下跌 {today_pct:.1f}% 量比{vol_ratio:.1f}x")
        # 5 日連跌
        five_d = price.get("5d漲跌%")
        if five_d is not None and float(five_d) < -5:
            score += 1
            reasons.append(f"近 5 日 {float(five_d):.1f}%")
    except (TypeError, ValueError):
        pass

    # D. 高融資 + 外資賣 (散戶接刀)
    margin_30d = margin.get("融資30日變化%", 0) or 0
    try:
        if float(margin_30d) > 20 and fi_5d < -1500:
            score += 1.5
            reasons.append(f"散戶接刀 (融資+{float(margin_30d):.0f}% / 外資 {fi_5d:,}張)")
    except (TypeError, ValueError):
        pass

    return round(score, 2), reasons


def _fetch_one_avoid(sid: str, name: str, days: int = 10) -> Optional[Dict]:
    """單檔分析."""
    try:
        import chip_analyzer
        chip = chip_analyzer.fetch_chip_data(sid, days=days)
        if not chip:
            return None
        score, reasons = _score_avoid(chip)
        if score < 4:  # 門檻較高 — 避免誤殺
            return None
        price = chip.get("price") or {}
        return {
            "stock_id": sid,
            "name": name,
            "score": score,
            "reasons": " · ".join(reasons[:3]),
            "current": price.get("close"),
            "today_pct": price.get("今日%"),
        }
    except Exception as _e:
        print(f"[avoid_signals] {sid} failed: {_e}", flush=True)
        return None


def find_avoid_picks(top_n: int = 5, max_scan: int = 80) -> List[Dict]:
    """掃 top max_scan 流動性最佳的台股, 找「該避開」top_n.

    對稱於 closing_analyzer.analyze_foreign_dumping, 但訊號條件更廣 (除了外資出貨,
    還包含跌破月線 / 散戶接刀等).
    """
    try:
        import sector_pulse
        uni = sector_pulse.universe_with_industry(top_n=max_scan)
    except Exception as e:
        print(f"[avoid_signals] universe failed: {e}", flush=True)
        return []
    if uni is None or uni.empty:
        return []

    results: List[Dict] = []
    name_map = uni.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in uni.columns else {}
    for sid in uni["stock_id"].tolist():
        nm = name_map.get(sid, "")
        rec = _fetch_one_avoid(str(sid), str(nm))
        if rec:
            results.append(rec)
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results[:top_n]
