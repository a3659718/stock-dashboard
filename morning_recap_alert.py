"""
morning_recap_alert.py — 早安推播 (07:30 TPE)

每天起床 1 封, 抓昨夜美股 + 重要推播統計 + Gemini 一句話建議.
解決問題: 用戶睡覺時推了很多, 醒來想一目了然.

API:
  build_morning_recap_msg() -> str   # TG HTML
  check_and_push() -> Dict           # 直接送 TG (給 cron 用)
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Dict, List


def _fetch_us_close(symbol: str) -> Dict:
    """抓最近一個交易日 close + change vs 前一日."""
    try:
        import data_sources as ds
        df = ds.fetch_yf_history(symbol, period="5d", interval="1d")
        if df is None or df.empty or len(df) < 2:
            return {}
        c = df["Close"].astype(float)
        last = float(c.iloc[-1])
        prev = float(c.iloc[-2])
        pct = (last / prev - 1) * 100 if prev > 0 else 0
        return {"last": round(last, 2), "pct": round(pct, 2)}
    except Exception as e:
        print(f"[morning_recap] fetch {symbol} fail: {e}", flush=True)
        return {}


def _summarize_push_history() -> List[str]:
    """從 push_history 抓過去 14 hr 推播 (約美股一場).

    回傳分類統計 list, e.g. ["反轉 2 則", "新聞 3 則", "強勢股 1 則"]
    """
    try:
        import watchlist_store
        state = watchlist_store.load_monitor_state() or {}
        hist = state.get("push_history") or []
        if not isinstance(hist, list):
            return []
        now_utc = dt.datetime.now(dt.timezone.utc)
        cutoff = now_utc - dt.timedelta(hours=14)
        recent = []
        for h in hist:
            try:
                ts_str = h.get("ts", "")
                ts = dt.datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                if ts >= cutoff and h.get("ok"):
                    recent.append(h)
            except Exception:
                continue
        if not recent:
            return ["(過去 14 小時無推播)"]
        # 分類聚合 — 把多種 push_type 歸成幾個大類
        cat_map = {
            "us_open": "🇺🇸 美股盤推",
            "us_mid": "🇺🇸 美股盤推",
            "us_close": "🇺🇸 美股盤推",
            "us_buy_picks": "🇺🇸 美股盤推",
            "intraday_reversal": "🔄 反轉警報",
            "weak_open": "📉 開盤即弱",
            "strong_open": "📈 開盤即強",
            "systemic_crash": "🚨 系統大跌",
            "strong_stock": "🚀 強勢個股",
            "volume_breakout": "⚡ 量爆",
            "chip_anomaly": "🏦 籌碼",
            "news_event": "📰 新聞事件",
            "analyst_insider": "📊 分析師",
            "trump_policy": "🇺🇸 川普政策",
            "weekend_recap": "🌐 週末摘要",
            "holiday_news": "📰 假日新聞",
        }
        counter: Counter = Counter()
        for h in recent:
            tp = str(h.get("type", "")).lower()
            label = cat_map.get(tp)
            if not label:
                # 模糊匹配
                for k, v in cat_map.items():
                    if k in tp:
                        label = v
                        break
            label = label or f"📤 {tp}"
            counter[label] += 1
        return [f"{lbl} {n} 則" for lbl, n in counter.most_common(8)]
    except Exception as e:
        print(f"[morning_recap] summarize fail: {e}", flush=True)
        return []


def _battle_tldr(sox_pct, ixic_pct) -> str:
    """今日台股一句話作戰結論 — 優先用 Gemini 潤飾, 失敗/無 quota 自動退回規則版。"""
    _rule = _battle_tldr_rule(sox_pct, ixic_pct)
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return _rule
        _sox = f"費半 {sox_pct:+.1f}%" if sox_pct is not None else "費半資料缺"
        _ix = f", 那指 {ixic_pct:+.1f}%" if ixic_pct is not None else ""
        prompt = (
            f"你是台股策略師。昨夜美股: {_sox}{_ix}。"
            "用繁體中文寫【一句話】給今日台股定調: 偏多還偏空開、最該留意哪個族群或方向。"
            "要具體可行動、40 字內、不要開場白或引號、只回那一句。"
        )
        from ai_analyzer import _get_model
        _m = _get_model()
        if _m is None:
            return _rule
        _resp = _m.generate_content(prompt)
        _txt = (_resp.text or "").strip() if _resp else ""
        _txt = _txt.replace("\n", " ").strip().strip('「」"\'` ')
        return _txt if _txt else _rule
    except Exception as _e:
        print(f"[morning_recap] gemini tldr fail: {_e}", flush=True)
        return _rule


def _battle_tldr_rule(sox_pct, ixic_pct) -> str:
    """規則版一句話 (Gemini 後備) — 依費半(台股先行指標)+ 那指 隔夜表現。"""
    if sox_pct is None:
        return "費半資料暫缺 → 開盤看量價再決定, 保守為宜"
    if sox_pct >= 2.0:
        return f"費半 {sox_pct:+.1f}% 大漲 → 台股半導體今日強力偏多開, 重點看權值(2330 / 2454)與 AI 供應鏈"
    if sox_pct >= 1.0:
        return f"費半 {sox_pct:+.1f}% → 台股偏多開, 電子 / 半導體較有表現空間"
    if sox_pct <= -2.0:
        return f"費半 {sox_pct:+.1f}% 大跌 → 台股偏空開, 留意權值拖累, 保守 / 減碼優先"
    if sox_pct <= -1.0:
        return f"費半 {sox_pct:+.1f}% 走弱 → 台股偏空開, 電子承壓, 觀望為宜"
    return f"費半 {sox_pct:+.1f}% 近平盤 → 台股方向不明, 開盤看量價與個股表現"


def build_morning_recap_msg() -> str:
    """組裝『今日作戰摘要』TG HTML — 一句話定調 + 昨夜美股 + 昨夜推播 + 命中回顧."""
    lines = ["☀️ <b>今日作戰摘要</b>", ""]

    # 抓美股大盤 (同時記下 pct 供一句話結論用)
    us_pct: Dict[str, float] = {}
    snaps: List[str] = []
    for sym, label in [("SPY", "SPY"), ("QQQ", "QQQ"), ("^SOX", "費半"), ("^IXIC", "那指")]:
        s = _fetch_us_close(sym)
        if s and s.get("pct") is not None:
            us_pct[sym] = s["pct"]
            tag = "🟢" if s["pct"] >= 0 else "🔴"
            snaps.append(f"{tag} {label} {s['pct']:+.2f}%")

    # 1. 一句話作戰結論 (置頂, 最醒目)
    lines.append(f"🧭 <b>一句話</b>:{_battle_tldr(us_pct.get('^SOX'), us_pct.get('^IXIC'))}")
    lines.append("")

    # 2. 昨夜美股大盤
    lines.append("📊 <b>昨夜美股</b>")
    if snaps:
        lines.append("  " + " · ".join(snaps))
    else:
        lines.append("  <i>⚠️ 大盤資料暫時無法取得 (yfinance API 異常或週末), 請從 dashboard / 券商 app 確認</i>")
    lines.append("")

    # 3. 昨夜推播統計
    recap_lines = _summarize_push_history()
    if recap_lines:
        lines.append("📱 <b>昨夜推播</b>")
        for r in recap_lines:
            lines.append(f"  • {r}")
        lines.append("")

    # 4. 推播命中回顧 (近 30 日勝率 → 建立信任感)
    try:
        import signal_tracker as _sig
        _s = _sig.accuracy_summary(None, lookback_days=30)
        _n = _s.get("n") or 0
        _pct = _s.get("pct")
        if _n >= 10 and _pct is not None:
            _mark = "🟢" if _pct >= 60 else ("🟡" if _pct >= 40 else "🔴")
            lines.append(f"🎯 <b>推播近 30 日勝率</b>:{_mark} {_pct:.0f}% (n={_n})")
            lines.append("")
    except Exception as _e:
        print(f"[morning_recap] signal_tracker fail: {_e}", flush=True)

    # 5. 今日台股盤前提醒
    lines.append("🇹🇼 <b>今日台股</b>:08:30 有完整盤前 (日韓即時 + 對台股影響) + BUY Top 5")
    lines.append("")
    lines.append("<i>※ 一封定調今日作戰;盤中緊急訊號(反轉 / 大跌 / 急報)仍會即時推.</i>")

    return "\n".join(lines)


def check_and_push() -> Dict:
    """[已併入 08:02 晨報, no-op] 給 cron 呼叫. 回 {ok, sent, msg_len, error}.

    使用者反映早上 07:32(本模組)/08:02(morning_brief)/08:17/08:33(pre_market) 四封
    推播集中在 1 小時內、內容高度重疊 (都在講美股隔夜收盤). 本模組的獨有內容
    (一句話定調 / 昨夜推播統計 / 30 日勝率) 已併入 scripts/morning_brief.py 的
    _section_recap_and_tldr(), 這裡改成 no-op 不再獨立送出, 使用者從四封減為三封。
    build_morning_recap_msg() 仍保留 (dashboard 或手動需要完整版時可呼叫)。
    """
    print("[morning_recap] 已併入 08:02 晨報 (scripts/morning_brief.py), "
          "本次排程 no-op 不推播", flush=True)
    return {"ok": True, "sent": False, "skipped": "merged_into_morning_brief"}
