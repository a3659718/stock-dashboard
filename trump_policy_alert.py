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


def _rule_based_action(alerts: List[Dict]) -> Dict:
    """規則式操作建議 — Gemini 不可用時 fallback. 根據新聞關鍵字推導.

    回傳跟 Gemini 一樣的 dict 格式, 確保 notifier 永遠有操作建議區塊顯示.
    """
    if not alerts:
        return {}
    # 整理全部關鍵字 (跨所有新聞合計)
    all_kw = set()
    all_titles = " ".join((a.get("title") or "") for a in alerts).lower()
    for a in alerts:
        for k in (a.get("keywords") or []):
            all_kw.add(k.lower())

    # 分類: 利多 / 利空 / 中性
    headline = "政策新聞 — 留意盤前波動"
    us_imp = "視政策細節而定 (詳查主新聞連結)"
    tw_imp = "視政策細節而定"
    gold = "中性"; oil = "中性"; usd = "中性"; bonds = "中性"
    long_play = "等盤前確定方向再進場, 不預判搶反彈/搶空"
    short_play = "等盤前確定方向再進場, 不預判"
    pos_advice = "持倉先觀察 30 分鐘, 不急著動作"
    risk_alert = "政策落地時間 + 對等報復可能"

    # === Rule 1: 關稅 / 貿易戰 → 黃金↑, 美元↑, 半導體↓ ===
    if any(k in all_titles for k in ["tariff", "trade war", "china tariff", "關稅"]):
        headline = "🔴 關稅 / 貿易戰升溫, 短期偏空"
        us_imp = "半導體 (SOX/NVDA/AMD/ASML) + 出口股 (AAPL) 短壓; 國防 (LMT/RTX) + 國內基建 (CAT) 相對抗"
        tw_imp = "半導體 (2330/2454/3711) + 蘋概 (2317) 開低風險; 重電/AI 伺服器 (2308/6669) 抗跌"
        gold = "🟢 利多 — 避險買盤入場"
        oil = "中性偏空 — 需求預期下調"
        usd = "🟢 利多 — 風險規避強化"
        bonds = "🟢 利多 — 殖利率下行"
        long_play = "黃金 GLD ETF / 國防 (LMT/RTX) 等開盤拉回到 5MA 接, 停損 -3% 目標 +5-8%"
        short_play = "半導體 (NVDA/AMD/2330/3711) 反彈到 5MA 空, 跌破今日低追空, 停損 +3% 目標 -5%"
        pos_advice = "半導體部位減碼 50% 或全停損; 黃金 / 國防 / 公用事業 抱緊; 出口導向小型股先出場"
        risk_alert = "若 48hr 內法院禁制令暫停或川普口頭軟化, 則翻盤; 注意中國對等公告"
    # === Rule 2: 半導體出口管制 → SOX 短空 + AI 基建受惠 ===
    elif any(k in all_titles for k in ["chip export", "semiconductor restrict", "ai chip", "tech ban"]):
        headline = "🔴 半導體出口管制, AI 鏈短壓"
        us_imp = "半導體 (NVDA/AMD/ASML/MU) 短壓; AI 軟體 (PLTR/CRM) 受惠; 國防 (LMT) 中性偏多"
        tw_imp = "台積電 (2330) / 聯電 (2303) 開低風險; ABF 載板 (3037) + 重電 (2308) 抗跌"
        gold = "🟢 利多 — 地緣風險"
        oil = "中性"
        usd = "🟢 利多"
        bonds = "中性偏多"
        long_play = "AI 軟體 (PLTR/CRM) + 重電 (2308/6669) 突破今日高追多, 停損 -3% 目標 +5%"
        short_play = "半導體 (NVDA/2330) 開低不過盤中高就追空, 停損 +3% 目標 -5%"
        pos_advice = "半導體大型股 (NVDA/2330/2454) 減碼 50%, 設停損; AI 應用 + 國防 抱緊"
        risk_alert = "若政策細節溫和或排除盟友, 則反彈; 留意 ASML 評論"
    # === Rule 3: 減稅 / 鬆綁 → 美股↑, 金融/能源/小型股 (排除 Fed/Powell 場景, 由 Rule 5 處理) ===
    elif any(k in all_titles for k in ["tax cut", "deregulat", "減稅"]) and not any(k in all_titles for k in ["fed", "powell", "federal reserve"]):
        headline = "🟢 減稅 / 鬆綁政策, 短期偏多"
        us_imp = "金融 (XLF/JPM) + 能源 (XLE/XOM) + 小型股 (IWM) 受惠; 公用事業 (XLU) 跑輸"
        tw_imp = "金融 (2891/2882) + 觀光/百貨 (2912/2731) 受惠; 防禦型股 (公用/食品) 跑輸"
        gold = "🔴 利空 — 風險偏好提升"
        oil = "🟢 利多 — 經濟預期改善"
        usd = "🟢 利多 — 經濟強"
        bonds = "🔴 利空 — 殖利率上行"
        long_play = "金融 (XLF/JPM) + 能源 (XLE) + 小型股 (IWM) 等開盤後 +1% 突破追多, 停損 -3% 目標 +5%"
        short_play = "公用事業 (XLU/2412) + REITs 反彈到 5MA 短空, 停損 +3% 目標 -5%"
        pos_advice = "金融 / 能源 / 小型股 加碼; 公用事業 / 防禦型 減碼"
        risk_alert = "若法案被擱置或縮水, 則翻盤; 留意參議院票數"
    # === Rule 4: 制裁 / 中俄 / 地緣 → 原油↑, 國防↑, 中概↓ ===
    elif any(k in all_titles for k in ["sanction", "russia", "iran", "war", "military"]):
        headline = "🟡 地緣政治升溫, 國防/原油受惠"
        us_imp = "國防 (LMT/RTX/NOC) + 能源 (XOM/CVX) 受惠; 中概股 (BABA/BIDU) 短壓"
        tw_imp = "鋼鐵 (2002) / 國防相關 (2049) 受惠; 中國市場曝險高的電子股 (2317/2382) 短壓"
        gold = "🟢 利多 — 避險"
        oil = "🟢 利多 — 供給疑慮"
        usd = "🟢 利多"
        bonds = "🟢 利多 — 避險"
        long_play = "國防 (LMT/RTX) + 能源 (XOM) + 黃金 (GLD) 突破前高追多, 停損 -3% 目標 +5-8%"
        short_play = "中概 (BABA/BIDU) + 中國曝險電子 反彈短空, 停損 +3% 目標 -5%"
        pos_advice = "國防 / 能源 / 黃金 加碼; 中國市場曝險高股票 (2317/2382) 減碼 30%"
        risk_alert = "若停火協議或制裁解除, 國防/原油 反向; 留意聯合國公告"
    # === Rule 5: Fed / Powell 壓力 ===
    elif any(k in all_titles for k in ["fed pressure", "powell", "rate cut", "federal reserve"]):
        headline = "🟡 Fed 政策壓力, 利率敏感股波動"
        us_imp = "金融 (XLF) + 房地產 (XLRE) + 小型股 (IWM) 利率敏感; 大型科技 (QQQ) 受惠 (若鴿派)"
        tw_imp = "金融 (2891/2882) + 房產建商 (2545) 利率敏感; 高息股 (00878) 受惠"
        gold = "🟢 利多 (若鴿派)"
        oil = "中性"
        usd = "🔴 利空 (若鴿派)"
        bonds = "🟢 利多 (殖利率下行)"
        long_play = "高息 ETF (00878/2412) 拉回到 5MA 接; 大型科技 (QQQ) 突破追多, 停損 -3%"
        short_play = "金融 (XLF/2891) 若殖利率快速下行則短空, 停損 +3% 目標 -5%"
        pos_advice = "高息 / 公用事業 抱緊; 銀行股留意殖利率變動"
        risk_alert = "Powell 公開回應將定調; CPI 公布前先減倉"

    return {
        "headline": headline,
        "us_impact": us_imp,
        "tw_impact": tw_imp,
        "global_impact": {"gold": gold, "oil": oil, "usd": usd, "us_bonds": bonds},
        "long_play": long_play,
        "short_play": short_play,
        "position_advice": pos_advice,
        "risk_alert": risk_alert,
        "_source": "rule_based",
    }


def analyze_with_gemini(alerts: List[Dict]) -> Dict:
    """用 Gemini 結構化分析: 對美股 / 台股 / 全球商品 影響 + 操作建議.
    若 Gemini 不可用或失敗, fallback 到 _rule_based_action.
    """
    if not alerts:
        return {}
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            print("[trump_policy] gemini unavailable, using rule-based fallback", flush=True)
            return _rule_based_action(alerts)
        # 整理新聞文字
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
            '  "long_play": "多單怎麼操作: 哪個族群/個股, 進場時機, 停損 -3%, 目標 +5-8% (1 句)",\n'
            '  "short_play": "空單怎麼操作: 哪個族群/個股, 進場時機, 停損 +3%, 目標 -5% (1 句)",\n'
            '  "position_advice": "對現有持倉的處置 (1 句)",\n'
            '  "risk_alert": "本次最大風險點 + 何時失效 (1 句)"\n'
            "}\n\n"
            "判斷準則:\n"
            "- 關稅 / 貿易戰 → 黃金 ↑, 美元 ↑, 半導體/出口股壓力\n"
            "- 減稅 / 鬆綁 → 美股 ↑, 金融/能源/小型股受惠\n"
            "- 制裁中俄 → 原油 ↑, 國防股 ↑, 中概股 ↓\n"
            "- 半導體出口管制 → SOX 短空, ASML/AMD 衝擊\n"
        )
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None:
            print("[trump_policy] gemini model None, using rule-based fallback", flush=True)
            return _rule_based_action(alerts)
        resp = model.generate_content(prompt)
        raw = (resp.text or "").strip() if resp else ""
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:].strip()
        import json as _json
        data = _json.loads(raw)
        if not isinstance(data, dict) or not data:
            print("[trump_policy] gemini parse failed, using rule-based fallback", flush=True)
            return _rule_based_action(alerts)
        data["_source"] = "gemini"
        return data
    except Exception as e:
        print(f"[trump_policy] gemini fail: {e}, using rule-based fallback", flush=True)
        return _rule_based_action(alerts)


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
        tpa["sent_hashes"] = sent[-200:]
        tpa["last_sent_ts"] = dt.datetime.utcnow().isoformat()
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
