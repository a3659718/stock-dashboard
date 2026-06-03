"""
trump_policy_alert.py
川普政策推播 — 抓含 Trump 相關新聞, 用 Gemini 分析對股市影響.

來源:
  - Finnhub general news 含 "Trump" / "tariff" / "executive order"
  - Yahoo Finance 個股新聞 (DELL/TSLA/NVDA/X 等川普常提)

觸發:
  - 新聞含「Trump + 個股/族群關鍵字」
  - per-(headline_hash) 1 次 dedup
  - 全域 cooldown 60min

API:
  check_trump_policy_news() -> List[Dict]
  mark_alerts_sent(alerts) -> None
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Dict, List

import watchlist_store


# Trump 常提的標的/族群關鍵字
TRUMP_KEYWORDS_EN = [
    "trump", "white house", "executive order", "tariff", "tariffs",
    "trade war", "china tariff", "fed pressure", "powell",
    "deportation", "immigration", "border",
    "drill", "energy independence", "oil", "exxon",
    "tesla", "musk", "dogecoin", "spacex",
    "dell", "nvda", "ai chip", "semiconductor",
    "lockheed", "defense", "military",
    "made in america", "reshore", "reshoring",
]

TRUMP_COOLDOWN_MIN = 180  # 60 → 180 降頻
TRUMP_MAX_PER_BATCH = 3  # 5 → 3 降量

# 關鍵 universe (川普推文常影響的股)
TRUMP_SENSITIVE_UNIVERSE = [
    "DELL", "TSLA", "NVDA", "LMT", "RTX", "XOM", "CVX",
    "X", "MSTR", "COIN", "DJT",
]


def _hash_title(title: str) -> str:
    return hashlib.md5((title or "").encode("utf-8")).hexdigest()[:12]


def _match_trump_keywords(title: str) -> List[str]:
    if not title:
        return []
    t = title.lower()
    hits = [k for k in TRUMP_KEYWORDS_EN if k in t]
    return hits[:5]


def check_trump_policy_news() -> List[Dict]:
    """抓 Trump 相關新聞. 回 alert list."""
    state = watchlist_store.load_monitor_state()
    tp_state = state.setdefault("trump_policy_alert", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    # 跨日 reset
    if tp_state.get("date") != today_str:
        tp_state.clear()
        tp_state.update({"date": today_str, "alerted": [], "last_batch_at": None})
    # cooldown
    last_at = tp_state.get("last_batch_at")
    if last_at:
        try:
            ts = dt.datetime.fromisoformat(last_at)
            if (now_utc - ts).total_seconds() < TRUMP_COOLDOWN_MIN * 60:
                return []
        except Exception:
            pass
    alerted: set = set(tp_state.get("alerted") or [])

    all_news = []
    # 1. Finnhub general news
    try:
        import data_sources as ds
        news = ds.fetch_finnhub_news(category="general", max_n=30) or []
        for n in news:
            if not isinstance(n, dict):
                continue
            head = n.get("headline") or n.get("title") or ""
            url = n.get("url") or n.get("link") or ""
            src = n.get("source") or "Finnhub"
            ts = n.get("datetime")
            if ts and isinstance(ts, (int, float)):
                pub_date = dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
            else:
                pub_date = ""
            hits = _match_trump_keywords(head)
            # 提高門檻: 至少 2 個關鍵字才推 (避免泛 trump 新聞淹沒)
            if len(hits) < 2:
                continue
            h_hash = _hash_title(head)
            if h_hash in alerted:
                continue
            all_news.append({
                "symbol": "GENERAL",
                "title": head,
                "link": url,
                "publisher": src,
                "date": pub_date,
                "keywords": hits,
                "h_hash": h_hash,
            })
    except Exception as e:
        print(f"[trump_policy] finnhub fail: {e}", flush=True)

    # 2. Yahoo News 對 Trump-sensitive universe
    try:
        import data_sources  # HIGH fix: 頂層 import 避免 NameError
        for sym in TRUMP_SENSITIVE_UNIVERSE:
            try:
                yh = data_sources.fetch_yahoo_news(sym, max_n=5) or []
            except Exception:
                yh = []
            for n in yh:
                if not isinstance(n, dict):
                    continue
                head = n.get("title") or ""
                hits = _match_trump_keywords(head)
                if not hits:
                    continue
                h_hash = _hash_title(head)
                if h_hash in alerted:
                    continue
                all_news.append({
                    "symbol": sym,
                    "title": head,
                    "link": n.get("link") or "",
                    "publisher": n.get("publisher") or "Yahoo",
                    "date": n.get("date") or "",
                    "keywords": hits,
                    "h_hash": h_hash,
                })
    except Exception as e:
        print(f"[trump_policy] yahoo fail: {e}", flush=True)

    if not all_news:
        return []
    return all_news[:TRUMP_MAX_PER_BATCH]


def analyze_with_gemini(alerts: List[Dict]) -> str:
    """用 Gemini 分析 Trump 新聞對股市影響."""
    if not alerts:
        return ""
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return ""
        ctx_lines = ["以下是川普相關新聞, 請逐條 2 句中文白話分析「對股市影響」:"]
        for a in alerts[:3]:
            ctx_lines.append(
                f"[{a['symbol']}] {a['title'][:200]} (關鍵字: {', '.join(a['keywords'])})"
            )
        prompt = "\n".join(ctx_lines) + \
            "\n\n每條給: (1) 利好還是利空 (2) 該關注哪個族群/個股. 聚焦結論."
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None:
            return ""
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip() if resp else ""
        return text
    except Exception as e:
        print(f"[trump_policy] gemini fail: {e}", flush=True)
        return ""


def mark_alerts_sent(alerts: List[Dict]) -> None:
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        tp = state.setdefault("trump_policy_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        if tp.get("date") != today_str:
            tp.clear()
            tp.update({"date": today_str, "alerted": [], "last_batch_at": None})
        a_set = set(tp.get("alerted") or [])
        for a in alerts:
            a_set.add(a.get("h_hash", ""))
        tp["alerted"] = sorted(a_set)
        tp["last_batch_at"] = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()
        state["trump_policy_alert"] = tp
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[trump_policy] mark fail: {e}", flush=True)
