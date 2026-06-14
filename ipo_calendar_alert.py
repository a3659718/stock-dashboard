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


def check_us_ipos_next_week() -> List[Dict]:
    """抓美股下週 IPO (Finnhub /calendar/ipo).

    Finnhub 免費版有 /calendar/ipo API.
    """
    out = []
    try:
        import data_sources as ds
        token = ds.get_finnhub_token()
        if not token:
            print("[ipo_us] no FINNHUB_TOKEN", flush=True)
            return []
        import requests
        today = dt.date.today()
        days_to_next_monday = (7 - today.weekday()) % 7 or 7
        next_monday = today + dt.timedelta(days=days_to_next_monday)
        next_sunday = next_monday + dt.timedelta(days=6)
        url = f"https://finnhub.io/api/v1/calendar/ipo"
        params = {
            "from": next_monday.strftime("%Y-%m-%d"),
            "to": next_sunday.strftime("%Y-%m-%d"),
            "token": token,
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            print(f"[ipo_us] finnhub status {r.status_code}", flush=True)
            return []
        js = r.json()
        rows = js.get("ipoCalendar") or []
        for ipo_row in rows:
            try:
                # Finnhub fields: symbol, name, date, exchange, numberOfShares,
                #                  totalSharesValue, price (range), status
                price_range = ipo_row.get("price", "")
                # price 可能是 "10-12" string 或 number
                if isinstance(price_range, str) and "-" in price_range:
                    parts = price_range.split("-")
                    try:
                        offer_low = float(parts[0].strip())
                        offer_high = float(parts[1].strip())
                    except ValueError:
                        offer_low = offer_high = 0
                elif isinstance(price_range, (int, float)):
                    offer_low = offer_high = float(price_range)
                else:
                    offer_low = offer_high = 0

                out.append({
                    "stock_id": ipo_row.get("symbol", ""),
                    "stock_name": ipo_row.get("name", ""),
                    "industry": ipo_row.get("exchange", ""),
                    "offer_price_low": offer_low,
                    "offer_price_high": offer_high,
                    "offer_price": offer_high or offer_low,
                    "list_date": ipo_row.get("date", ""),
                    "shares": ipo_row.get("numberOfShares", 0),
                    "total_value": ipo_row.get("totalSharesValue", 0),
                    "status": ipo_row.get("status", ""),
                    "business_description": "",  # Finnhub 沒提供業務描述
                })
            except Exception:
                continue
        out.sort(key=lambda x: x.get("list_date", ""))
    except Exception as e:
        print(f"[ipo_us] fetch fail: {e}", flush=True)
    return out


def build_us_ipo_preview_msg() -> str:
    """美股下週 IPO 預告 (週末推).

    用 Gemini 過濾 worth_buying='no'.
    """
    listings = check_us_ipos_next_week()
    if not listings:
        return ""

    # Gemini 評估 + 過濾
    candidates = []
    for s in listings[:8]:
        # 給 Gemini 用的 dict (補預設 industry/desc)
        eval_input = {
            "stock_id": s.get("stock_id", ""),
            "stock_name": s.get("stock_name", ""),
            "industry": s.get("industry", "") or "US Stock",
            "offer_price": s.get("offer_price", 0),
            "business_description": s.get("business_description", "") or s.get("stock_name", ""),
        }
        analysis = _gemini_analyze_ipo(eval_input)
        if analysis.get("worth_buying") == "no":
            print(f"[ipo_us_preview] skip {s.get('stock_id','')} {s.get('stock_name','')[:30]} "
                  f"(worth=no)", flush=True)
            continue
        candidates.append((s, analysis))

    if not candidates:
        print("[ipo_us_preview] all IPOs evaluated as 'no', skip push", flush=True)
        return ""

    today = dt.date.today()
    days_to_mon = (7 - today.weekday()) % 7 or 7
    next_mon = today + dt.timedelta(days=days_to_mon)
    next_sun = next_mon + dt.timedelta(days=6)

    lines = ["🇺🇸 <b>下週美股 IPO 預告</b>"]
    lines.append(f"<i>{next_mon.strftime('%m/%d')} ~ {next_sun.strftime('%m/%d')} | "
                  f"{len(candidates)}/{len(listings)} 檔值得關注</i>")
    lines.append("")

    by_date = {}
    for s, analysis in candidates:
        ld = s["list_date"]
        by_date.setdefault(ld, []).append((s, analysis))

    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for ld in sorted(by_date.keys()):
        try:
            d = dt.datetime.strptime(ld, "%Y-%m-%d").date()
            wd = weekday_names[d.weekday()]
            lines.append(f"<b>📌 {d.strftime('%m/%d')} ({wd})</b>")
        except Exception:
            lines.append(f"<b>📌 {ld}</b>")
        for s, analysis in by_date[ld]:
            sid = s.get("stock_id", "")
            name = s.get("stock_name", "")[:35]
            ex = s.get("industry", "")  # exchange
            low = s.get("offer_price_low", 0)
            high = s.get("offer_price_high", 0)
            shares = s.get("shares", 0) or 0
            price_str = f"${low}-{high}" if low != high else f"${low}"
            shares_m = f"{shares/1e6:.1f}M" if shares >= 1e6 else f"{shares/1e3:.0f}k"
            worth = analysis.get("worth_buying", "maybe")
            worth_emoji = {"yes": "✅", "maybe": "🟡"}.get(worth, "🟡")
            rp_low = analysis.get("reasonable_price_low")
            rp_high = analysis.get("reasonable_price_high")
            worth_reason = analysis.get("worth_reason", "")
            lines.append(
                f"  <code>{sid}</code> {name} · {ex} · {price_str} · {shares_m} 股 {worth_emoji}"
            )
            if rp_low and rp_high:
                try:
                    lines.append(f"     📊 合理價 ${float(rp_low):.1f}-${float(rp_high):.1f}")
                except (TypeError, ValueError):
                    pass
            if worth_reason:
                lines.append(f"     🎯 {worth_reason}")
        lines.append("")

    lines.append("<i>※ 已過濾「不值得買」的標的, 只列值得關注</i>")
    lines.append("<i>※ 美股 IPO 開盤常有 ±50% 波動, 嚴守風控</i>")
    return "\n".join(lines)


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
    """Gemini 分析 — 合理價 / 競爭力 / 進場建議 / 值不值得買.

    Fallback: Gemini 不可用就用 rule-based 簡化版.

    回傳:
      reasonable_price_low / reasonable_price_high
      competitive (str)
      entry_action (str)
      worth_buying: "yes" / "maybe" / "no"  ← 新加
      worth_reason: str  ← 新加 (為何值得 / 不值得)
    """
    out = {"reasonable_price_low": None, "reasonable_price_high": None,
           "competitive": "", "entry_action": "",
           "worth_buying": "maybe", "worth_reason": "", "raw": ""}

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
                    "你是新股分析師. 評估這檔 IPO 值不值得買, 給 JSON (繁中, 不要其他字):\n\n"
                    f"代號: {sid}\n股名: {name}\n產業: {industry}\n"
                    f"承銷價: {offer:.2f} 元\n業務: {desc[:300]}\n"
                    f"同業平均 PE: {industry_pe}\n題材: {' / '.join(themes) if themes else '無明顯'}\n\n"
                    "JSON 格式:\n"
                    "{\n"
                    '  "reasonable_price_low": <number 合理價低點>,\n'
                    '  "reasonable_price_high": <number 合理價高點>,\n'
                    '  "competitive": "<1 句競爭力評估: 護城河 / 客戶 / 技術門檻>",\n'
                    '  "entry_action": "<具體進場建議: 開盤 +X% 內可追 / 拉回承銷價 +Y% 接>",\n'
                    '  "worth_buying": "yes / maybe / no",\n'
                    '  "worth_reason": "<1 句為何值得或不值得買>"\n'
                    "}\n"
                    "判斷準則:\n"
                    "- 合理價: 承銷價 ± 30% 區間\n"
                    "- 競爭力: 看公司是否有獨家技術 / 大客戶 / 進入門檻\n"
                    "- worth_buying:\n"
                    "  · yes = 熱門題材 + 估值便宜 (合理價 > 承銷價 +20%) + 競爭力強\n"
                    "  · maybe = 普通題材 / 估值合理\n"
                    "  · no = 估值嚴重高估 (合理價 < 承銷價 -10%) / 弱題材 / 競爭力差\n"
                    "- 嚴格: 寧錯殺勿縱放. 無明顯題材或估值貴就標 no\n"
                    "- entry_action 要具體, 給數字"
                )
                resp = model.generate_content(prompt)
                text = (resp.text or "").strip() if resp else ""
                import re as _re, json as _json
                m = _re.search(r"\{[\s\S]*\}", text)
                if m:
                    try:
                        parsed = _json.loads(m.group(0))
                        wb = (parsed.get("worth_buying", "maybe") or "maybe").lower()
                        if wb not in ("yes", "maybe", "no"):
                            wb = "maybe"
                        out.update({
                            "reasonable_price_low": parsed.get("reasonable_price_low"),
                            "reasonable_price_high": parsed.get("reasonable_price_high"),
                            "competitive": parsed.get("competitive", ""),
                            "entry_action": parsed.get("entry_action", ""),
                            "worth_buying": wb,
                            "worth_reason": parsed.get("worth_reason", ""),
                            "raw": "gemini",
                        })
                        return out
                    except Exception:
                        out["raw"] = text[:500]
    except Exception as e:
        print(f"[ipo] gemini fail: {e}", flush=True)

    # Fallback: 規則式 (Gemini 不可用時)
    if offer > 0:
        if themes and any(t in ("AI / 半導體", "AI 伺服器", "重電 / 核電", "機器人") for t in themes):
            # 熱門題材 → 合理價偏高, 值得買
            out["reasonable_price_low"] = round(offer * 1.0, 2)
            out["reasonable_price_high"] = round(offer * 1.5, 2)
            out["competitive"] = f"熱門題材 ({' / '.join(themes)}) — 短線需求強"
            out["entry_action"] = (
                f"開盤若 ≤ 承銷價 +30% (≤ {offer*1.3:.1f}) 可小量試單, 停損 -5%, 短目標 +15%"
            )
            out["worth_buying"] = "yes"
            out["worth_reason"] = f"熱門題材 + 估值便宜 (合理價高點 {offer*1.5:.1f} > 承銷價)"
        elif themes:
            # 普通題材 → maybe
            out["reasonable_price_low"] = round(offer * 0.9, 2)
            out["reasonable_price_high"] = round(offer * 1.2, 2)
            out["competitive"] = f"題材 ({' / '.join(themes)}) — 中性"
            out["entry_action"] = f"開盤拉回承銷價 +10% 內 (≤ {offer*1.1:.1f}) 才考慮"
            out["worth_buying"] = "maybe"
            out["worth_reason"] = "普通題材, 估值合理"
        else:
            # 無題材 → no
            out["reasonable_price_low"] = round(offer * 0.85, 2)
            out["reasonable_price_high"] = round(offer * 1.05, 2)
            out["competitive"] = f"非熱門題材 ({industry or '傳統產業'})"
            out["entry_action"] = "建議跳過"
            out["worth_buying"] = "no"
            out["worth_reason"] = "無熱門題材 + 估值無折扣, 不值得追"
    out["raw"] = "rule_based"
    return out


def build_today_listing_msg() -> str:
    """今日新股上市推播.

    過濾 worth_buying='no' 的 (不浪費注意力).
    沒新股上市 / 全部 no → 回空字串.
    """
    listings = check_today_listing()
    if not listings:
        return ""

    # 先 evaluate 所有 IPO, 過濾掉 worth_buying='no'
    candidates = []
    for ipo_data in listings[:8]:
        analysis = _gemini_analyze_ipo(ipo_data)
        if analysis.get("worth_buying") == "no":
            print(f"[ipo_today] skip {ipo_data.get('stock_id','')} {ipo_data.get('stock_name','')} "
                  f"(worth=no: {analysis.get('worth_reason','')})", flush=True)
            continue
        candidates.append((ipo_data, analysis))

    if not candidates:
        print("[ipo_today] all IPOs evaluated as 'no', skip push", flush=True)
        return ""

    lines = ["⭐ <b>今日新股上市</b>"]
    today = dt.date.today().strftime("%m/%d")
    lines.append(f"<i>{today} TPE | {len(candidates)}/{len(listings)} 檔值得關注</i>")
    lines.append("")

    for ipo_data, analysis in candidates[:5]:
        sid = ipo_data.get("stock_id", "")
        name = ipo_data.get("stock_name", "")
        offer = float(ipo_data.get("offer_price", 0) or 0)
        industry = ipo_data.get("industry", "")

        rp_low = analysis.get("reasonable_price_low")
        rp_high = analysis.get("reasonable_price_high")
        competitive = analysis.get("competitive", "")
        entry_action = analysis.get("entry_action", "")
        worth = analysis.get("worth_buying", "maybe")
        worth_reason = analysis.get("worth_reason", "")
        worth_emoji = {"yes": "✅", "maybe": "🟡"}.get(worth, "🟡")

        lines.append(f"<code>{sid}</code> <b>{name}</b> ({industry}) {worth_emoji}")
        lines.append(f"  承銷價 <b>{offer:.2f}</b> 元")

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

        if competitive:
            lines.append(f"  💼 {competitive}")
        if worth_reason:
            lines.append(f"  🎯 <b>{worth_emoji} 值得買: {worth_reason}</b>")
        if entry_action:
            lines.append(f"  💡 進場: {entry_action}")
        lines.append("")

    lines.append("<i>※ 已過濾「不值得買」的標的, 只列值得關注</i>")
    lines.append("<i>※ 首日波動極大, 嚴守停損 ≤ 部位 5%</i>")
    return "\n".join(lines)


def check_and_push(mode: str = "today", market: str = "TW") -> Dict:
    """推播.

    mode:
      "today" — 推今日上市 (台股, 含 Gemini)
      "next_week" — 推下週上市預告 (輕量版)
    market: TW / US — 只對 mode='next_week' 生效
    """
    if mode == "next_week":
        if market == "US":
            msg = build_us_ipo_preview_msg()
            log_tag = "ipo_us_next_week"
        else:
            msg = build_next_week_preview_msg()
            log_tag = "ipo_tw_next_week"
    if mode == "next_week":
        if market == "US":
            msg = build_us_ipo_preview_msg()
            log_tag = "ipo_us_next_week"
        else:
            msg = build_next_week_preview_msg()
            log_tag = "ipo_tw_next_week"
    else:
        msg = build_today_listing_msg()
        log_tag = "ipo_today"
    if not msg:
        return {"triggered": False, "reason": f"no_listing_{mode}_{market}"}
    try:
        import notifier
        ok, info = notifier.send_message(msg, disable_preview=True)
        print(f"[{log_tag}] push: ok={ok}", flush=True)
        return {"triggered": True, "sent": ok, "mode": mode, "market": market}
    except Exception as e:
        print(f"[{log_tag}] push fail: {e}", flush=True)
        return {"triggered": False, "err": str(e)}


def build_next_week_preview_msg() -> str:
    """台股下週要上市的新股預告 (週五推).

    用 Gemini 分析 + 過濾掉「不值得買」的標的.
    """
    listings = check_next_week_listings()
    if not listings:
        return ""

    # 過濾 worth_buying='no'
    candidates = []
    for ipo_data in listings[:8]:
        analysis = _gemini_analyze_ipo(ipo_data)
        if analysis.get("worth_buying") == "no":
            print(f"[ipo_tw_preview] skip {ipo_data.get('stock_id','')} {ipo_data.get('stock_name','')} "
                  f"(worth=no: {analysis.get('worth_reason','')})", flush=True)
            continue
        candidates.append((ipo_data, analysis))

    if not candidates:
        print("[ipo_tw_preview] all IPOs evaluated as 'no', skip push", flush=True)
        return ""

    today = dt.date.today()
    days_to_next_monday = (7 - today.weekday()) % 7 or 7
    next_monday = today + dt.timedelta(days=days_to_next_monday)
    next_sunday = next_monday + dt.timedelta(days=6)

    lines = ["📅 <b>下週台股新股上市預告</b>"]
    lines.append(f"<i>{next_monday.strftime('%m/%d')} ~ {next_sunday.strftime('%m/%d')} | "
                  f"{len(candidates)}/{len(listings)} 檔值得關注</i>")
    lines.append("")

    by_date = {}
    for ipo_data, analysis in candidates:
        ld = ipo_data["list_date"]
        by_date.setdefault(ld, []).append((ipo_data, analysis))


    weekday_names = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
    for ld in sorted(by_date.keys()):
        try:
            d = dt.datetime.strptime(ld, "%Y-%m-%d").date()
            wd = weekday_names[d.weekday()]
            lines.append(f"<b>📌 {d.strftime('%m/%d')} ({wd})</b>")
        except Exception:
            lines.append(f"<b>📌 {ld}</b>")
        for ipo_data, analysis in by_date[ld]:
            sid = ipo_data.get("stock_id", "")
            name = ipo_data.get("stock_name", "")
            industry = ipo_data.get("industry", "")
            offer = ipo_data.get("offer_price", 0) or 0
            worth = analysis.get("worth_buying", "maybe")
            worth_emoji = {"yes": "✅", "maybe": "🟡"}.get(worth, "🟡")
            rp_low = analysis.get("reasonable_price_low")
            rp_high = analysis.get("reasonable_price_high")
            worth_reason = analysis.get("worth_reason", "")
            industry_short = (industry or "—")[:6]
            lines.append(
                f"  <code>{sid}</code> {name} · {industry_short} · 承銷 {offer:.1f} 元 {worth_emoji}"
            )
            if rp_low and rp_high:
                try:
                    lines.append(f"     📊 合理價 {float(rp_low):.1f}-{float(rp_high):.1f} 元")
                except (TypeError, ValueError):
                    pass
            if worth_reason:
                lines.append(f"     🎯 {worth_reason}")
        lines.append("")

    lines.append("<i>※ 已過濾「不值得買」的標的, 只列值得關注</i>")
    lines.append("<i>※ 上市當天 08:30 會推具體進場建議</i>")
    return "\n".join(lines)
