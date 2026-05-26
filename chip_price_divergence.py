"""
chip_price_divergence.py
盤後籌碼-價量交叉分析 — 識別專業分析師關注的 12 種訊號模式.

為什麼需要這個:
  單看「外資買賣超」或「股價漲跌」會錯失關鍵訊號. 真正有意義的是「兩者的組合」.
  例如: 外資大買 + 股價大漲 = 健康強勢 (Bullish confirmation)
       外資大賣 + 股價大漲 = 危險訊號 (主力出貨給散戶)
       外資大買 + 股價大跌 = 潛在反轉 (外資逢低布局)

12 種專業模式 (按 signal_strength 排序):

【極強看好 (strong_bullish)】
  1. 三大法人共識買 + 放量上漲           — 主力共同進場, 量價齊揚
  2. 外資大買 + 投信跟買 + 收紅           — 法人共識看好

【看好 (bullish)】
  3. 外資轉買 (由賣轉買) + 由跌轉漲       — 反轉訊號
  4. 外資大買 + 股價跌                    — 外資逢低布局 (中期看好, 短線壓力)
  5. 放量上漲 + 法人不賣                  — 散戶有量進場, 法人不阻擋

【中性偏多 (neutral_bullish)】
  6. 投信大買 + 外資小賣                  — 內外資意見分歧, 投信為主導

【警示 (warning) - 用戶問的這類】
  7. 外資大賣 + 股價大漲                  — ⚠️ 主力或散戶拉抬, 外資反向減碼 (假突破/出貨前兆)
  8. 投信大賣 + 外資也賣 + 股價漲         — ⚠️ 散戶接刀, 主力共識出貨

【看壞 (bearish)】
  9. 放量下跌 + 外資大賣                  — 確認出貨
 10. 三大法人都賣 + 股價跌               — 共識看空

【中性偏空 (neutral_bearish)】
 11. 量縮上漲 + 法人不買                 — 投機性反彈, 不具持續性

【中性等待 (neutral_waiting)】
 12. 量縮整理 + 法人小買小賣             — 籌碼沉澱期, 等待方向

對外接口:
    analyze_stock(stock_id) -> Dict
        # 回傳 {pattern, strength, reason, recommendation, raw_data}

    analyze_batch(stock_ids) -> Dict[str, Dict]
"""
from __future__ import annotations

from typing import Dict, List, Optional

import chip_analyzer


# 訊號強度等級
SIGNAL_STRENGTH = {
    "strong_bullish":  ("🟢🟢", "極強看好", "可考慮加碼 / 進場"),
    "bullish":         ("🟢", "看好", "可考慮分批進場"),
    "neutral_bullish": ("🟡↑", "中性偏多", "觀察, 等更強訊號"),
    "warning":         ("⚠️", "警示", "持股可考慮減碼, 不該進場"),
    "bearish":         ("🔴", "看壞", "考慮減碼 / 出清"),
    "neutral_bearish": ("🟡↓", "中性偏空", "觀察, 不該加碼"),
    "neutral_waiting": ("⚪", "中性等待", "等待方向確認"),
}


def analyze_stock(stock_id: str, chip_data: Optional[Dict] = None) -> Dict:
    """分析單檔股票的籌碼-價量模式.

    chip_data: 若已有 (從 chip_analyzer.fetch_chip_data) 可直接傳, 省 API.

    回傳:
        {
            stock_id, pattern, strength, strength_label, emoji,
            reason, recommendation, signals (list), raw
        }
    """
    if chip_data is None:
        chip_data = chip_analyzer.fetch_chip_data(stock_id, days=10) or {}

    inst = (chip_data or {}).get("institutional", {}) or {}
    price = (chip_data or {}).get("price", {}) or {}

    fi = inst.get("Foreign_Investor", {}) or {}
    it = inst.get("Investment_Trust", {}) or {}
    dlr = inst.get("Dealer_self", {}) or {}

    # 提取關鍵數字
    fi_today = fi.get("today", 0) or 0
    fi_5d = fi.get("5d_total", 0) or 0
    fi_consec = fi.get("consecutive_days", 0) or 0
    it_today = it.get("today", 0) or 0
    it_5d = it.get("5d_total", 0) or 0
    dlr_today = dlr.get("today", 0) or 0

    today_pct = price.get("今日%", 0) or 0
    five_pct = price.get("5d漲跌%", 0) or 0
    vol_ratio = price.get("量比", 1) or 1

    # 三大法人合計
    total_today = fi_today + it_today + dlr_today

    # === 規模分級 (台股慣例) ===
    # 大: > 2000 張, 中: 500-2000, 小: < 500
    def _scale(n: int) -> str:
        n = abs(n)
        if n >= 2000:
            return "大"
        elif n >= 500:
            return "中"
        else:
            return "小"

    fi_scale = _scale(fi_today)
    it_scale = _scale(it_today)

    # === 12 模式偵測 ===
    signals: List[str] = []
    pattern = None
    strength = "neutral_waiting"
    reason = ""
    recommendation = ""

    # 模式 1: 三大法人共識買 + 放量上漲
    if (fi_today > 1000 and it_today > 500 and dlr_today > 0
        and today_pct > 1 and vol_ratio > 1.5):
        pattern = "三大法人共識買 + 放量上漲"
        strength = "strong_bullish"
        reason = (f"外資 +{fi_today:,} / 投信 +{it_today:,} / 自營 +{dlr_today:,} "
                  f"全部買超 + 股價 +{today_pct:.2f}% + 量比 {vol_ratio:.1f}x")
        recommendation = "量價齊揚, 法人共識進場, 可考慮短線進場或加碼"
        signals.append("✓ 法人三方共識買")
        signals.append("✓ 放量上漲確認")

    # 模式 2: 外資大買 + 投信跟買 + 收紅
    elif fi_today > 1500 and it_today > 300 and today_pct > 0.5:
        pattern = "外資大買 + 投信跟買"
        strength = "strong_bullish"
        reason = (f"外資 +{fi_today:,} 張 + 投信 +{it_today:,} 張 + 股價 +{today_pct:.2f}%, "
                  "雙法人共識看好")
        recommendation = "雙法人共識買進, 可考慮跟單"
        signals.append("✓ 外資大買")
        signals.append("✓ 投信跟買")

    # 模式 7: 外資大賣 + 股價大漲 ← 用戶問的這種!
    elif fi_today < -1500 and today_pct > 2:
        pattern = "⚠️ 外資大賣但股價大漲"
        strength = "warning"
        reason = (f"外資 {fi_today:,} 張賣超, 但股價卻 +{today_pct:.2f}%. "
                  "通常是主力 / 散戶逼軋空 OR 主力拉高出貨給散戶 (假突破風險)")
        recommendation = ("🚨 持股的人考慮減碼, 短線當沖獲利了結; "
                          "千萬不要追進. 若隔日跌破今日低點, 確認出貨")
        signals.append("⚠ 外資逆向減碼")
        signals.append("⚠ 量大但籌碼方向相反 = 警示")

    # 模式 8: 法人共識賣 + 股價漲 (散戶接刀)
    elif (fi_today < -500 and it_today < 0 and today_pct > 1):
        pattern = "⚠️ 三法人都賣但股價漲 (散戶接刀)"
        strength = "warning"
        reason = (f"外資 {fi_today:,} / 投信 {it_today:,} 都賣超, "
                  f"但股價 +{today_pct:.2f}% — 籌碼從主力流向散戶")
        recommendation = "🚨 典型出貨末段, 不該進場, 持股建議減碼"
        signals.append("⚠ 法人共識賣")
        signals.append("⚠ 散戶接刀")

    # 模式 3: 外資轉買 + 反轉訊號
    elif (fi_today > 800 and fi_5d < 0 and today_pct > 1
          and five_pct < 0):
        pattern = "外資轉買 + 反轉訊號"
        strength = "bullish"
        reason = (f"外資 5 日累計 {fi_5d:,} 張為負但今日 +{fi_today:,} 張轉買, "
                  f"股價今日 +{today_pct:.2f}% (5日仍 {five_pct:.1f}%) — 反轉跡象")
        recommendation = "可考慮觀察 1-2 天確認, 站穩支撐則進場"
        signals.append("✓ 外資由賣轉買")
        signals.append("✓ 股價由跌轉漲")

    # 模式 4: 外資大買 + 股價跌 (逢低布局)
    elif fi_today > 2000 and today_pct < -0.5:
        pattern = "外資大買 + 股價跌"
        strength = "bullish"
        reason = (f"外資 +{fi_today:,} 張大買, 但股價 {today_pct:.2f}%, "
                  "屬於外資逢低布局, 主力收貨")
        recommendation = "中期看好但短線可能還有壓力, 可分批佈局"
        signals.append("✓ 外資逢低布局")
        signals.append("△ 短線有壓力")

    # 模式 9: 放量下跌 + 外資大賣 (出貨確認)
    elif vol_ratio > 1.5 and today_pct < -1.5 and fi_today < -1000:
        pattern = "放量下跌 + 外資出貨"
        strength = "bearish"
        reason = (f"量比 {vol_ratio:.1f}x 放量 + 股價 {today_pct:.2f}% + "
                  f"外資 {fi_today:,} 張賣超 — 出貨確認")
        recommendation = "持股建議出清, 不該逢低承接"
        signals.append("⚠ 放量下跌")
        signals.append("⚠ 外資確認出貨")

    # 模式 10: 三大法人都賣 + 股價跌
    elif fi_today < -500 and it_today < 0 and today_pct < -0.5:
        pattern = "三法人都賣 + 股價跌"
        strength = "bearish"
        reason = (f"外資 {fi_today:,} / 投信 {it_today:,} 共識看空 + "
                  f"股價 {today_pct:.2f}%")
        recommendation = "順勢操作, 持股考慮減碼或停損"
        signals.append("⚠ 法人共識賣")
        signals.append("⚠ 股價配合下跌")

    # 模式 6: 投信大買 + 外資小賣
    elif it_today > 500 and -500 < fi_today < 0:
        pattern = "投信大買 + 外資小幅減碼"
        strength = "neutral_bullish"
        reason = (f"投信 +{it_today:,} 主導 + 外資 {fi_today:,} 小減 — "
                  "意見分歧, 投信為主導 (投信通常看 3-6 個月)")
        recommendation = "投信主導通常中線看好, 可少量參與"
        signals.append("○ 投信主動")
        signals.append("○ 外資觀望")

    # 模式 5: 放量上漲 + 法人不賣
    elif vol_ratio > 1.5 and today_pct > 1.5 and fi_today >= -200:
        pattern = "放量上漲 + 法人不阻擋"
        strength = "bullish"
        reason = (f"量比 {vol_ratio:.1f}x + 股價 +{today_pct:.2f}% + "
                  "法人未大舉賣超, 散戶有量進場")
        recommendation = "可考慮跟進, 但要設好停損 (主動性買盤主導)"
        signals.append("✓ 散戶有量進場")
        signals.append("✓ 法人不阻擋")

    # 模式 11: 量縮上漲 + 法人不買 (投機性反彈)
    elif vol_ratio < 0.8 and today_pct > 1 and fi_today <= 0 and it_today <= 0:
        pattern = "量縮上漲 + 法人不買"
        strength = "neutral_bearish"
        reason = (f"量比 {vol_ratio:.1f}x 量縮 + 股價 +{today_pct:.2f}% + "
                  "法人未進場 — 投機性反彈, 持續性可疑")
        recommendation = "不該追進, 持股若有獲利可考慮獲利了結"
        signals.append("△ 量縮上漲")
        signals.append("△ 法人冷眼旁觀")

    # 模式 12: 量縮整理 + 法人小買小賣 (中性等待)
    else:
        pattern = "量縮整理 / 訊號中性"
        strength = "neutral_waiting"
        reason = (f"外資 {fi_today:+,} / 投信 {it_today:+,} / 股價 {today_pct:+.2f}% / "
                  f"量比 {vol_ratio:.1f}x — 沒有明確主導訊號")
        recommendation = "等待方向確認, 不建議重押"

    emoji, strength_label, default_rec = SIGNAL_STRENGTH.get(
        strength, ("", "", "")
    )

    return {
        "stock_id": stock_id,
        "pattern": pattern,
        "strength": strength,
        "strength_label": strength_label,
        "emoji": emoji,
        "reason": reason,
        "recommendation": recommendation,
        "signals": signals,
        "raw": {
            "fi_today": fi_today, "fi_5d": fi_5d, "fi_consec": fi_consec,
            "it_today": it_today, "it_5d": it_5d, "dlr_today": dlr_today,
            "today_pct": today_pct, "five_pct": five_pct, "vol_ratio": vol_ratio,
            "fi_scale": fi_scale, "it_scale": it_scale,
        },
    }


def analyze_batch(stock_ids: List[str]) -> Dict[str, Dict]:
    """批次分析多檔. 共用 chip_analyzer 的 thread-safe cache."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(analyze_stock, sid): sid for sid in stock_ids}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                out[sid] = fut.result()
            except Exception as e:
                out[sid] = {"stock_id": sid, "error": str(e)}
    return out


def filter_by_strength(results: Dict[str, Dict],
                        strengths: List[str]) -> Dict[str, Dict]:
    """從 analyze_batch 的結果中, 只留特定 strength 的 (e.g., 只看 warning 訊號)."""
    return {sid: r for sid, r in results.items()
            if r.get("strength") in strengths}


def fmt_summary_tg(results: Dict[str, Dict],
                    only_strong: bool = True) -> str:
    """格式化為 TG 訊息. only_strong=True 只顯示 strong_bullish / warning / bearish.

    如 analyze_batch 結果含 100 檔, 預設只篩出值得注意的 ~10-20 檔.
    """
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)

    if only_strong:
        results = filter_by_strength(results, ["strong_bullish", "warning", "bearish"])

    if not results:
        return "📊 <b>盤後籌碼價量分析</b>\n\n無符合篩選條件的標的 (今日所有股訊號中性)"

    # 按 strength 分組排序
    grouped = {"strong_bullish": [], "bullish": [], "warning": [], "bearish": [],
                "neutral_bullish": [], "neutral_bearish": [], "neutral_waiting": []}
    for sid, r in results.items():
        s = r.get("strength", "neutral_waiting")
        grouped.setdefault(s, []).append(r)

    lines = ["📊 <b>盤後籌碼價量分析</b>", ""]
    for s_key in ["strong_bullish", "bullish", "warning", "bearish"]:
        items = grouped.get(s_key) or []
        if not items:
            continue
        emoji, label, _ = SIGNAL_STRENGTH[s_key]
        lines.append(f"<b>{emoji} {label} ({len(items)} 檔)</b>")
        for r in items[:5]:
            sid = _esc(r.get("stock_id"))
            pat = _esc(r.get("pattern", ""))
            tp = r.get("raw", {}).get("today_pct", 0)
            lines.append(f"  • <code>{sid}</code> {pat} ({tp:+.2f}%)")
            rec = r.get("recommendation", "")
            if rec:
                lines.append(f"    💡 {_esc(rec[:90])}")
        lines.append("")
    return "\n".join(lines).rstrip()
