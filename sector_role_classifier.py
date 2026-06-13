"""
sector_role_classifier.py — 強族選股: 龍頭 / 跟風 / 候補 分類

問題:
  - 強族訊號把整個族群裡所有股票一起推, 但**只有龍頭會大漲**, 跟風漲不久, 候補才有機會
  - 現在用戶看到「強族 + 5 檔股票」, 不知道該追哪個

分類規則:
  🏆 龍頭 (leader)    — 漲幅 > 族群均 × 1.5 + 量比 > 2x + RS > 80
                          (帶頭衝, 真正 driver)
  🚶 跟風 (follower)  — 漲幅 ≈ 族群均 + 量比 < 1.5x
                          (沒有獨立題材, 漲幅平庸, 不會持續)
  🕵️ 候補 (laggard)   — 同題材但今日 +0~+1.5% + 量比 ≥ 1.5x + RS > 60
                          (還沒動但有量, 籌碼吸收中, 明日可能補漲)

API:
  classify_stock_role(stock, sector_avg_pct) -> str
  classify_stocks_in_sector(stocks, sector_avg_pct) -> List[Dict]  # 每檔加 sector_role
"""
from __future__ import annotations

from typing import Dict, List, Optional


# 門檻
LEADER_PCT_MULTIPLE = 1.5    # 漲幅至少是族群均 1.5x
LEADER_VOL_RATIO = 2.0       # 量比 ≥ 2x
LEADER_RS = 80               # RS ≥ 80

FOLLOWER_VOL_RATIO = 1.5     # 量比 < 1.5x = 跟風
FOLLOWER_PCT_RANGE = 0.5     # 漲幅在族群均 ±50% 內

LAGGARD_PCT_MAX = 1.5        # 今日漲幅 ≤ 1.5%
LAGGARD_VOL_RATIO = 1.5      # 量比 ≥ 1.5x (有量但沒動)
LAGGARD_RS = 60              # RS ≥ 60 (中等以上強)


def classify_stock_role(stock: Dict, sector_avg_pct: float) -> Dict:
    """單檔分類. 回 dict 含 role + emoji + reason."""
    out = {"role": "—", "emoji": "", "reason": ""}

    tp = float(stock.get("today_pct", 0) or 0)
    vr = float(stock.get("vol_ratio", 0) or 0)
    rs = stock.get("rs")
    if rs is None:
        # 沒有 RS 資料就用簡化規則 (跳過 RS 條件)
        rs = 70  # 默認中性偏強

    # 1. 龍頭 — 漲多 + 量大 + 強過大盤
    if (tp >= max(sector_avg_pct * LEADER_PCT_MULTIPLE, 2.0)
            and vr >= LEADER_VOL_RATIO
            and rs >= LEADER_RS):
        out.update(
            role="leader",
            emoji="🏆",
            reason=f"漲 {tp:+.2f}% (族群均 {sector_avg_pct:+.2f}% 的 {tp/max(0.1,sector_avg_pct):.1f}x) "
                   f"+ 量比 {vr:.2f}x + RS {rs:.0f}",
        )
        return out

    # 2. 候補 — 同族群但還沒動, 籌碼吸收
    if (-0.5 <= tp <= LAGGARD_PCT_MAX
            and vr >= LAGGARD_VOL_RATIO
            and rs >= LAGGARD_RS
            and sector_avg_pct >= 2.0):
        out.update(
            role="laggard",
            emoji="🕵️",
            reason=f"族群已熱 ({sector_avg_pct:+.2f}%) 但個股僅 {tp:+.2f}%, "
                   f"量比 {vr:.2f}x + RS {rs:.0f} (吸籌中, 明日可能補漲)",
        )
        return out

    # 3. 跟風 — 漲幅平庸 + 量沒明顯
    if (sector_avg_pct * 0.5 <= tp <= sector_avg_pct * 1.5
            and vr < FOLLOWER_VOL_RATIO):
        out.update(
            role="follower",
            emoji="🚶",
            reason=f"漲 {tp:+.2f}% 接近族群均 ({sector_avg_pct:+.2f}%) "
                   f"+ 量比 {vr:.2f}x 平庸, 沒獨立題材",
        )
        return out

    # 4. 其他 — 不分類
    if tp >= 2.0 and vr >= 1.3:
        out.update(role="strong", emoji="✅",
                    reason=f"漲 {tp:+.2f}% + 量比 {vr:.2f}x")
    else:
        out.update(role="other", emoji="·",
                    reason=f"漲 {tp:+.2f}% + 量比 {vr:.2f}x")
    return out


def classify_stocks_in_sector(stocks: List[Dict],
                                sector_avg_pct: float) -> List[Dict]:
    """對一族群裡的所有股票分類, 加 sector_role 欄位.

    回: 同 list, 每檔加 sector_role / sector_role_emoji / sector_role_reason

    排序: 龍頭 > 候補 > 強 > 跟風 > 其他
    """
    role_order = {"leader": 0, "laggard": 1, "strong": 2, "follower": 3, "other": 4, "—": 5}
    for s in stocks:
        c = classify_stock_role(s, sector_avg_pct)
        s["sector_role"] = c["role"]
        s["sector_role_emoji"] = c["emoji"]
        s["sector_role_reason"] = c["reason"]
    return sorted(stocks, key=lambda x: role_order.get(x.get("sector_role", "—"), 5))


def summarize_sector_roles(stocks: List[Dict]) -> Dict:
    """統計一族群裡有多少龍頭/跟風/候補.

    回: {leader_n, follower_n, laggard_n, strong_n}
    """
    out = {"leader_n": 0, "follower_n": 0, "laggard_n": 0, "strong_n": 0}
    for s in stocks:
        r = s.get("sector_role", "")
        if r == "leader": out["leader_n"] += 1
        elif r == "follower": out["follower_n"] += 1
        elif r == "laggard": out["laggard_n"] += 1
        elif r == "strong": out["strong_n"] += 1
    return out
