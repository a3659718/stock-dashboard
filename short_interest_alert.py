"""
short_interest_alert.py
台股「大戶空單」籌碼 — 借券餘額 / 借券賣出 / 融券餘額 / 券資比.

法人/大戶準備放空的彈藥指標 (台股最關鍵的空單訊號):
  - 借券餘額 (TaiwanStockSecuritiesLending) — 法人手上的「可賣空子彈」
  - 借券賣出 — 已實際下單的法人空單
  - 融券餘額 (TaiwanStockMarginPurchaseShortSale) — 散戶+部分自營商空單
  - 券資比 = 融券/融資 — 多空氣氛分歧度, ≥30% 軋空潛力

異常推播觸發:
  - 借券賣出單日增加 ≥ 5 億張 → "🔴 法人空單暴增"
  - 券資比 ≥ 35% → "🟢 軋空潛力 (散戶空單過重)"
  - 借券餘額 7 日連續增 → "🔴 法人準備倒貨"

API:
  fetch_short_interest_snapshot() -> Dict  # 整市場合計
  format_short_for_tg(snap) -> str  # 給 pre_market 用
  check_anomaly_for_alert() -> List[Dict]  # 給 monitor 用
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import institutional_positioning as ip  # reuse _safe_finmind_data


def _fetch_securities_lending() -> Dict:
    """整市場借券餘額 + 借券賣出 (最近 5-7 個交易日)."""
    out = {"data": [], "summary": "", "anomaly": None}
    rows = ip._safe_finmind_data("TaiwanStockSecuritiesLending", days=10)
    if not rows:
        return out

    # 整市場合計: 各日 sum
    by_date = {}
    for r in rows:
        d = r.get("date", "")
        if not d:
            continue
        # 不同 FinMind 版本欄位名可能不同
        lend_balance = float(r.get("balance", 0) or r.get("securities_lending_balance", 0) or 0)
        lend_short_sell = float(r.get("short_sale_balance", 0) or r.get("securities_lending_short_sale", 0) or 0)
        if d not in by_date:
            by_date[d] = {"lend_balance": 0, "lend_short_sell": 0}
        by_date[d]["lend_balance"] += lend_balance
        by_date[d]["lend_short_sell"] += lend_short_sell

    dates = sorted(by_date.keys())[-7:]
    data = []
    for d in dates:
        rec = by_date[d]
        data.append({
            "date": d,
            "lend_balance_yi": round(rec["lend_balance"] / 1e8, 2),  # 元 → 億
            "lend_short_sell_yi": round(rec["lend_short_sell"] / 1e8, 2),
        })
    out["data"] = data

    if not data:
        return out
    latest = data[-1]
    out["summary"] = (
        f"借券餘額 {latest['lend_balance_yi']:.0f} 億 | "
        f"借券賣出 {latest['lend_short_sell_yi']:.0f} 億"
    )

    # 異常判定
    if len(data) >= 2:
        prev = data[-2]
        delta_lend = latest["lend_balance_yi"] - prev["lend_balance_yi"]
        delta_short = latest["lend_short_sell_yi"] - prev["lend_short_sell_yi"]

        if delta_short >= 50:  # 借券賣出單日增 50 億
            out["anomaly"] = {
                "type": "lend_short_surge",
                "msg": f"🔴 借券賣出單日 +{delta_short:.0f} 億, 法人空單暴增",
                "severity": "high",
            }
        elif delta_lend >= 100:  # 借券餘額單日增 100 億 (準備子彈)
            out["anomaly"] = {
                "type": "lend_balance_surge",
                "msg": f"🟡 借券餘額單日 +{delta_lend:.0f} 億, 法人準備子彈",
                "severity": "medium",
            }

        # 7 日連續增
        if len(data) >= 5:
            streak_inc = all(
                data[i]["lend_short_sell_yi"] > data[i-1]["lend_short_sell_yi"]
                for i in range(1, len(data))
            )
            if streak_inc:
                out["anomaly"] = {
                    "type": "lend_short_streak",
                    "msg": f"🔴 借券賣出連 {len(data)} 日增, 法人持續加空",
                    "severity": "high",
                }
    return out


def _fetch_margin_short() -> Dict:
    """整市場融資/融券餘額 + 券資比."""
    out = {"margin_yi": None, "short_yi": None, "ratio_pct": None, "signal": ""}
    rows = ip._safe_finmind_data("TaiwanStockTotalMarginPurchaseShortSale", days=5)
    if not rows:
        return out

    latest_date = max(r.get("date", "") for r in rows)
    today = [r for r in rows if r.get("date") == latest_date]
    if not today:
        return out
    r = today[0]
    margin_balance = float(r.get("margin_purchase_today_balance", 0) or 0)
    short_balance = float(r.get("short_sale_today_balance", 0) or 0)
    # 不同單位: 有些是「張」(千股), 有些是金額. 用金額轉億
    # 若數字 < 10000 視為「億」, 否則轉億
    if margin_balance > 1e6:  # 元
        margin_yi = margin_balance / 1e8
        short_yi = short_balance / 1e8
    else:
        margin_yi = margin_balance / 100  # 萬 → 億
        short_yi = short_balance / 100

    out["margin_yi"] = round(margin_yi, 2)
    out["short_yi"] = round(short_yi, 2)
    if margin_yi > 0:
        ratio = (short_yi / margin_yi) * 100
        out["ratio_pct"] = round(ratio, 2)
        if ratio >= 35:
            out["signal"] = "🟢 券資比 ≥35% — 軋空潛力 (散戶空單過重, 反向偏多)"
        elif ratio >= 25:
            out["signal"] = "🟡 券資比偏高 — 多空分歧, 留意震盪"
        elif ratio <= 8:
            out["signal"] = "🔴 券資比 <8% — 散戶過度樂觀, 反向偏空"
        else:
            out["signal"] = "⚪ 券資比正常"
    return out


def fetch_short_interest_snapshot() -> Dict:
    """整合所有空單指標."""
    snap = {
        "lending": _fetch_securities_lending(),
        "margin_short": _fetch_margin_short(),
        "fetched_at": dt.datetime.utcnow().isoformat(),
    }
    # 綜合 verdict
    verdict_score = 0  # 正 = 偏多 (反指標), 負 = 偏空 (大戶準備倒貨)
    lend = snap["lending"]
    ms = snap["margin_short"]

    if lend.get("anomaly"):
        sev = lend["anomaly"].get("severity", "")
        if sev == "high":
            verdict_score -= 2
        elif sev == "medium":
            verdict_score -= 1

    if ms.get("ratio_pct"):
        r = ms["ratio_pct"]
        if r >= 35:
            verdict_score += 2  # 反向偏多
        elif r <= 8:
            verdict_score -= 1  # 散戶樂觀 → 反向偏空

    if verdict_score >= 2:
        snap["verdict"] = "🟢 大戶空單偏多訊號 (反指標)"
    elif verdict_score >= 1:
        snap["verdict"] = "🟢 大戶空單微偏多"
    elif verdict_score <= -2:
        snap["verdict"] = "🔴 大戶空單偏空訊號 (法人積極倒貨)"
    elif verdict_score <= -1:
        snap["verdict"] = "🔴 大戶空單微偏空"
    else:
        snap["verdict"] = "⚪ 大戶空單中性"

    return snap


def format_short_for_tg(snap: Dict) -> str:
    """格式化給 pre_market / monitor 用. HTML 格式."""
    if not snap:
        return ""
    lend = snap.get("lending", {})
    ms = snap.get("margin_short", {})
    lines = ["🩻 <b>大戶空單監測</b>", f"<i>{snap.get('verdict', '')}</i>"]

    if lend.get("summary"):
        lines.append(f"• 法人空單: {lend['summary']}")
    if lend.get("anomaly"):
        lines.append(f"  └ {lend['anomaly']['msg']}")

    if ms.get("ratio_pct") is not None:
        lines.append(
            f"• 散戶空單: 融券 {ms['short_yi']:.0f} 億 / 融資 {ms['margin_yi']:.0f} 億 "
            f"(券資比 {ms['ratio_pct']:.2f}%)"
        )
        if ms.get("signal"):
            lines.append(f"  └ {ms['signal']}")

    if not (lend.get("summary") or ms.get("ratio_pct")):
        return ""  # 沒抓到資料就 skip
    return "\n".join(lines)


def summarize_for_gemini(snap: Dict) -> str:
    """精簡版 (給 Gemini prompt)."""
    if not snap:
        return ""
    parts = []
    lend = snap.get("lending", {})
    ms = snap.get("margin_short", {})
    if lend.get("summary"):
        parts.append(lend["summary"])
    if lend.get("anomaly"):
        parts.append(lend["anomaly"]["msg"].replace("🔴 ", "").replace("🟡 ", ""))
    if ms.get("ratio_pct") is not None:
        parts.append(f"券資比 {ms['ratio_pct']:.1f}%")
    if snap.get("verdict"):
        parts.append(snap["verdict"].replace("🟢 ", "").replace("🔴 ", "").replace("⚪ ", ""))
    return " | ".join(parts)
