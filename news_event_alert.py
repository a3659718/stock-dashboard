"""
news_event_alert.py
事件型新聞推播 — 抓 Yahoo News + Finnhub general news, 對 watchlist + holdings
+ 主流個股 universe, 用關鍵字過濾 (Trump/FDA/buyback/併購/upgrade 等), 命中就推 TG.

跟既有 morning_brief 的差別:
  - morning_brief: 每日 08:00 定時, AI 摘要新聞
  - 本模組: 每次 monitor cron tick (~15 min) 都掃, 事件命中立刻推 (1-15 min 延遲)

Throttle:
  - per-(stock_id, headline_hash) per-day: 同新聞只推一次
  - 全域 cooldown 30 min between batches

State: monitor_state["news_event_alert"] = {
   "date": "YYYY-MM-DD",
   "alerted": [{sid, h_hash}, ...],
   "last_batch_at": iso,
}

API:
  - check_news_events() -> List[Dict]
  - mark_alerts_sent(alerts) -> None
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Dict, List, Optional, Set

import data_sources as ds
import watchlist_store


# === 中英關鍵字 — 命中即視為重大事件 ===
KEYWORDS_EN = [
    # 政治/名人
    "trump", "biden", "musk", "tweets",
    # FDA / Biotech
    "fda", "approval", "phase 3", "clinical", "pdufa",
    # 公司財務行為
    "buyback", "share repurchase", "acquisition", "merger",
    "spin-off", "spinoff", "ipo", "secondary offering",
    # Earnings
    "earnings beat", "beats estimates", "misses estimates",
    "guidance raised", "guidance lowered", "guidance cut",
    # 分析師
    "upgrade", "downgrade", "price target raised",
    "initiated coverage",
    # 重大事件
    "lawsuit", "scandal", "settled", "settlement",
    "ceo fired", "ceo resigns", "ceo steps down",
    "partnership", "deal", "contract awarded",
    "investigation", "probe",
]

KEYWORDS_ZH = [
    "川普", "馬斯克",
    "FDA", "通過", "核准",
    "買回", "庫藏股", "併購", "收購", "合併",
    "增資", "減資", "配息", "配股",
    "利多", "利空", "暴漲", "暴跌",
    "法說", "財報優於", "不如預期",
    "上調", "下調", "調升", "調降",
    "違規", "罰款", "提告", "辭職", "解任",
    "策略合作", "簽約", "獲利",
]

# 全域 throttle
NEWS_COOLDOWN_MIN = 30
NEWS_MAX_PER_BATCH = 5  # 一次推播最多 N 則新聞 (避免暴量)


# === Urgency 分級 (HIGH=急報響鈴 / MED=注意響鈴 / LOW=靜音批次) ===
URGENCY_HIGH_KEYWORDS = {
    "fda", "approval", "buyback", "share repurchase", "acquisition", "merger",
    "spin-off", "spinoff", "ceo fired", "ceo resigns", "ceo steps down",
    "lawsuit", "scandal", "settled",
    "併購", "收購", "合併", "庫藏股", "FDA", "通過", "核准",
    "辭職", "解任", "違規", "罰款", "暴漲", "暴跌",
}
URGENCY_MED_KEYWORDS = {
    "upgrade", "downgrade", "price target", "earnings beat", "beats estimates",
    "misses estimates", "guidance raised", "guidance lowered", "guidance cut",
    "trump", "musk", "biden",
    "上調", "下調", "調升", "調降", "財報優於", "不如預期", "利多", "利空",
    "川普", "馬斯克",
}


def classify_urgency(alert: Dict) -> str:
    """根據 source_type + keywords 算 urgency level: HIGH / MED / LOW."""
    src = (alert.get("source_type") or "").upper()
    # SEC 8-K / 重大訊息 = 最高權威, 直接 HIGH
    if src in {"8-K", "8-K/A", "TW_NEWS"}:
        return "HIGH"
    if src == "S-1":
        return "MED"  # IPO 注意但非急報
    # 用關鍵字判斷
    title_lower = (alert.get("title") or "").lower()
    hits_lower = [str(k).lower() for k in (alert.get("keywords_hit") or [])]
    all_text = title_lower + " " + " ".join(hits_lower)
    for kw in URGENCY_HIGH_KEYWORDS:
        if kw.lower() in all_text:
            return "HIGH"
    for kw in URGENCY_MED_KEYWORDS:
        if kw.lower() in all_text:
            return "MED"
    return "LOW"

# 主流 universe (給沒設 watchlist/holdings 的用戶當預設掃描範圍)
DEFAULT_US_UNIVERSE_NEWS = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA",
    "AVGO", "AMD", "PLTR", "DELL", "ORCL", "CRM", "LLY", "REGN",
    "RKLB", "ASTS", "BABA", "TSM",
]
DEFAULT_TW_UNIVERSE_NEWS = [
    "2330", "2317", "2454", "2382", "2308",
    "3231", "6669", "1519", "1513",
]


def _is_tw_or_us_session() -> bool:
    """是否該掃新聞.
    平日: 台股或美股 session 內才掃 (省 API call).
    週末: 全天放行 (新聞/8-K filings 24x7 都會發, 持倉用戶想立即知道).
    """
    # Py 3.12+: utcnow() deprecated, 用 datetime.now(timezone.utc)
    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    # 週末: 全天掃 (重大新聞/併購公告/CEO 變動常週末發)
    if now_utc.weekday() >= 5:
        return True
    cur = now_utc.hour + now_utc.minute / 60.0
    # 平日 TW: 01:00-05:30 UTC, US: 13:30-21:00 UTC (含 DST 邊界)
    return (1.0 <= cur < 5.5) or (13.0 <= cur < 21.5)


def _headline_hash(headline: str) -> str:
    """新聞標題 hash (前 8 字), 給去重用."""
    if not headline:
        return ""
    return hashlib.md5(headline.encode("utf-8")).hexdigest()[:12]


def _match_keywords(text: str) -> List[str]:
    """回傳命中的關鍵字 list. 不分大小寫."""
    if not text:
        return []
    t = text.lower()
    hits = []
    for kw in KEYWORDS_EN:
        if kw.lower() in t:
            hits.append(kw)
    for kw in KEYWORDS_ZH:
        if kw in text:  # 中文不用 lower
            hits.append(kw)
    return hits


def _gather_universe() -> List[Dict]:
    """整合 watchlist + holdings + actionable picks + 預設主流 universe.
    回 [{"symbol": str, "name": str, "market": "TW"/"US", "tag": "watch/hold/pick"}].
    """
    universe = {}  # {symbol: {market, tag}}, dedup

    # Watchlist (用戶觀察名單) — 通常含台股
    try:
        wl = watchlist_store.load_watchlist() or []
        for sid in wl:
            sid = str(sid).strip().upper()
            if not sid:
                continue
            if sid.isdigit():
                universe[sid] = {"market": "TW", "tag": "watch"}
            else:
                universe[sid] = {"market": "US", "tag": "watch"}
    except Exception:
        pass

    # Holdings
    try:
        import holdings_store
        for h in holdings_store.load_holdings() or []:
            sid = str(h.get("stock_id", "")).strip().upper()
            if not sid:
                continue
            mk = h.get("market", "TW")
            universe[sid] = {"market": mk, "tag": "hold"}
    except Exception:
        pass

    # 預設主流 universe
    for sid in DEFAULT_US_UNIVERSE_NEWS:
        if sid not in universe:
            universe[sid] = {"market": "US", "tag": "main"}
    for sid in DEFAULT_TW_UNIVERSE_NEWS:
        if sid not in universe:
            universe[sid] = {"market": "TW", "tag": "main"}

    return [{"symbol": s, **v} for s, v in universe.items()]


def _scan_news_for_symbol(item: Dict, alerted_hashes: Set[str]) -> List[Dict]:
    """單檔抓多來源新聞 (Yahoo + Finnhub 8-K/PR + TW 重大訊息), 過濾關鍵字, 回觸發 alert list.
    跳過已 alerted_hashes 內的 (避免反覆推同新聞).

    來源優先順序:
      1. Finnhub 8-K (美股, SEC 直發, 最早) — 命中自動加 [8K] tag
      2. Finnhub PR (美股, 公司主動發布)
      3. FinMind 台股重大訊息 (台股)
      4. Yahoo News (.TW / .TWO fallback) — 最慢但覆蓋率高
    """
    sym = item["symbol"]
    market = item["market"]
    tag = item["tag"]
    out = []
    try:
        # === 1. 多來源彙整 ===
        news = []
        try:
            import event_sources as es
            if market == "US":
                # 8-K 命中 = 即推 (不管關鍵字; 8-K 本身就是重大事件)
                eight_k = es.fetch_finnhub_8k(sym, days_back=2) or []
                for k in eight_k:
                    k["_force_alert"] = True  # 8-K 強制觸發, 不需關鍵字命中
                    news.append(k)
                # Press releases
                pr = es.fetch_us_press_releases(sym, days_back=2) or []
                news.extend(pr)
            elif market == "TW":
                tw_major = es.fetch_tw_major_announcements(stock_id=sym, days_back=2) or []
                news.extend(tw_major)
        except Exception as _e:
            print(f"[news_event] {sym} 抓 8K/PR/TW 重大訊息失敗 (fallback Yahoo): {_e}", flush=True)

        # === 2. Yahoo News fallback (覆蓋率高) ===
        if market == "TW":
            yh = ds.fetch_yahoo_news(f"{sym}.TW", max_n=6) or []
            if not yh:
                yh = ds.fetch_yahoo_news(f"{sym}.TWO", max_n=6) or []
        else:
            yh = ds.fetch_yahoo_news(sym, max_n=6) or []
        # 標記 Yahoo source
        for y in yh:
            if isinstance(y, dict):
                y.setdefault("type", "YAHOO")
        news.extend(yh)
        for n in news:
            if not isinstance(n, dict):
                continue
            title = n.get("title", "") or ""
            link = n.get("link", "") or ""
            if not title:
                continue
            # 8-K 強制觸發, 不需關鍵字命中 (本身已是重大事件)
            force = bool(n.get("_force_alert"))
            hits = _match_keywords(title) if not force else ["8K-AUTO"]
            if not hits:
                continue
            # 去重
            h_hash = _headline_hash(title)
            dedup_key = f"{sym}:{h_hash}"
            if dedup_key in alerted_hashes:
                continue
            alerted_hashes.add(dedup_key)
            alert_d = {
                "symbol": sym,
                "market": market,
                "tag": tag,         # watch / hold / main
                "title": title,
                "link": link,
                "publisher": n.get("publisher", ""),
                "source_type": n.get("type", "YAHOO"),  # 8-K / PR / TW_NEWS / YAHOO
                "keywords_hit": hits[:5],
                "h_hash": h_hash,
            }
            alert_d["urgency"] = classify_urgency(alert_d)
            out.append(alert_d)
    except Exception as e:
        print(f"[news_event] {sym} 掃新聞失敗: {e}", flush=True)
    return out


def check_news_events() -> List[Dict]:
    """整合掃 watchlist + holdings + 主流個股 universe 的事件型新聞.

    HIGH pattern: 不在這裡 save state. caller 用 mark_alerts_sent 在 send 成功後寫.
    """
    if not _is_tw_or_us_session():
        return []

    state = watchlist_store.load_monitor_state()
    ne_state = state.setdefault("news_event_alert", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    # 跨日 reset
    if ne_state.get("date") != today_str:
        ne_state.clear()
        ne_state.update({"date": today_str, "alerted": [], "last_batch_at": None})

    # 全域 cooldown
    last_batch = ne_state.get("last_batch_at")
    if last_batch:
        try:
            ts = dt.datetime.fromisoformat(last_batch)
            if (now_utc - ts).total_seconds() < NEWS_COOLDOWN_MIN * 60:
                return []
        except Exception:
            pass

    # 已 alerted 的去重 keys (今天)
    alerted_hashes = set(ne_state.get("alerted") or [])

    universe = _gather_universe()
    print(f"[news_event] 掃 {len(universe)} 檔 universe", flush=True)

    # Bug fix: all_alerts 原本從未初始化 → 進到下方 .extend / if not all_alerts 必 NameError,
    #          導致新聞事件警報每次呼叫都炸、等於完全沒在推。
    all_alerts: list = []

    # 並行掃 (Yahoo News 每檔 ~1-2s, 30 檔 ~5s with 8 workers)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=2) as ex:  # 每檔掃 4 來源, workers=2 避免 Yahoo 429
        futs = {ex.submit(_scan_news_for_symbol, it, alerted_hashes): it
                for it in universe}
        for fut in as_completed(futs):
            try:
                hits = fut.result()
                if hits:
                    all_alerts.extend(hits)
            except Exception:
                continue

    if not all_alerts:
        return []

    # 排序: HIGH > MED > LOW; 同 urgency 內 hold > watch > main
    urgency_order = {"HIGH": 0, "MED": 1, "LOW": 2}
    tag_priority = {"hold": 0, "watch": 1, "main": 2}
    all_alerts.sort(key=lambda x: (
        urgency_order.get(x.get("urgency", "LOW"), 9),
        tag_priority.get(x.get("tag", "main"), 9),
    ))

    return all_alerts[:NEWS_MAX_PER_BATCH]


def unmark_alerts_sent(alerts: List[Dict]) -> None:
    """回滾 mark_alerts_sent — 送出失敗時呼叫, 把剛 claim 的 (sid, h_hash) 移除, 讓下次能重試."""
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        ne_state = state.get("news_event_alert") or {}
        alerted = set(ne_state.get("alerted") or [])
        for a in alerts:
            alerted.discard(f"{a.get('symbol', '')}:{a.get('h_hash', '')}")
        ne_state["alerted"] = sorted(alerted)
        state["news_event_alert"] = ne_state
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[news_event] unmark_alerts_sent failed: {e}", flush=True)


def mark_alerts_sent(alerts: List[Dict]) -> None:
    """把 (sid, h_hash) 加進已 alerted set. 防重複建議「送出前」就 claim, 送失敗再 unmark 回滾."""
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        ne_state = state.setdefault("news_event_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        if ne_state.get("date") != today_str:
            ne_state.clear()
            ne_state.update({"date": today_str, "alerted": [], "last_batch_at": None})
        alerted = set(ne_state.get("alerted") or [])
        for a in alerts:
            alerted.add(f"{a.get('symbol', '')}:{a.get('h_hash', '')}")
        ne_state["alerted"] = sorted(alerted)
        ne_state["last_batch_at"] = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat()
        state["news_event_alert"] = ne_state
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[news_event] mark_alerts_sent failed: {e}", flush=True)
