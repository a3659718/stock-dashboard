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


def build_morning_recap_msg() -> str:
    """組裝 morning recap TG HTML."""
    lines = ["☀️ <b>早安!昨夜美股重點</b>"]
    lines.append("")

    # 1. 美股大盤
    snaps: List[str] = []
    fetch_attempted = 0
    for sym, label, flag in [
        ("SPY", "SPY", "🇺🇸"),
        ("QQQ", "QQQ", "🇺🇸"),
        ("^SOX", "費半", "🟦"),
        ("^IXIC", "那指", "🟪"),
    ]:
        fetch_attempted += 1
        s = _fetch_us_close(sym)
        if s:
            tag = "🟢" if s.get("pct", 0) >= 0 else "🔴"
            snaps.append(f"{tag} {label} {s['pct']:+.2f}%")
    lines.append("📊 <b>大盤</b>")
    if snaps:
        lines.append("  " + " · ".join(snaps))
    else:
        # BUG FIX: 之前 yfinance 全 fail 時整段都不顯示 → 用戶以為推播壞了
        # 改成明確顯示「資料暫時無法取得」+ 建議
        lines.append("  <i>⚠️ 大盤資料暫時無法取得 (yfinance API 異常或週末)</i>")
        lines.append("  <i>請從 dashboard 或券商 app 確認</i>")
    lines.append("")

    # 2. 推播統計
    recap_lines = _summarize_push_history()
    if recap_lines:
        lines.append("📱 <b>昨夜推播</b>")
        for r in recap_lines:
            lines.append(f"  • {r}")
        lines.append("")

    # 3. 今日台股盤前提醒
    lines.append("🇹🇼 <b>今日台股盤前 (08:30)</b> 會有完整盤前 + BUY Top 5")
    lines.append("")
    lines.append("<i>※ 早安推播 — 一封看完昨夜重點. 詳細請翻過往推播.</i>")

    return "\n".join(lines)


def check_and_push() -> Dict:
    """給 cron 呼叫. 回 {ok, sent, msg_len, error}."""
    try:
        import notifier
        msg = build_morning_recap_msg()
        if not msg:
            return {"ok": False, "sent": False, "error": "empty msg"}
        ok, info = notifier.send_message(msg, disable_preview=True)
        print(f"[morning_recap] sent: ok={ok} info={info}", flush=True)
        return {"ok": ok, "sent": ok, "msg_len": len(msg), "info": info}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "sent": False, "error": str(e)}
