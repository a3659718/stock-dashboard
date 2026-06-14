"""
ipo_calendar_alert.py — 今日新股上市推播 (含 Gemini 合理價分析)

只推「今日上市」的新股, 不推申購提醒 (用戶不抽籤, 直接看盤中要不要追).

對每檔做:
  1. 同業 PE 估合理價區間
  2. Gemini 分析: 公司本業 / 競爭力 / 合理價 / 進場建議
  3. 首日操作策略 (進場價 / 停損 / 目標)

API:
  check_today_listing() -> List[Dict]  # 今天上市的
  build_today_listing_msg() -> str
  check_and_push() -> Dict
"""
from __future__ import annotations

import datetime as dt
from datetime import timezone
from typing import Dict, List, Optional


HOT_THEME_KEYWORDS = {
    "AI / 半導體": ["ai", "人工智慧", "晶片", "半導體", "ic 設計", "ic設計", "asic",
                       "edge ai", "hpc", "高效能運算", "gpu"],
    "AI 伺服器": ["server", "伺服器", "資料中心", "data center", "散熱", "電源"],
    "重電 / 核電": ["重電", "核電", "smr", "電纜", "變壓器", "綠能", "再生能源"],
    "機器人": ["機器人", "robot", "humanoid", "自動化"],
    "衛星 / 太空": ["衛星", "satellite", "太空", "low earth orbit", "leo"],
    "電動車": ["電動車", "ev", "充電", "電池", "鋰電池"],
    "生技 / 醫材": ["生技", "biotech", "醫材", "醫藥", "新藥", "醫療"],
    "ABF 載板": ["abf", "載板", "ic 載板", "ic載板"],
    "光通訊 / 連接器": ["光通訊", "光收發", "連接器", "高速傳輸", "cpo"],
    "量子計算": ["量子", "quantum"],
}


def _safe_finmind_data(dataset: str, days: int = 7) -> Optional[List[Dict]]:
    try:
        import os
        token = os.getenv("FINMIND_TOKEN") or ""
        if not token:
            try:
                import streamlit as st
                token = st.secrets.get("FINMIND_TOKEN", "")  # type: ignore
            except Exception:
                pass
        if not token:
            return None
        import requests
        today = dt.date.today()
        end = (today + dt.timedelta(days=days)).strftime("%Y-%m-%d")
        start = (today - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": dataset,
            "start_date": start,
            "end_date": end,
            "token": token,
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        js = r.json()
        if js.get("status") != 200:
            return None
        return js.get("data") or []
    except Exception as e:
        print(f"[ipo] finmind {dataset} fail: {e}", flush=True)
        return None


def check_today_listing() -> List[Dict]:
    """抓今天上市的新股."""
    rows = _safe_finmind_data("TaiwanIPONewStock", days=3) \
            or _safe_finmind_data("TaiwanStockNewStock", days=3)
    if not rows:
        return []
    today = dt.date.today()
    today_str = today.strftime("%Y-%m-%d")
    out = []
    for r in rows:
        list_date = r.get("listing_date") or r.get("list_date") or ""
        if str(list_date)[:10] != today_str:
            continue
        out.append({
            "stock_id": str(r.get("stock_id") or r.get("stock_no", "")),
            "stock_name": r.get("stock_name", ""),
            "industry": r.get("industry_category") or r.get("industry", ""),
            "offer_price": float(r.get("offer_price", 0) or 0),
            "list_date": str(list_date)[:10],
            "business_description": r.get("business_description") or r.get("description", ""),
        })
    return out


def check_next_week_listings() -> List[Dict]:
    """抓下週 (週一到週日) 要上市的新股. 用於週五晚預告."""
    rows = _safe_finmind_data("TaiwanIPONewStock", days=14) \
            or _safe_finmind_data("TaiwanStockNewStock", days=14)
    if not rows:
        return []
    today = dt.date.today()
    # 計算下週一 & 下週日
    # weekday: Mon=0, Sun=6. 今天若週五 (4), next_monday = today + (7-4) = today+3
    days_to_next_monday = (7 - today.weekday()) % 7
    if days_to_next_monday == 0:
        days_to_next_monday = 7
    next_monday = today + dt.timedelta(days=days_to_next_monday)
    next_sunday = next_monday + dt.timedelta(days=6)

    out = []
    for r in rows:
        list_date = r.get("listing_date") or r.get("list_date") or ""
        try:
            ld = dt.datetime.strptime(str(list_date)[:10], "%Y-%m-%d").date()
            if not (next_monday <= ld <= next_sunday):
                continue
        except Exception:
            continue
        out.append({
            "stock_id": str(r.get("stock_id") or r.get("stock_no", "")),
            "stock_name": r.get("stock_name", ""),
            "industry": r.get("industry_category") or r.get("industry", ""),
            "offer_price": float(r.get("offer_price", 0) or 0),
            "list_date": str(list_date)[:10],
            "business_description": r.get("business_description") or r.get("description", ""),
        })
    # 按上市日排序
    out.sort(key=lambda x: x["list_date"])
    return out


def _classify_theme(description: str, stock_name: str = "", industry: str = "") -> List[str]:
    if not description and not stock_name and not industry:
        return []
    text = f"{description} {stock_name} {industry}".lower()
    matched = []
    for theme, keywords in HOT_THEME_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                matched.append(theme)
                break
    return matched[:3]


def _estimate_industry_pe(industry: str) -> Optional[float]:
    INDUSTRY_PE_MAP = {
        "半導體業": 18, "電腦及週邊設備業": 16, "電子零組件業": 15,
        "通信網路業": 18, "光電業": 14,
        "金融保險業": 10, "其他金融業": 8,
        "生技醫療業": 30, "化學工業": 14,
        "鋼鐵工業": 10, "塑膠工業": 12,
        "電機機械": 16, "電器電纜": 14,
        "貿易百貨": 18, "觀光事業": 25,
        "建材營造": 10, "汽車工業": 12,
        "食品工業": 18,
        "公用事業": 14, "油電燃氣業": 12,
    }
    for key, pe in INDUSTRY_PE_MAP.items():
        if key in (industry or ""):
            return pe
    return 18


def _gemini_analyze_ipo(ipo: Dict) -> Dict:
    """Gemini 分析 — 合理價 / 競爭力 / 進場建議.

    Fallback: Gemini 不可用就用 rule-based 簡化版.
    """
    out = {"reasonable_price_low": None, "reasonable_price_high": None,
           "competitive": "", "entry_action": "", "raw": ""}

    sid = ipo.get("stock_id", "")
    name = ipo.get("stock_name", "")
    industry = ipo.get("industry", "")
    offer = float(ipo.get("offer_price", 0) or 0)
    desc = ipo.get("business_description", "") or "(未提供)"
    industry_pe = _estimate_industry_pe(industry) or 18
    themes = _classify_theme(desc, name, industry)

    # Try Gemini
    try:
        import ai_analyzer
        if ai_analyzer.gemini_available():
            from ai_analyzer import _get_model
            model = _get_model()
            if model:
                prompt = (
                    "你是台股新股分析師. 以下新股今日上市, 請給 JSON 分析 (繁中, 不要其他字):\n\n"
                    f"代號: {sid}\n股名: {name}\n產業: {industry}\n"
                    f"承銷價: {offer:.2f} 元\n業務: {desc[:300]}\n"
                    f"同業平均 PE: {industry_pe}\n題材: {' / '.join(themes) if themes else '無明顯'}\n\n"
                    "JSON 格式:\n"
                    "{\n"
                    '  "reasonable_price_low": <number 合理價低點>,\n'
                    '  "reasonable_price_high": <number 合理價高點>,\n'
                    '  "competitive": "<1 句競爭力評估: 護城河 / 客戶 / 技術門檻>",\n'
                    '  "entry_action": "<具體進場建議: 開盤 +X% 內可追 / 拉回承銷價 +Y% 接 / 跳過>"\n'
                    "}\n"
                    "判斷準則:\n"
                    "- 合理價: 承銷價 ± 30% 區間 (估出來 < 承銷價 = 估值貴, > 承銷價 = 估值便宜)\n"
                    "- 競爭力: 看公司是否有獨家技術 / 大客戶 / 進入門檻\n"
                    "- entry_action 要具體, 給數字, 不要說「視情況」"
                )
                resp = model.generate_content(prompt)
                text = (resp.text or "").strip() if resp else ""
                import re as _re, json as _json
                m = _re.search(r"\{[\s\S]*\}", text)
                if m:
                    try:
                        parsed = _json.loads(m.group(0))
                        out.update({
                            "reasonable_price_low": parsed.get("reasonable_price_low"),
                            "reasonable_price_high": parsed.get("reasonable_price_high"),
                            "competitive": parsed.get("competitive", ""),
                            "entry_action": parsed.get("entry_action", ""),
                            "raw": "gemini",
                        })
                        return out
                    except Exception:
                        out["raw"] = text[:500]
    except Exception as e:
        print(f"[ipo] gemini fail: {e}", flush=True)

    # Fallback: 規則式 (Gemini 不可用時)
    # 合理價估計 = 承銷價 ±20-30% (簡化)
    if offer > 0:
        if themes and any(t in ("AI / 半導體", "AI 伺服器", "重電 / 核電", "機器人") for t in themes):
            # 熱門題材 → 合理價偏高
            out["reasonable_price_low"] = round(offer * 1.0, 2)
            out["reasonable_price_high"] = round(offer * 1.5, 2)
            out["competitive"] = f"熱門題材 ({' / '.join(themes)}) — 短線需求強, 長線看公司本業實力"
            out["entry_action"] = (
                f"開盤若 ≤ 承銷價 +30% (≤ {offer*1.3:.1f}) 可小量試單 (≤ 1% 部位), "
                f"停損 -5%, 短目標 +15%"
            )
        else:
            out["reasonable_price_low"] = round(offer * 0.9, 2)
            out["reasonable_price_high"] = round(offer * 1.2, 2)
            out["competitive"] = f"非熱門題材 ({industry or '傳統產業'}) — 評估同業競爭"
            out["entry_action"] = (
                f"開盤觀察 30 分鐘, 若拉回承銷價 +10% 內 (≤ {offer*1.1:.1f}) 才考慮, "
                f"否則跳過"
            )
    out["raw"] = "rule_based"
    return out


def build_today_listing_msg() -> str:
    """今日新股上市推播.

    沒新股上市 → 回空字串 (caller 不會 send)
    """
    listings = check_today_listing()
    if not listings:
        return ""

    lines = ["⭐ <b>今日新股上市</b>"]
    today = dt.date.today().strftime("%m/%d")
    lines.append(f"<i>{today} TPE | {len(listings)} 檔今日掛牌</i>")
    lines.append("")

    for ipo_data in listings[:5]:
        sid = ipo_data.get("stock_id", "")
        name = ipo_data.get("stock_name", "")
        offer = float(ipo_data.get("offer_price", 0) or 0)
        industry = ipo_data.get("industry", "")

        # Gemini 分析
        analysis = _gemini_analyze_ipo(ipo_data)
        rp_low = analysis.get("reasonable_price_low")
        rp_high = analysis.get("reasonable_price_high")
        competitive = analysis.get("competitive", "")
        entry_action = analysis.get("entry_action", "")

        # 主行
        # 主行
        lines.append(f"<code>{sid}</code> <b>{name}</b> ({industry})")
        lines.append(f"  承銷價 <b>{offer:.2f}</b> 元")

        # 合理價區間
        if rp_low and rp_high:
            try:
                low_f = float(rp_low); high_f = float(rp_high)
                premium = (high_f / offer - 1) * 100 if offer > 0 else 0
                lines.append(
                    f"  📊 合理價 <b>{low_f:.1f} - {high_f:.1f}</b> 元 "
                    f"(承銷價 {'偏低' if premium > 30 else '偏高' if premium < 0 else '合理'})"
                )
            except (TypeError, ValueError):
                pass

        # 競爭力
        if competitive:
            lines.append(f"  💼 {competitive}")

        # 進場建議
        if entry_action:
            lines.append(f"  💡 <b>{entry_action}</b>")

        lines.append("")

    lines.append("<i>※ 首日上市波動極大, 嚴守停損 ≤ 部位 5%.</i>")
    lines.append("<i>※ 合理價 = 同業 PE × 估計 EPS (Gemini 分析, 僅供參考).</i>")
    return "\n".join(lines)


def build_next_week_preview_msg() -> str:
    """下週要上市的新股預告 (週五推).

    輕量版 — 不跑 Gemini (省 API 配額), 只列名單 + 題材 + 上市日 + 承銷價.
    當天 08:30 那封會跑 Gemini 給具體進場建議.
    """
    listings = check_next_week_listings()
    if not listings:
        return ""

    today = dt.date.today()
    days_to_next_monday = (7 - today.weekday()) % 7 or 7
    next_monday = today + dt.timedelta(days=days_to_next_monday)
    next_sunday = next_monday + dt.timedelta(days=6)

    lines = ["📅 <b>下週新股上市預告</b>"]
    lines.append(f"<i>{next_monday.strftime('%m/%d')} ~ {next_sunday.strftime('%m/%d')} | "
                  f"共 {len(listings)} 檔</i>")
    lines.append("")

    by_date = {}
    for s in listings:
        ld = s["list_date"]
        by_date.setdefault(ld, []).append(s)

    weekday_names = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    for ld in sorted(by_date.keys()):
        try:
            d = dt.datetime.strptime(ld, "%Y-%m-%d").date()
            wd = weekday_names[d.weekday()]
            lines.append(f"<b>📌 {d.strftime('%m/%d')} ({wd})</b>")
        except Exception:
            lines.append(f"<b>📌 {ld}</b>")
        for s in by_date[ld]:
            sid = s.get("stock_id", "")
            name = s.get("stock_name", "")
            industry = s.get("industry", "")
            offer = s.get("offer_price", 0) or 0
            desc = s.get("business_description", "")
            themes = _classify_theme(desc, name, industry)
            theme_tag = f" 🔥{'/'.join(themes[:2])}" if themes else ""
            industry_short = (industry or "—")[:6]
            lines.append(
                f"  <code>{sid}</code> {name} · {industry_short} · "
                f"承銷 {offer:.1f} 元{theme_tag}"
            )
        lines.append("")

    lines.append("<i>※ 上市當天 08:30 會推具體 Gemini 分析 + 進場建議</i>")
    lines.append("<i>※ 首日波動極大, 不追高, 嚴守停損</i>")
    return "\n".join(lines)


def check_and_push(mode: str = "today") -> Dict:
    """推播.

    mode:
      "today" — 推今日上市 (含 Gemini 分析)
      "next_week" — 推下週上市預告 (週五用, 輕量版)
    """
    if mode == "next_week":
        msg = build_next_week_preview_msg()
        log_tag = "ipo_next_week_preview"
    else:
        msg = build_today_listing_msg()
        log_tag = "ipo_today"
    if not msg:
        return {"triggered": False, "reason": f"no_listing_{mode}"}
    try:
        import notifier
        ok, info = notifier.send_message(msg, disable_preview=True)
        print(f"[{log_tag}] push: ok={ok}", flush=True)
        return {"triggered": True, "sent": ok, "mode": mode}
    except Exception as e:
        print(f"[{log_tag}] push fail: {e}", flush=True)
        return {"triggered": False, "err": str(e)}
