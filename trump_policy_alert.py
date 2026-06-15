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
TRUMP_DAILY_CAP = 3  # 6→4→3 (用戶要求再降, 一天絕對上限 3 則)
TRUMP_MAX_AGE_HR = 12  # 48→12 (新聞超過 12 小時就視為舊聞, 不推)

# 白名單來源 — 只信高品質媒體 + 官方
TRUMP_PUBLISHER_WHITELIST = [
    # 主流通訊社
    "reuters", "bloomberg", "associated press", "ap news",
    # 美國主流財經/政治媒體
    "wall street journal", "wsj", "new york times", "nytimes",
    "washington post", "cnbc", "cnn", "bbc",
    # 財經專業
    "financial times", "ft", "marketwatch", "barron's", "barrons",
    "yahoo finance",  # Yahoo 自家編輯內容 (非 SeekingAlpha 轉貼)
    "investor's business daily", "investing.com",
    # 官方
    "white house", "whitehouse", "treasury", "federal reserve",
    "department of state",
    # 推文/聲明直連
    "truth social", "twitter", "x.com",
]

# 黑名單來源 — 明確拒絕 (分析文/部落格/低品質轉貼)
TRUMP_PUBLISHER_BLACKLIST = [
    "seeking alpha", "seekingalpha", "zacks", "motley fool",
    "thestreet", "benzinga", "simply wall st", "insider monkey",
    "247wallst", "247 wall st", "tipranks",
]

# 關鍵 universe (川普推文常影響的股) — 已移除加密相關 (MSTR/COIN/DJT)
TRUMP_SENSITIVE_UNIVERSE = [
    "DELL", "TSLA", "NVDA", "LMT", "RTX", "XOM", "CVX",
    "X",
]


def _is_publisher_allowed(publisher: str) -> bool:
    """白名單通過 + 黑名單未命中 = 允許.

    Bug fix: 過去任何來源都收 → "Dell 在 Zacks 的分析" 也會被推 → 假陽性.
    """
    if not publisher:
        return False  # 沒 publisher 訊息, 偏保守 reject (避免不明來源)
    p = publisher.lower()
    # 黑名單先擋
    if any(b in p for b in TRUMP_PUBLISHER_BLACKLIST):
        return False
    # 白名單通過
    return any(w in p for w in TRUMP_PUBLISHER_WHITELIST)


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
    # I: 過濾統計 (給推播末段顯示, 讓用戶知道過濾效果)
    filter_stats = {
        "scanned": 0,        # 掃描總數
        "filtered_keyword": 0,  # 因 keyword 不足擋掉
        "filtered_publisher": 0,  # 因不在白名單擋掉
        "filtered_age": 0,    # 因超過 12 小時擋掉
        "filtered_dedup": 0,  # 因已推過擋掉
        "passed": 0,
    }
    # 1. Finnhub general news
    try:
        import data_sources as ds
        news = ds.fetch_finnhub_news(category="general", max_n=30) or []
        filter_stats["scanned"] += len(news)
        for n in news:
            if not isinstance(n, dict):
                continue
            head = n.get("headline") or n.get("title") or ""
            url = n.get("url") or n.get("link") or ""
            src = n.get("source") or "Finnhub"
            ts = n.get("datetime")
            pub_ts = None
            pub_date = ""
            if ts and isinstance(ts, (int, float)):
                try:
                    pub_ts = dt.datetime.fromtimestamp(int(ts))
                    pub_date = pub_ts.strftime("%Y-%m-%d")
                except Exception:
                    pass
            hits = _match_trump_keywords(head)
            # 提高門檻: 至少 3 個關鍵字才推 (避免泛 trump 新聞淹沒)
            if len(hits) < 3:
                filter_stats["filtered_keyword"] += 1
                continue
            # Bug fix (白名單): 來源不在白名單就跳過
            if not _is_publisher_allowed(src):
                filter_stats["filtered_publisher"] += 1
                continue
            h_hash = _hash_title(head)
            if h_hash in alerted:
                filter_stats["filtered_dedup"] += 1
                continue
            # Bug fix (時效): 過濾 > TRUMP_MAX_AGE_HR 舊新聞
            if pub_ts is not None:
                hrs = (now_utc - pub_ts).total_seconds() / 3600
                if hrs > TRUMP_MAX_AGE_HR:
                    filter_stats["filtered_age"] += 1
                    continue
            filter_stats["passed"] += 1
            all_news.append({
                "symbol": "GENERAL",
                "title": head,
                "link": url,
                "publisher": src,
                "date": pub_date,
                "keywords": hits,
                "h_hash": h_hash,
                "pub_ts": pub_ts.isoformat() if pub_ts else None,
                "_sort_ts": pub_ts.timestamp() if pub_ts else 0,
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
            filter_stats["scanned"] += len(yh)
            for n in yh:
                if not isinstance(n, dict):
                    continue
                head = n.get("title") or ""
                hits = _match_trump_keywords(head)
                # Bug fix (準確性): Yahoo News 也要 ≥3 keywords (跟 Finnhub 一致)
                if len(hits) < 3:
                    filter_stats["filtered_keyword"] += 1
                    continue
                # Bug fix (白名單): publisher 不在白名單就跳過
                publisher = n.get("publisher") or ""
                if not _is_publisher_allowed(publisher):
                    filter_stats["filtered_publisher"] += 1
                    continue
                h_hash = _hash_title(head)
                if h_hash in alerted:
                    filter_stats["filtered_dedup"] += 1
                    continue
                # Bug fix (時效): 只收近 TRUMP_MAX_AGE_HR 小時內的新聞
                # 解析 publish date — yfinance 回 "2025-12-01" or 'M d, YYYY' 或 int
                pub_ts = None
                raw_date = n.get("date") or n.get("publish_date") or n.get("providerPublishTime")
                if isinstance(raw_date, (int, float)):
                    try:
                        pub_ts = dt.datetime.fromtimestamp(int(raw_date))
                    except Exception:
                        pass
                elif isinstance(raw_date, str) and raw_date:
                    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y"):
                        try:
                            pub_ts = dt.datetime.strptime(raw_date[:19], fmt)
                            break
                        except Exception:
                            continue
                # 過濾舊新聞 (> TRUMP_MAX_AGE_HR 小時)
                if pub_ts is not None:
                    hrs = (now_utc - pub_ts).total_seconds() / 3600
                    if hrs > TRUMP_MAX_AGE_HR:
                        filter_stats["filtered_age"] += 1
                        continue
                filter_stats["passed"] += 1
                all_news.append({
                    "symbol": sym,
                    "title": head,
                    "link": n.get("link") or "",
                    "publisher": n.get("publisher") or "Yahoo",
                    "date": n.get("date") or "",
                    "keywords": hits,
                    "h_hash": h_hash,
                    "pub_ts": pub_ts.isoformat() if pub_ts else None,
                    "_sort_ts": pub_ts.timestamp() if pub_ts else 0,
                })
    except Exception as e:
        print(f"[trump_policy] yahoo fail: {e}", flush=True)

    # I: 記錄過濾統計 (給 fmt_trump_policy_alerts 顯示)
    if all_news:
        # 把 stats 附加到第一筆 (作為 batch-level meta)
        all_news[0]["_filter_stats"] = filter_stats
    # 印 log (給 GH Actions log 看)
    print(f"[trump_policy] filter stats: scanned={filter_stats['scanned']} "
          f"keyword={filter_stats['filtered_keyword']} "
          f"publisher={filter_stats['filtered_publisher']} "
          f"age={filter_stats['filtered_age']} "
          f"dedup={filter_stats['filtered_dedup']} "
          f"passed={filter_stats['passed']}", flush=True)
    if not all_news:
        return []
    # Bug fix (排序): 按發布時間排序 (newest first), 不是按 universe 順序
    all_news.sort(key=lambda x: x.get("_sort_ts", 0), reverse=True)
    # daily cap 已在 cooldown 前檢查 (使用 daily_count[today]), remaining 已算好
    max_this_batch = min(TRUMP_MAX_PER_BATCH, remaining)
    return all_news[:max_this_batch]


def _rule_based_action(alerts: List[Dict]) -> Dict:
    """規則式操作建議 — Gemini 不可用時 fallback. 根據新聞關鍵字推導.

    Bug fix: 不再給「視政策細節而定」「全部中性」這種廢話, 即使沒命中精確 rule,
    也根據新聞情緒 (川普推文多偏激進) 給明確方向.
    """
    if not alerts:
        return {}
    all_kw = set()
    all_titles = " ".join((a.get("title") or "") for a in alerts).lower()
    for a in alerts:
        for k in (a.get("keywords") or []):
            all_kw.add(k.lower())

    # === 預設值: 川普推文 95% 偏激進 → 通膨預期 + 政策不確定性 ===
    # Bug fix: 美債從「利多」改「利空」(川普強硬 → 通膨/赤字預期 → 殖利率↑ → 美債利空)
    headline = "🟡 川普政策動向, 短線波動加大"
    us_imp = "風險資產短壓 (大型科技 / 半導體), 防禦型 (公用 / 高息) 抗跌"
    tw_imp = "權值股 (2330/2317) 短壓, 內需 / 高股息 (00878/2412) 抗跌"
    gold = "🟢 利多 — 政策不確定性 → 避險買盤"
    oil = "🟡 中性偏多 — 政策可能影響供給"
    usd = "🟢 利多 — 政策強硬 → 美元短強"
    bonds = "🔴 利空 — 通膨/赤字預期 → 殖利率上行"  # bug fix
    long_play = "高息 ETF (00878/SCHD) + 黃金 (GLD) 拉回到 5MA 接, 停損 -3% 目標 +5%"
    short_play = "權值科技 (NVDA/2330) 反彈到 5MA 短空, 停損 +3% 目標 -5%; 長天期美債 (TLT) 高位空"
    pos_advice = "半導體 / 中概減碼 30%; 黃金 / 高息 / 公用事業 抱緊; 長債部位減碼"
    risk_alert = "若 24hr 內川普政策軟化或被法院擋下, 則反向; 留意對等報復"

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
        gold = "🟢 利多 — 鴿派預期 + 美元走弱"
        oil = "🟡 中性偏多 — 鴿派 → 需求預期改善"
        usd = "🔴 利空 — 若鴿派 (殖利率下行)"
        bonds = "🟢 利多 — 殖利率下行"
        long_play = "高息 ETF (00878/2412) 拉回到 5MA 接; 大型科技 (QQQ) 突破追多, 停損 -3%"
        short_play = "金融 (XLF/2891) 若殖利率快速下行則短空, 停損 +3% 目標 -5%"
        pos_advice = "高息 / 公用事業 抱緊; 銀行股留意殖利率變動"
        risk_alert = "Powell 公開回應將定調; CPI 公布前先減倉"
    # === Rule 6: 烏俄/中東 戰爭升溫 ===
    elif any(k in all_titles for k in ["ukraine", "putin", "kremlin", "israel", "gaza", "hezbollah", "houthi", "missile strike", "drone strike"]):
        headline = "🔴 戰爭升溫, 避險資產大漲"
        us_imp = "國防 (LMT/RTX/NOC/GD) + 能源 (XOM/CVX) + 黃金 (GLD) 強勢; 航空/觀光 短壓"
        tw_imp = "鋼鐵 (2002) + 國防 (2049) 受惠; 出口/航運 (2603/2615) 短壓 (運輸成本)"
        gold = "🟢🟢 強烈利多 — 戰爭避險買盤湧入"
        oil = "🟢🟢 強烈利多 — 供給疑慮 + 制裁俄油"
        usd = "🟢 利多 — 避險主流貨幣"
        bonds = "🟢 利多 — 避險買盤"
        long_play = "國防 (LMT/RTX) + 能源 (XOM) + 黃金 (GLD/IAU) 突破前高追多, 停損 -3% 目標 +8%"
        short_play = "航空 (AAL/DAL) + 旅遊 (BKNG) 反彈短空, 停損 +3% 目標 -5%"
        pos_advice = "立即加碼國防 / 能源 / 黃金; 出口導向股 (台股航運/觀光) 減碼 30%"
        risk_alert = "若 24hr 內停火傳出, 避險資產急跌; 留意聯合國 / 北約緊急會議"
    # === Rule 7: 外交 / 峰會 / 和平協議 ===
    elif any(k in all_titles for k in ["summit", "diplomatic", "peace deal", "agreement", "treaty", "talks"]):
        headline = "🟢 外交緩和訊號, 風險偏好回升"
        us_imp = "風險資產回升 (QQQ/SPY 大型科技), 國防/避險 短壓 (LMT/GLD); 跨國企業受惠"
        tw_imp = "權值科技 (2330/3711) 拉回後回升, 內需消費 (2912) 抗跌; 國防股短壓"
        gold = "🔴 利空 — 避險買盤撤出"
        oil = "🔴 利空 — 供給疑慮緩解"
        usd = "🟡 中性偏弱 — 避險溢價下降"
        bonds = "🔴 利空 — 殖利率上行 (避險賣出)"
        long_play = "權值科技 (NVDA/QQQ/2330) 拉回到 5MA 接, 停損 -3% 目標 +5%"
        short_play = "黃金 (GLD) + 國防 (LMT) 高位短空, 停損 +3% 目標 -5%"
        pos_advice = "避險部位 (黃金/國防) 減碼 30%; 風險資產 (科技/小型股) 加碼"
        risk_alert = "若協議破局或重啟衝突, 反向; 留意執行細節"
    # === Rule 8: 監管 / 反壟斷 / 反 Big Tech ===
    elif any(k in all_titles for k in ["antitrust", "regulation", "doj", "ftc", "big tech", "breakup", "monopoly"]):
        headline = "🔴 監管壓力, Big Tech 短壓"
        us_imp = "GOOGL/META/AMZN/MSFT 短壓; 小型股 (IWM) + 中型科技反受惠; 金融 (XLF) 中性"
        tw_imp = "依賴美國雲端的台廠 (2330 美國客戶比重高) 短壓; 純內需 (2912/2330 中性)"
        gold = "🟡 中性偏多 — 不確定性提升"
        oil = "🟡 中性"
        usd = "🟡 中性"
        bonds = "🟢 利多 — 風險規避"
        long_play = "中小型科技 (IWM/PLTR/SNOW) + 半導體 (SMH 拉回) 接, 停損 -3% 目標 +5%"
        short_play = "GOOGL / META / AMZN 反彈到 5MA 短空, 停損 +3% 目標 -5%"
        pos_advice = "Big Tech 部位減碼 30-50%; 中小型科技 / 純消費 加碼"
        risk_alert = "若法院駁回或政策軟化, 反向; 留意 FTC / DOJ 公告時間"
    # === Rule 9: 移民 / 邊境 / 勞動力政策 ===
    elif any(k in all_titles for k in ["deportation", "immigration", "border", "ice raid", "labor"]):
        headline = "🟡 移民政策, 服務/農業/建築短壓"
        us_imp = "農業 (DE/AGCO) + 建築 (CAT/DHI) + 餐飲 (CMG/MCD) 勞動力成本上升; 自動化 (ABBV) 受惠"
        tw_imp = "出口導向受美國消費降溫拖累 (2317/2382); 內需 (2912) 中性"
        gold = "🟡 中性偏多 — 通膨預期"
        oil = "🟡 中性"
        usd = "🟢 利多 — 經濟強硬"
        bonds = "🔴 利空 — 通膨預期上升"
        long_play = "自動化 / 機器人 (ROBO/2308) 突破追多, 停損 -3% 目標 +5%"
        short_play = "餐飲 (CMG/MCD) + 建築 (DHI) 反彈短空, 停損 +3% 目標 -5%"
        pos_advice = "勞力密集型 (餐飲/農業/建築) 減碼; 自動化 / 機器人加碼"
        risk_alert = "若聯邦法院禁制令暫停, 反向; 留意執法時程"
    # === Rule 10: 能源政策 / 石油 ===
    elif any(k in all_titles for k in ["drill", "energy independence", "exxon", "fossil fuel", "epa rollback"]):
        headline = "🟢 能源鬆綁 / 化石燃料友好"
        us_imp = "油氣 (XOM/CVX/SLB) + 油田服務 (HAL) 受惠; 太陽能 (FSLR/ENPH) + 電動車 (TSLA) 短壓"
        tw_imp = "傳產化工 / 塑化 (1301/1303) 受惠; 綠能 (1503/1504) 短壓"
        gold = "🟡 中性偏多 — 通膨預期"
        oil = "🟢🟢 強烈利多 — 供給增加但成本下降, 油商獲利改善"
        usd = "🟢 利多"
        bonds = "🔴 利空 — 通膨預期"
        long_play = "油氣 (XOM/CVX) + 鑽井 (HAL) 突破追多, 停損 -3% 目標 +8%"
        short_play = "太陽能 (FSLR/ENPH) + EV (TSLA/RIVN) 反彈短空, 停損 +3% 目標 -5%"
        pos_advice = "油氣 / 化工 加碼; 太陽能 / EV 減碼"
        risk_alert = "若 OPEC+ 反向減產, 油價回檔; 留意 OPEC 月會"

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


def _extract_direction(text: str) -> str:
    """從文字提取「利多/利空/中性」方向.

    用於 cross-validation: 從 Gemini 或 rule 的字串 (含 emoji) 抓出方向.
    """
    if not text:
        return "unknown"
    t = str(text)
    # emoji 優先 (rule_based 用 emoji)
    if "🟢" in t: return "bullish"
    if "🔴" in t: return "bearish"
    if "🟡" in t: return "neutral"
    # 文字判斷 (Gemini 用文字)
    t_low = t.lower()
    if any(k in t_low for k in ["利多", "偏多", "看多", "bullish", "positive"]):
        return "bullish"
    if any(k in t_low for k in ["利空", "偏空", "看空", "bearish", "negative"]):
        return "bearish"
    if any(k in t_low for k in ["中性", "neutral", "震盪"]):
        return "neutral"
    return "unknown"


def _cross_validate(gemini_result: Dict, rule_result: Dict) -> Dict:
    """比對 Gemini vs Rule 對 gold/oil/usd/bonds 方向是否一致.

    回:
      {agreement_rate: 0.0-1.0, disagreements: [field, ...], note: str}

    一致率 ≥ 75% → confidence_tag = "✓ 規則確認"
    一致率 < 50% → confidence_tag = "⚠️ Gemini vs 規則分歧, 操作保守"
    """
    if not gemini_result or not rule_result:
        return {"agreement_rate": 0.0, "disagreements": [], "note": "", "confidence_tag": ""}
    fields = ["gold", "oil", "usd", "us_bonds"]
    agreements = 0
    total = 0
    disagreements = []
    g_global = gemini_result.get("global_impact") or {}
    for f in fields:
        # Gemini field 在 global_impact 內
        g_text = g_global.get(f) or g_global.get(f.replace("us_", ""))
        # Rule field 在 top level
        rule_f = "bonds" if f == "us_bonds" else f
        r_text = rule_result.get(rule_f)
        if not g_text or not r_text:
            continue
        g_dir = _extract_direction(g_text)
        r_dir = _extract_direction(r_text)
        if g_dir == "unknown" or r_dir == "unknown":
            continue
        total += 1
        if g_dir == r_dir:
            agreements += 1
        else:
            disagreements.append(f"{f} (Gemini={g_dir}, Rule={r_dir})")
    rate = agreements / total if total > 0 else 0.0
    if rate >= 0.75:
        tag = "✓ 規則確認 (一致率 {:.0%})".format(rate)
    elif rate < 0.5 and total >= 2:
        tag = "⚠️ Gemini vs 規則分歧 ({:.0%}), 操作保守".format(rate)
    else:
        tag = ""
    return {
        "agreement_rate": rate,
        "disagreements": disagreements,
        "total_checked": total,
        "confidence_tag": tag,
    }


def analyze_with_gemini(alerts: List[Dict]) -> Dict:
    """用 Gemini 結構化分析: 對美股 / 台股 / 全球商品 影響 + 操作建議.

    Hybrid 3 層:
      1. Filter: rule-based 已 filter alerts (白名單/時效/keyword)
      2. Consensus: Gemini + rule 同時跑, cross-validate 一致性
      3. Fallback: Gemini 失敗 → rule-based

    若 Gemini 不可用或失敗, fallback 到 _rule_based_action.
    """
    if not alerts:
        return {}
    # 先跑 rule (永遠有, 當基準)
    rule_result = _rule_based_action(alerts)

    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            print("[trump_policy] gemini unavailable, using rule-based fallback", flush=True)
            return rule_result
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
            '  "headline": "用一句話總結這些政策的核心方向 + 是利多還是利空 (15 字內)",\n'
            '  "us_impact": "對美股影響 (1 句)",\n'
            '  "tw_impact": "對台股影響 (1 句)",\n'
            '  "global_impact": {\n'
            '    "gold": "對黃金影響 (利多/利空/中性 + 原因)",\n'
            '    "oil": "對原油影響",\n'
            '    "usd": "對美元影響",\n'
            '    "us_bonds": "對美債殖利率影響"\n'
            "  },\n"
            '  "long_play": "多單怎麼操作",\n'
            '  "short_play": "空單怎麼操作",\n'
            '  "position_advice": "對現有持倉的處置",\n'
            '  "risk_alert": "本次最大風險點 + 何時失效"\n'
            "}\n"
        )
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None:
            return rule_result
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip() if resp else ""
        if not text:
            return rule_result
        # 解析 JSON
        import re as _re, json as _json
        m = _re.search(r"\{[\s\S]*\}", text)
        gemini_result = {}
        if m:
            try:
                gemini_result = _json.loads(m.group(0))
            except Exception:
                gemini_result = {"raw_text": text}
        if not gemini_result:
            return rule_result

        # === Cross-validate: Gemini vs Rule 對方向一致性 ===
        cross = _cross_validate(gemini_result, rule_result)
        gemini_result["_cross_validation"] = cross
        gemini_result["_rule_baseline"] = rule_result
        return gemini_result
    except Exception as e:
        print(f"[trump_policy] gemini fail: {e}", flush=True)
        return rule_result


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
        tpa["last_sent_ts"] = dt.datetime.now(dt.timezone.utc).isoformat()
        today = dt.date.today().strftime("%Y-%m-%d")
        daily = tpa.setdefault("daily_count", {})
        if not isinstance(daily, dict):
            daily = {}
            tpa["daily_count"] = daily
        daily[today] = int(daily.get(today, 0)) + len(alerts)
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[trump_policy] mark_alerts_sent fail (non-fatal): {e}", flush=True)
