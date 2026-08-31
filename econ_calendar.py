"""重大事件預告 — 總經 (FOMC / 非農 / CPI 等) + 重要財報。

為什麼要這個模組:
  原本系統只會「被動」從新聞撈到 FOMC/CPI 字眼, 不會事前提醒; 財報也只在個股被選中時才標。
  這裡提供「主動」的事前預告: 本週 / 今明有哪些重大總經事件與重要財報, 附日期與台北時間。

資料來源:
  1. 總經 (FOMC 利率決議 / 非農就業): 用「內建可靠排程」— FOMC 2026 官方日期 + 非農=每月第一個週五。
     這兩個最重要、且排程固定, 不依賴外部 API, 保證一定抓得到。
  2. CPI / PCE / GDP / 零售 等其他總經: 嘗試 Finnhub economic calendar (部分方案是 premium, 失敗就略過)。
  3. 重要財報: Finnhub earnings calendar (免費方案可用) 一次抓整個區間, 再過濾成「大型權值股 + 用戶自選」。

所有函式對外部失敗都 graceful (回空 list / 空字串), 不 raise, 不會炸掉呼叫它的推播。
"""
from __future__ import annotations

import datetime as _dt
from typing import Dict, List, Optional

try:
    import requests as _requests
except Exception:  # pragma: no cover
    _requests = None

try:
    import data_sources as _ds
except Exception:  # pragma: no cover
    _ds = None


# ---------------------------------------------------------------------------
# 內建可靠排程: FOMC (2026 官方) + 非農 (每月第一個週五)
# ---------------------------------------------------------------------------
# FOMC 2026 「決議公布日」= 會議第二天 (14:00 ET 公布聲明 → 台北隔日 02:00/03:00)。
# 來源: Federal Reserve 官方 FOMC 行事曆。
FOMC_2026: List[_dt.date] = [
    _dt.date(2026, 1, 28),
    _dt.date(2026, 3, 18),
    _dt.date(2026, 4, 29),
    _dt.date(2026, 6, 17),
    _dt.date(2026, 7, 29),
    _dt.date(2026, 9, 16),
    _dt.date(2026, 10, 28),
    _dt.date(2026, 12, 9),
]
# 3/6/9/12 這幾次含「經濟預測 + 點陣圖 (dot plot)」, 對市場影響更大。
FOMC_WITH_SEP = {
    _dt.date(2026, 3, 18),
    _dt.date(2026, 6, 17),
    _dt.date(2026, 9, 16),
    _dt.date(2026, 12, 9),
}


def _first_friday(year: int, month: int) -> _dt.date:
    d = _dt.date(year, month, 1)
    # Monday=0 .. Sunday=6; Friday=4
    return d + _dt.timedelta(days=(4 - d.weekday()) % 7)


def _nfp_tpe_time(d: _dt.date) -> str:
    """非農 (08:30 ET) 對應的台北時間 — 隨美東夏令/冬令變動。
    EDT (UTC-4) → 台北 20:30 ; EST (UTC-5) → 台北 21:30。
    重用 index_alerts 已寫好的夏令判斷, 不另外土砲一份。
    """
    try:
        import index_alerts as _ia
        return "20:30" if _ia._is_us_in_dst(d) else "21:30"
    except Exception:
        return "20:30"


def _nfp_dates(from_d: _dt.date, to_d: _dt.date) -> List[_dt.date]:
    """美國非農就業 (Nonfarm Payrolls) — BLS 慣例為每月第一個週五公布。
    少數月份 (第一個週五落在 1 號等) 會延到第二週, 但多數月份此規則準確。"""
    out = []
    y, m = from_d.year, from_d.month
    for _ in range(3):  # 涵蓋跨月
        ff = _first_friday(y, m)
        if from_d <= ff <= to_d:
            out.append(ff)
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def get_macro_events(from_d: _dt.date, to_d: _dt.date) -> List[Dict]:
    """回傳窗口 [from_d, to_d] 內的高影響美股總經事件。"""
    events: List[Dict] = []

    for d in FOMC_2026:
        if from_d <= d <= to_d:
            extra = "（含經濟預測 / 點陣圖）" if d in FOMC_WITH_SEP else ""
            events.append({
                "date": d, "type": "FOMC",
                "title": f"FOMC 利率決議{extra}",
                "impact": "high",
                "tpe_time": "台北隔日 02:00–03:00 公布",
            })

    for d in _nfp_dates(from_d, to_d):
        events.append({
            "date": d, "type": "NFP",
            "title": "美國非農就業數據 (Nonfarm Payrolls)",
            "impact": "high",
            # Bug fix (2026-08): NFP 固定 08:30 ET 公布。EDT = 台北 20:30, EST = 台北 21:30。
            # 原本寫死 20:30, 冬令 (11 月–3 月) 那幾次會讓你早一小時守盤。
            "tpe_time": f"台北 {_nfp_tpe_time(d)}",
        })

    # 其他總經 (CPI/PCE/GDP…) — 嘗試 Finnhub, 失敗略過 (FOMC/非農已由上面保底)
    events += _fetch_finnhub_economic(from_d, to_d)

    # 去重 (同日同類型只留一筆)
    seen = set()
    uniq = []
    for e in sorted(events, key=lambda x: (x["date"], x.get("type", ""))):
        key = (e["date"], e.get("type", ""), e.get("title", "")[:20])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    return uniq


def _fetch_finnhub_economic(from_d: _dt.date, to_d: _dt.date) -> List[Dict]:
    """Finnhub economic calendar — 補 CPI/PCE/GDP/零售 等。部分方案 premium → 失敗回 []。"""
    if _requests is None or _ds is None:
        return []
    try:
        token = _ds.get_finnhub_token()
    except Exception:
        token = None
    if not token:
        return []
    try:
        r = _requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"from": from_d.isoformat(), "to": to_d.isoformat(), "token": token},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        rows = (r.json() or {}).get("economicCalendar", []) or []
    except Exception:
        return []

    KW = ("cpi", "pce", "gdp", "nonfarm", "retail sales", "unemployment",
          "ppi", "core inflation", "consumer confidence", "ism")
    out = []
    for e in rows:
        country = (e.get("country") or "").upper()
        if country not in ("US", "USA", ""):
            continue
        ev = (e.get("event") or "").strip()
        impact = (e.get("impact") or "").lower()
        if impact != "high" and not any(k in ev.lower() for k in KW):
            continue
        raw_t = (e.get("time") or "")[:10]
        try:
            ed = _dt.date.fromisoformat(raw_t)
        except Exception:
            continue
        # FOMC / 非農已由內建排程處理, 這裡避免重複
        low = ev.lower()
        if "fomc" in low or "rate decision" in low or "nonfarm" in low:
            continue
        out.append({"date": ed, "type": "ECON", "title": ev, "impact": impact or "med",
                    "tpe_time": ""})
    return out


# ---------------------------------------------------------------------------
# 重要財報 — Finnhub earnings calendar 一次抓區間, 過濾成大型股 + 用戶自選
# ---------------------------------------------------------------------------
# 財報最受市場關注的大型權值股 / 指標股 (漲跌會帶動大盤或族群)
MAJOR_US = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "AVGO", "AMD",
    "NFLX", "JPM", "BAC", "V", "MA", "WMT", "COST", "LLY", "UNH", "XOM",
    "JNJ", "PG", "KO", "PEP", "HD", "DIS", "CRM", "ORCL", "ADBE", "INTC",
    "QCOM", "MU", "PLTR", "SMCI", "ASML", "TSM", "BABA", "UBER", "BA", "CAT",
]


def _fetch_finnhub_earnings(from_d: _dt.date, to_d: _dt.date) -> List[Dict]:
    if _requests is None or _ds is None:
        return []
    try:
        token = _ds.get_finnhub_token()
    except Exception:
        token = None
    if not token:
        return []
    try:
        r = _requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": from_d.isoformat(), "to": to_d.isoformat(), "token": token},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        rows = (r.json() or {}).get("earningsCalendar", []) or []
    except Exception:
        return []
    out = []
    for e in rows:
        raw = (e.get("date") or "")[:10]
        try:
            ed = _dt.date.fromisoformat(raw)
        except Exception:
            continue
        out.append({
            "date": ed,
            "symbol": (e.get("symbol") or "").upper(),
            "hour": e.get("hour") or "",   # bmo=盤前, amc=盤後
            "eps_est": e.get("epsEstimate"),
        })
    return out


def get_earnings_events(from_d: _dt.date, to_d: _dt.date,
                        watchlist: Optional[List[str]] = None) -> List[Dict]:
    """窗口內的重要財報 = 大型權值股 + 用戶自選 (美股)。"""
    all_e = _fetch_finnhub_earnings(from_d, to_d)
    if not all_e:
        return []
    major = set(MAJOR_US)
    if watchlist:
        major |= {str(s).upper() for s in watchlist if s}
    picked = [e for e in all_e if e["symbol"] in major]
    picked.sort(key=lambda x: (x["date"], x["symbol"]))
    return picked


# ---------------------------------------------------------------------------
# 組推播訊息
# ---------------------------------------------------------------------------
_WD = ["一", "二", "三", "四", "五", "六", "日"]


def _hour_label(hour: str) -> str:
    return {"bmo": "盤前", "amc": "盤後", "dmh": "盤中"}.get((hour or "").lower(), "")


def build_events_digest(mode: str = "week",
                        watchlist: Optional[List[str]] = None,
                        today: Optional[_dt.date] = None) -> str:
    """組『重大事件預告』TG 訊息。

    mode="week"  → 今天起未來 7 天 (適合週一整週預告)
    mode="today" → 今明兩天 (適合每日 heads-up)
    無任何事件 → 回 "" (caller 不推)。
    """
    if today is None:
        # GitHub Actions 跑在 UTC; 台北早上 08:00 = UTC 00:00 同日, 用 UTC date 即可對齊台北當天
        today = _dt.datetime.utcnow().date()
    if mode == "today":
        from_d, to_d = today, today + _dt.timedelta(days=1)
        header = "📅 <b>今明重大事件</b>"
    else:
        from_d, to_d = today, today + _dt.timedelta(days=7)
        header = "📅 <b>本週重大事件預告</b>"

    macro = get_macro_events(from_d, to_d)
    earnings = get_earnings_events(from_d, to_d, watchlist)
    if not macro and not earnings:
        return ""

    # 依日期彙整
    by_date: Dict[_dt.date, Dict[str, list]] = {}
    for e in macro:
        by_date.setdefault(e["date"], {"macro": [], "earn": []})["macro"].append(e)
    for e in earnings:
        by_date.setdefault(e["date"], {"macro": [], "earn": []})["earn"].append(e)

    lines = [header, "━━━━━━━━━━━━━━━━━"]
    for d in sorted(by_date.keys()):
        wd = _WD[d.weekday()]
        d_lbl = f"{d.month}/{d.day}（{wd}）"
        # 距今天數
        dd = (d - today).days
        rel = "今天" if dd == 0 else ("明天" if dd == 1 else f"{dd} 天後")
        lines.append(f"\n<b>{d_lbl} · {rel}</b>")
        for e in by_date[d]["macro"]:
            emoji = "🏦" if e["type"] == "FOMC" else ("📊" if e["type"] == "NFP" else "📈")
            t = e.get("tpe_time", "")
            lines.append(f"  {emoji} <b>{e['title']}</b>" + (f"  <i>{t}</i>" if t else ""))
        earn = by_date[d]["earn"]
        if earn:
            syms = []
            for e in earn[:12]:
                hl = _hour_label(e.get("hour", ""))
                syms.append(f"{e['symbol']}" + (f"({hl})" if hl else ""))
            lines.append(f"  💼 財報: {' · '.join(syms)}")
            if len(earn) > 12:
                lines.append(f"     …另有 {len(earn) - 12} 檔")

    lines.append("")
    lines.append("<i>※ 總經數據 / 財報前後波動常放大, 注意倉位與停損。</i>")
    return "\n".join(lines)


if __name__ == "__main__":  # 手動測試
    print(build_events_digest("week"))
    print("\n\n=== today ===")
    print(build_events_digest("today"))
