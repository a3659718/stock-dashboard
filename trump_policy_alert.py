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

TRUMP_COOLDOWN_MIN = 360  # 60→180→240→360 (6 小時 / batch)
TRUMP_MAX_PER_BATCH = 1  # 2→1 (每批最多 1 則, 取最重要)
TRUMP_DAILY_CAP = 4  # 6→4 (一天絕對上限 4 則)

# 關鍵 universe (川普推文常影響的股) — 已移除加密相關 (MSTR/COIN/DJT)
TRUMP_SENSITIVE_UNIVERSE = [
    "DELL", "TSLA", "NVDA", "LMT", "RTX", "XOM", "CVX",
    "X",
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
    """抓 Trump 相關新聞. 回 alert list.

    Bug fix (HIGH): 統一 state keys 與 mark_alerts_sent 對齊
      - sent_hashes (取代舊 'alerted')  — dedup
      - last_sent_ts (取代舊 'last_batch_at') — cooldown
      - daily_count[YYYY-MM-DD]  — daily cap
    """
    state = watchlist_store.load_monitor_state()
    tp_state = state.setdefault("trump_policy_alert", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    now_utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    # cooldown — 統一用 last_sent_ts (mark_alerts_sent 寫的 key)
    last_at = tp_state.get("last_sent_ts") or tp_state.get("last_batch_at")
    if last_at:
        try:
            ts = dt.datetime.fromisoformat(last_at)
            # tz-aware comparison
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            if (now_utc - ts).total_seconds() < TRUMP_COOLDOWN_MIN * 60:
                return []
        except Exception:
            pass

    # daily cap — 統一用 daily_count[today]
    daily_count = tp_state.get("daily_count", {})
    today_pushed = int(daily_count.get(today_str, 0)) if isinstance(daily_count, dict) else 0
    if today_pushed >= TRUMP_DAILY_CAP:
        print(f"[trump_policy] daily cap {TRUMP_DAILY_CAP} reached ({today_pushed}), skip", flush=True)
        return []
    remaining = max(0, TRUMP_DAILY_CAP - today_pushed)

    # dedup — 統一用 sent_hashes (mark_alerts_sent 寫的 key)
    alerted: set = set(tp_state.get("sent_hashes") or tp_state.get("alerted") or [])

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
            # 提高門檻: 至少 3 個關鍵字才推 (避免泛 trump 新聞淹沒)
            if len(hits) < 3:
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
    # daily cap 已在 cooldown 前檢查 (使用 daily_count[today]), remaining 已算好
    max_this_batch = min(TRUMP_MAX_PER_BATCH, remaining)
    return all_news[:max_this_batch]


def analyze_with_gemini(alerts: List[Dict]) -> Dict:
    """用 Gemini 結構化分析: 對美股 / 台股 / 全球商品 影響 + 操作建議.

    回傳 dict (給 notifier 顯示):
      {
        "headline": "一句話總結 (政策核心 + 利多/利空)",
        "us_impact": "對美股 (科技/金融/能源/原物料) 影響, 含具體族群",
        "tw_impact": "對台股影響 + 主要族群",
        "global_impact": {
            "gold": "黃金影響 1 句",
            "oil": "原油影響 1 句",
            "usd": "美元影響 1 句",
            "us_bonds": "美債影響 1 句",
            "btc": "比特幣影響 1 句",
        },
        "trade_action": "操作建議 1-2 句 (含族群方向)",
      }
    若 Gemini 不可用, 回 {} (notifier 自會 fallback 為純標題).
    """
    if not alerts:
        return {}
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return {}
        # 整理新聞文字 (取前 3 則, 每則限 200 字)
        news_lines = []
        for a in alerts[:3]:
            sym = a.get("symbol", "GENERAL")
            title = (a.get("title") or "")[:200]
            kws = ", ".join((a.get("keywords") or [])[:3])
            news_lines.append(f"[{sym}] {title} (關鍵字: {kws})")
        news_block = "\n".join(news_lines)

        prompt = (
            "你是專業總體策略分析師. 以下是川普相關新聞:\n\n"
            f"{news_block}\n\n"
            "請以 JSON 格式回答 (繁體中文, 只輸出 JSON 不要其他字, 每欄聚焦核心結論, 1-2 句):\n"
            "{\n"
            '  "headline": "用一句話總結這些政策的核心方向 + 是利多還是利空 (15 字內)",\n'
            '  "us_impact": "對美股影響 (1 句, 點名: 科技/金融/能源/原物料/Defense 哪些族群受惠或受損)",\n'
            '  "tw_impact": "對台股影響 (1 句, 點名台股族群: 半導體/電子組/航運/重電/生技 哪些受影響)",\n'
            '  "global_impact": {\n'
            '    "gold": "對黃金影響 (1 句, 利多/利空/中性)",\n'
            '    "oil": "對原油影響 (1 句)",\n'
            '    "usd": "對美元影響 (1 句)",\n'
            '    "us_bonds": "對美債殖利率影響 (1 句)"\n'
            "  },\n"
            '  "long_play": "多單怎麼操作: 哪個族群/個股, 進場時機 (例: 開低殺到 -2% 接 / 突破前高追), 停損 -3%, 目標 +5-8% (1 句)",\n'
            '  "short_play": "空單怎麼操作: 哪個族群/個股, 進場時機 (例: 反彈到 5MA 空 / 跌破前低追空), 停損 +3%, 目標 -5% (1 句)",\n'
            '  "position_advice": "對現有持倉的處置: 哪些減碼/停損 (明確點名族群), 哪些可以續抱不動 (1 句)",\n'
            '  "risk_alert": "本次最大風險點 + 何時失效 (1 句, 例如: 若 X 政策被司法擋下則翻盤)"\n'
            "}\n\n"
            "判斷準則:\n"
            "- 關稅 / 貿易戰 → 黃金 ↑, 美元 ↑, 風險資產 ↓, 半導體/出口股壓力\n"
            "- 減稅 / 鬆綁 → 美股 ↑, 美元 ↑, 金融/能源/小型股受惠\n"
            "- 制裁中俄 → 原油 ↑, 國防股 ↑, 中概股 ↓\n"
            "- 半導體出口管制 → SOX 短空, ASML/AMD 衝擊, 台積電要看細節\n"
        )
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None:
            return {}
        resp = model.generate_content(prompt)
        raw = (resp.text or "").strip() if resp else ""
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:].strip()
        import json as _json
        data = _json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[trump_policy] gemini fail: {e}", flush=True)
        return {}


def mark_alerts_sent(alerts: List[Dict]) -> None:
    if not alerts:
        return
    try:
        import watchlist_store
        state = watchlist_store.load_monitor_state()
        tpa = state.setdefault("trump_policy_alert", {})
        sent = tpa.setdefault("sent_hashes", [])
        for a in alerts:
            h = _hash_title(a.get("title", ""))
            if h not in sent:
                sent.append(h)
        tpa["sent_hashes"] = sent[-200:]  # 保留近 200 筆
        tpa["last_sent_ts"] = dt.datetime.utcnow().isoformat()
        # daily counter (date-keyed)
        today = dt.date.today().strftime("%Y-%m-%d")
        daily = tpa.setdefault("daily_count", {})
        if not isinstance(daily, dict):
            daily = {}
            tpa["daily_count"] = daily
        daily[today] = int(daily.get(today, 0)) + len(alerts)
        cutoff = (dt.date.today() - dt.timedelta(days=7)).strftime("%Y-%m-%d")
        tpa["daily_count"] = {k: v for k, v in daily.items() if k >= cutoff}
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[trump_policy] mark_alerts_sent fail: {e}", flush=True)
