"""
chip_filter.py
籌碼面過濾器 — 找出「籌碼亂掉、外資撤離、散戶接刀」的股票, 從買進名單剔除.

共用邏輯 — 由 closing_analyzer / potential_picker / tw_screener 等模組使用.

判斷「籌碼亂」標準 (任一觸發就算亂):
  1. 外資 5 日累計賣超 >= 4000 張
  2. 外資單日大賣 >= 8000 張 (像台玻 1802 那種 23000 張規模)
  3. 外資連續賣 >= 4 天 + 投信也賣超 (兩大法人共識看空)
  4. 融資 30 日 +25% 以上 (散戶接刀, 籌碼從主力流向散戶)
  5. 過去 20 日跌幅 <= -10% (中期已破位, 套牢壓力大)

使用方式:
    from chip_filter import filter_out_messy
    clean_ids, excluded = filter_out_messy(["2330", "1802", ...])
    # excluded = {"1802": "外資5日賣18000張 · 融資+22%散戶接刀"}
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple

import chip_analyzer


# ---------------------------------------------------------------------------
# 單檔判斷
# ---------------------------------------------------------------------------
def is_chip_messy(chip_data: Dict) -> Tuple[bool, str]:
    """判斷單檔籌碼是否亂掉.
    Args:
        chip_data: chip_analyzer.fetch_chip_data() 回傳的 dict
    Returns:
        (is_messy, reason)  — reason 是觸發的訊號 (中文, 用 · 分隔)
    """
    if not chip_data:
        return False, ""

    inst = chip_data.get("institutional", {}) or {}
    margin = chip_data.get("margin", {}) or {}
    price = chip_data.get("price", {}) or {}

    fi = inst.get("Foreign_Investor", {}) or {}
    it = inst.get("Investment_Trust", {}) or {}

    fi_5d = fi.get("5d_total", 0) or 0
    fi_today = fi.get("today", 0) or 0
    fi_consec = fi.get("consecutive_days", 0) or 0
    it_5d = it.get("5d_total", 0) or 0

    margin_30d = margin.get("融資30日變化%", 0) or 0
    pct_20d = price.get("20d漲跌%", 0) or 0

    reasons: List[str] = []

    # 1) 外資 5 日累計大賣
    if fi_5d <= -4000:
        reasons.append(f"外資5日賣{abs(fi_5d):,}張")

    # 2) 外資單日大賣 (像台玻 2.3 萬張)
    if fi_today <= -8000:
        reasons.append(f"外資單日賣{abs(fi_today):,}張")

    # 3) 連續賣 + 投信跟賣
    if fi_consec <= -4 and it_5d <= -200:
        reasons.append(f"外資連{abs(fi_consec)}天賣+投信也賣")

    # 4) 融資爆增 (散戶接刀)
    if margin_30d >= 25:
        reasons.append(f"融資+{margin_30d:.0f}%散戶接刀")

    # 5) 中期破位
    if pct_20d <= -10:
        reasons.append(f"20日跌{pct_20d:.1f}%破位")

    return (len(reasons) > 0, " · ".join(reasons[:3]))


# ---------------------------------------------------------------------------
# 批次查多檔
# ---------------------------------------------------------------------------
def fetch_messy_map(stock_ids: List[str], max_workers: int = 5) -> Dict[str, Tuple[bool, str]]:
    """批次抓籌碼資料 + 判斷, 回傳 {sid: (is_messy, reason)}.
    抓不到資料的視為「不亂」(False, ""), 不會誤刪 (保守原則).
    """
    if not stock_ids:
        return {}

    results: Dict[str, Tuple[bool, str]] = {}

    def _check(sid: str) -> Tuple[str, Tuple[bool, str]]:
        try:
            chip = chip_analyzer.fetch_chip_data(sid, days=10)
            return sid, is_chip_messy(chip)
        except Exception:
            return sid, (False, "")

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_check, sid) for sid in stock_ids]
        for f in as_completed(futures):
            try:
                sid, result = f.result()
                results[sid] = result
            except Exception:
                continue

    for sid in stock_ids:
        results.setdefault(sid, (False, ""))
    return results


# ---------------------------------------------------------------------------
# 過濾 helper
# ---------------------------------------------------------------------------
def filter_out_messy(stock_ids: List[str]) -> Tuple[List[str], Dict[str, str]]:
    """從一份個股清單剔除「籌碼亂」的.
    Returns:
        (clean_list, excluded_with_reasons)
    """
    messy_map = fetch_messy_map(stock_ids)
    clean: List[str] = []
    excluded: Dict[str, str] = {}
    for sid in stock_ids:
        is_messy, reason = messy_map.get(sid, (False, ""))
        if is_messy:
            excluded[sid] = reason
        else:
            clean.append(sid)
    return clean, excluded
