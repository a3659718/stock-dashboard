"""
chip_filter.py
籌碼面過濾器 — 找出「籌碼亂掉、外資撤離、散戶接刀」的股票, 從買進名單剔除.

B5 修正 (2026-05): 把固定張數閾值改為相對 30 日均量百分比,
  避免「大型股太鬆 / 小型股太嚴」的問題.

新判斷標準 (任一觸發就算亂):
  1. 外資 5 日累計賣超 >= 0.5 × 30日均量 (= 平均每天賣 10% 均量)
  2. 外資單日大賣 >= 0.2 × 30日均量 (= 一天賣 20% 均量)
  3. 外資連續賣 >= 4 天 + 投信 5 日也淨賣超 (法人共識看空)
  4. 融資 30 日 +25% 以上 (散戶接刀, 籌碼從主力流向散戶)
  5. 過去 20 日跌幅 <= -10% (中期已破位)

使用方式:
    from chip_filter import filter_out_messy
    clean_ids, excluded = filter_out_messy(["2330", "1802", ...])
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple

import chip_analyzer


# ---------------------------------------------------------------------------
# B5: 用 30 日均量決定動態閾值
# ---------------------------------------------------------------------------
# 預設 fallback 閾值 (當 price.20日均量 missing 時使用, 與舊版相容)
_FALLBACK_FI_5D = 4000
_FALLBACK_FI_TODAY = 8000

# 相對閾值: 外資 5 日累計賣超對 30 日均量的比例
_REL_FI_5D_RATIO = 0.5    # 5 日累計賣 >= 50% × 30日均量
_REL_FI_TODAY_RATIO = 0.20  # 單日賣 >= 20% × 30日均量


# ---------------------------------------------------------------------------
# 單檔判斷
# ---------------------------------------------------------------------------
def is_chip_messy(chip_data: Dict) -> Tuple[bool, str]:
    """判斷單檔籌碼是否亂掉. B5: 閾值依股本大小動態調整.

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

    # L3 修正: 用 dict.get() 預設 None, 後續用 `is not None` 判斷,
    # 區分「真的無變化 (0)」與「資料缺失 (None)」.
    margin_30d = margin.get("融資30日變化%")
    pct_20d = price.get("20d漲跌%", 0) or 0

    # H3 修正: 優先用 chip_analyzer 明確 export 的「20日均量_張」(已換算為張),
    # fallback 才用「20日均量」(股) 自己除 1000.
    avg20_lots = price.get("20日均量_張")
    if not avg20_lots:
        avg20_vol_shares = price.get("20日均量", 0) or 0
        avg20_lots = avg20_vol_shares / 1000.0 if avg20_vol_shares > 0 else 0

    # B5: 動態閾值 — 用 30 日均量算出真正大賣標準
    if avg20_lots > 0:
        threshold_5d = max(500, avg20_lots * _REL_FI_5D_RATIO)  # 至少 500 張 floor
        threshold_today = max(800, avg20_lots * _REL_FI_TODAY_RATIO)
    else:
        # Fallback (與舊版相容)
        threshold_5d = _FALLBACK_FI_5D
        threshold_today = _FALLBACK_FI_TODAY

    reasons: List[str] = []

    # 1) 外資 5 日累計大賣 (相對標準)
    if fi_5d <= -threshold_5d:
        pct_of_avg = abs(fi_5d) / avg20_lots * 100 if avg20_lots > 0 else None
        if pct_of_avg:
            reasons.append(f"外資5日賣{abs(fi_5d):,}張 (~{pct_of_avg:.0f}% 均量)")
        else:
            reasons.append(f"外資5日賣{abs(fi_5d):,}張")

    # 2) 外資單日大賣 (相對標準)
    if fi_today <= -threshold_today:
        pct_today = abs(fi_today) / avg20_lots * 100 if avg20_lots > 0 else None
        if pct_today:
            reasons.append(f"外資單日賣{abs(fi_today):,}張 (~{pct_today:.0f}% 均量)")
        else:
            reasons.append(f"外資單日賣{abs(fi_today):,}張")

    # 3) 連續賣 + 投信跟賣
    if fi_consec <= -4 and it_5d <= -200:
        reasons.append(f"外資連{abs(fi_consec)}天賣+投信也賣")

    # 4) 融資爆增 (散戶接刀) — L3: 用 is not None 區分 0 與未知
    if margin_30d is not None and margin_30d >= 25:
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
