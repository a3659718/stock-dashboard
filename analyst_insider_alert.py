"""
analyst_insider_alert.py
分析師升降評 + 內部人 (CEO/CFO) 大額買賣 alert (美股).

來源: Finnhub
  /stock/recommendation-trends — 分析師當月 Strong Buy/Buy/Hold/Sell/Strong Sell
  /stock/insider-transactions — 內部人 Form 4 揭露 (法定 2 天內必須報)

觸發:
  1. 分析師 buy ratio 大幅上升 (升評 wave)
  2. 內部人單筆 ≥ $500K 買進
  3. CEO/CFO 大額買進 (任何金額)

對象: holdings + watchlist + 主流 universe (~25 檔)

API:
  check_analyst_insider() -> List[Dict]
  mark_alerts_sent(alerts) -> None
  analyze_with_gemini(alerts) -> str  # 用 Gemini 解讀
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List

import watchlist_store


INSIDER_BUY_THRESHOLD_USD = 500_000      # 一般員工門檻
CEO_INSIDER_BUY_THRESHOLD_USD = 50_000   # CEO/CFO 較低門檻
DEFAULT_UNIVERSE = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
    "AVGO", "AMD", "PLTR", "DELL", "ORCL", "CRM", "LLY",
    "RKLB", "BABA", "TSM", "ARM",
]


def _fetch_analyst_recommendations(symbol: str) -> Dict:
    """Finnhub /stock/recommendation-trends."""
    try:
        import data_sources as ds
        import requests
        token = ds.get_finnhub_token()
        if not token:
            return {}
        r = requests.get("https://finnhub.io/api/v1/stock/recommendation-trends",
                          params={"symbol": symbol, "token": token}, timeout=10)
        if r.status_code != 200:
            return {}
        data = r.json() or []
        if not data:
            return {}
        # 排序: period (YYYY-MM-DD) DESC, 取最近 2 月對比
        data = sorted(data, key=lambda x: x.get("period", ""), reverse=True)
        cur = data[0]
        prev = data[1] if len(data) >= 2 else None
        cur_buy = (cur.get("strongBuy", 0) or 0) + (cur.get("buy", 0) or 0)
        cur_total = sum((cur.get(k, 0) or 0) for k in
                        ["strongBuy", "buy", "hold", "sell", "strongSell"])
        cur_ratio = cur_buy / cur_total * 100 if cur_total > 0 else 0
        if prev:
            prev_buy = (prev.get("strongBuy", 0) or 0) + (prev.get("buy", 0) or 0)
            prev_total = sum((prev.get(k, 0) or 0) for k in
                              ["strongBuy", "buy", "hold", "sell", "strongSell"])
            prev_ratio = prev_buy / prev_total * 100 if prev_total > 0 else 0
        else:
            prev_ratio = cur_ratio
        return {
            "symbol": symbol,
            "period": cur.get("period", ""),
            "buy_ratio_cur": round(cur_ratio, 1),
            "buy_ratio_prev": round(prev_ratio, 1),
            "buy_ratio_change_pp": round(cur_ratio - prev_ratio, 1),
            "strongBuy": cur.get("strongBuy", 0),
            "buy": cur.get("buy", 0),
            "hold": cur.get("hold", 0),
            "sell": cur.get("sell", 0),
            "strongSell": cur.get("strongSell", 0),
        }
    except Exception:
        return {}


def _fetch_insider_transactions(symbol: str, days_back: int = 30) -> List[Dict]:
    """Finnhub /stock/insider-transactions — 最近 N 天 Form 4 揭露."""
    try:
        import data_sources as ds
        import requests
        token = ds.get_finnhub_token()
        if not token:
            return []
        end = dt.date.today().strftime("%Y-%m-%d")
        start = (dt.date.today() - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")
        r = requests.get("https://finnhub.io/api/v1/stock/insider-transactions",
                          params={"symbol": symbol, "from": start, "to": end, "token": token},
                          timeout=10)
        if r.status_code != 200:
            return []
        data = r.json() or {}
        items = data.get("data") if isinstance(data, dict) else data
        if not items:
            return []
        out = []
        for it in items[:20]:
            # transaction shares × price = total value
            shares = float(it.get("share") or 0)
            price = float(it.get("transactionPrice") or 0)
            value = shares * price
            tx_code = (it.get("transactionCode") or "").upper()  # P=Purchase, S=Sale, A=Award...
            name = it.get("name") or ""
            position = it.get("position") or ""
            is_ceo_cfo = any(t in (position.upper() + name.upper())
                              for t in ["CEO", "CFO", "PRESIDENT", "CHAIRMAN"])
            # 過濾: 買進 + 金額夠大
            if tx_code == "P" and shares > 0:
                threshold = CEO_INSIDER_BUY_THRESHOLD_USD if is_ceo_cfo else INSIDER_BUY_THRESHOLD_USD
                if value >= threshold:
                    out.append({
                        "symbol": symbol,
                        "name": name,
                        "position": position,
                        "is_ceo_cfo": is_ceo_cfo,
                        "shares": int(shares),
                        "price": round(price, 2),
                        "value": int(value),
                        "tx_date": it.get("transactionDate", ""),
                        "filing_date": it.get("filingDate", ""),
                    })
        return out
    except Exception:
        return []


def check_analyst_insider() -> List[Dict]:
    """整合掃描. 回 alert list (含 analyst + insider 2 種類型)."""
    state = watchlist_store.load_monitor_state()
    ai_state = state.setdefault("analyst_insider_alert", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    if ai_state.get("date") != today_str:
        ai_state.clear()
        ai_state.update({"date": today_str, "alerted": []})
    alerted: set = set(ai_state.get("alerted") or [])

    # universe: watchlist + holdings + default
    universe = set(DEFAULT_UNIVERSE)
    try:
        wl = watchlist_store.load_watchlist() or []
        for sid in wl:
            s = str(sid).strip().upper()
            if s and not s.isdigit():
                universe.add(s)
    except Exception:
        pass
    try:
        import holdings_store
        for h in holdings_store.load_holdings() or []:
            sid = str(h.get("stock_id", "")).strip().upper()
            mk = h.get("market", "TW" if sid.isdigit() else "US")
            if sid and mk == "US":
                universe.add(sid)
    except Exception:
        pass

    alerts: List[Dict] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # 1. analyst 升評 wave
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs_a = {ex.submit(_fetch_analyst_recommendations, s): s for s in universe}
        for fut in as_completed(futs_a):
            try:
                r = fut.result()
                if not r: continue
                # 升評 wave: buy_ratio 上升 ≥ 10 pp
                if r.get("buy_ratio_change_pp", 0) >= 10:
                    key = f"analyst:{r['symbol']}"
                    if key not in alerted:
                        r["type"] = "analyst_upgrade"
                        alerts.append(r)
            except Exception:
                pass
    # 2. insider buy
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs_i = {ex.submit(_fetch_insider_transactions, s): s for s in universe}
        for fut in as_completed(futs_i):
            try:
                items = fut.result()
                for it in items:
                    key = f"insider:{it['symbol']}:{it['filing_date']}"
                    if key not in alerted:
                        it["type"] = "insider_buy"
                        alerts.append(it)
            except Exception:
                pass

    return alerts


def mark_alerts_sent(alerts: List[Dict]) -> None:
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        ai = state.setdefault("analyst_insider_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        if ai.get("date") != today_str:
            ai.clear()
            ai.update({"date": today_str, "alerted": []})
        a_set = set(ai.get("alerted") or [])
        for a in alerts:
            if a.get("type") == "analyst_upgrade":
                a_set.add(f"analyst:{a.get('symbol','')}")
            elif a.get("type") == "insider_buy":
                a_set.add(f"insider:{a.get('symbol','')}:{a.get('filing_date','')}")
        ai["alerted"] = sorted(a_set)
        state["analyst_insider_alert"] = ai
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[analyst_insider] mark fail: {e}", flush=True)


def analyze_with_gemini(alerts: List[Dict]) -> str:
    """對 alert 用 Gemini 分析「為什麼 + 該不該跟」. 失敗回空字串."""
    if not alerts:
        return ""
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return ""
        # 只挑 top 3 給 Gemini (省 quota)
        top = alerts[:3]
        ctx_lines = ["以下美股近期出現分析師升評或內部人(CEO/CFO)大額買進, 請逐個分析:"]
        for a in top:
            if a.get("type") == "analyst_upgrade":
                ctx_lines.append(
                    f"[{a['symbol']}] 分析師升評: 上月 BUY {a['buy_ratio_prev']}% "
                    f"→ 本月 {a['buy_ratio_cur']}% (+{a['buy_ratio_change_pp']:.2f} pp)"
                )
            else:
                ctx_lines.append(
                    f"[{a['symbol']}] 內部人 {a['position']} ({a['name']}) "
                    f"買進 ${a['value']:,} ({a['shares']:,} 股 @ ${a['price']:.2f})"
                )
        prompt = (
            "\n".join(ctx_lines) +
            "\n\n請每個 2 句中文白話: (1) 可能原因 (2) 該不該跟進"
            "(BUY/觀望/AVOID). 不要列數據, 聚焦結論."
        )
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None:
            return ""
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip() if resp else ""
        return text
    except Exception as e:
        print(f"[analyst_insider] gemini fail: {e}", flush=True)
        return ""
