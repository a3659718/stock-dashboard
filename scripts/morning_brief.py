"""
scripts/morning_brief.py

每天早上 8:00 台北時間 (UTC 00:00) 推播到 Telegram 的晨報.

內容 (精簡, 控制在 < 4000 字元):
  1. 美股隔夜總結 (SPY/QQQ/DIA/SOX/NDX)
  2. 美股新聞 / 重大消息 (3-5 則)
  3. AI / 半導體相關新聞 (專門挑出來)
  4. 國際 / 總經 (Fed / 油價 / 美元等)
  5. 台股推薦買進 Top 5 (從 upside_screener)
  6. 今日盤前提醒

設計理念:
  - 一封訊息搞定, 不分多封 (避免 TG quota / 通知打擾)
  - 每個 section 用 emoji header + bullet list
  - 失敗 graceful skip (例如 yfinance 抓不到就 skip 該 section, 不整封失敗)

用法:
  # 手動跑
  python scripts/morning_brief.py
  # GitHub Actions 排程跑 (見 .github/workflows/morning_brief.yml)
"""
from __future__ import annotations

import datetime as dt
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data_sources as ds
import notifier


# Telegram 訊息上限 4096 chars (HTML)
MAX_TG_CHARS = 3800


def _safe(label: str, fn, default=""):
    """Run fn() and return its output, or default if it crashes."""
    try:
        return fn() or default
    except Exception as e:
        print(f"[morning_brief] {label} failed: {type(e).__name__}: {e}", flush=True)
        return default


# ---------------------------------------------------------------------------
# Section 0: 一句話定調 + 昨夜推播統計 + 30 日勝率
# (併自原本獨立的 07:32 morning_recap_alert 推播 — 使用者反映 07:32/08:02/08:17/
#  08:33 四封推播集中在 1 小時內、內容高度重疊, 都在講美股隔夜收盤. 把
#  morning_recap 的獨有內容 (一句話結論 / 推播統計 / 命中回顧) 併進這封晨報,
#  07:32 那個獨立排程改成 no-op 不再推播 (見 morning_recap_alert.check_and_push),
#  使用者從 4 封減為 3 封, 資訊不流失.)
# ---------------------------------------------------------------------------
def _section_recap_and_tldr() -> str:
    try:
        import morning_recap_alert as _mr
        lines = []
        sox = _mr._fetch_us_close("^SOX")
        ixic = _mr._fetch_us_close("^IXIC")
        tldr = _mr._battle_tldr(sox.get("pct"), ixic.get("pct"))
        lines.append(f"🧭 <b>一句話</b>:{tldr}")
        recap_lines = _mr._summarize_push_history()
        if recap_lines and recap_lines != ["(過去 14 小時無推播)"]:
            lines.append("📱 <b>昨夜推播</b>: " + " / ".join(recap_lines[:6]))
        try:
            import signal_tracker as _sig
            s = _sig.accuracy_summary(None, lookback_days=30)
            n = s.get("n") or 0
            pct = s.get("pct")
            if n >= 10 and pct is not None:
                mark = "🟢" if pct >= 60 else ("🟡" if pct >= 40 else "🔴")
                lines.append(f"🎯 <b>推播近 30 日勝率</b>:{mark} {pct:.0f}% (n={n})")
        except Exception:
            pass
        return "\n".join(lines)
    except Exception as e:
        print(f"[morning_brief] recap_and_tldr section failed: {e}", flush=True)
        return ""


# ---------------------------------------------------------------------------
# Section 1: 美股隔夜總結
# ---------------------------------------------------------------------------
def _section_us_overnight() -> str:
    """美股隔夜 + SOX + NDX 表現."""
    lines = ["🌎 <b>美股隔夜</b>"]
    symbols = [
        ("SPY", "S&P500"),
        ("QQQ", "Nasdaq100"),
        ("DIA", "Dow"),
        ("^SOX", "費半"),
        ("^IXIC", "Nasdaq"),
        ("^RUT", "Russell2k"),
    ]
    for sym, name in symbols:
        try:
            df = ds.fetch_yf_history(sym, period="5d", interval="1d")
            if df is None or df.empty or len(df) < 2:
                continue
            c = df["Close"].astype(float)
            last = float(c.iloc[-1])
            prev = float(c.iloc[-2])
            pct = (last / prev - 1) * 100
            arrow = "🟢" if pct > 0 else "🔴" if pct < 0 else "⚪"
            lines.append(f"  {arrow} {name}: {last:,.2f} ({pct:+.2f}%)")
        except Exception:
            continue
    # Fear & Greed
    try:
        fg = ds.fetch_fear_greed()
        if fg and fg.get("score") is not None:
            lines.append(f"  😱 Fear&Greed: {float(fg['score']):.0f} ({fg.get('rating', '?')})")
    except Exception:
        pass
    return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# Section 2: 板塊輪動 (今日強勢板塊 Top 3)
# ---------------------------------------------------------------------------
def _section_sector_rotation() -> str:
    try:
        sectors = ds.fetch_sector_rotation()
        if sectors is None or sectors.empty or "1d_%" not in sectors.columns:
            return ""
        s = sectors.sort_values("1d_%", ascending=False).head(3)
        lines = ["📊 <b>美股強勢板塊</b>"]
        for _, r in s.iterrows():
            sym = r.get("symbol", "")
            name = r.get("sector", "")
            p1 = r.get("1d_%", 0) or 0
            lines.append(f"  {sym} {name}: {p1:+.2f}%")
        return "\n".join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Section 3: AI / 半導體相關新聞 (從 SPY/QQQ/NVDA news pool 抽出題材命中的)
# ---------------------------------------------------------------------------
def _section_ai_news() -> str:
    try:
        # 抓 NVDA, AVGO, AMD, SMCI, PLTR, ARM, ASML 的新聞
        ai_symbols = ["NVDA", "AVGO", "AMD", "SMCI", "PLTR", "ARM", "ASML",
                       "GOOGL", "MSFT", "META"]
        ai_keywords = ["ai", "artificial intelligence", "chip", "semiconductor",
                        "gpu", "hbm", "data center", "llm", "openai", "anthropic",
                        "nvidia", "tsmc"]
        all_news = []
        seen = set()
        for sym in ai_symbols:
            news = ds.fetch_yahoo_news(sym, max_n=4)
            for n in news:
                title = (n.get("title") or "").strip()
                if not title or title in seen:
                    continue
                # 只留命中 AI keyword 的
                t_low = title.lower()
                if any(kw in t_low for kw in ai_keywords):
                    seen.add(title)
                    all_news.append(n)
        # 取最近 5 則
        all_news.sort(key=lambda x: x.get("providerPublishTime") or 0, reverse=True)
        if not all_news:
            return ""
        lines = ["🤖 <b>AI / 半導體新聞</b>"]
        for n in all_news[:5]:
            title = (n.get("title") or "")[:90]
            publisher = n.get("publisher", "")
            link = n.get("link")
            if link:
                lines.append(f"  • <a href=\"{link}\">{title}</a> <i>({publisher})</i>")
            else:
                lines.append(f"  • {title} <i>({publisher})</i>")
        return "\n".join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Section 4: 國際 / 總經 / 油價 / 美元 / FOMC
# ---------------------------------------------------------------------------
def _section_macro_news() -> str:
    try:
        macro_symbols = ["SPY", "DIA", "TLT"]
        macro_keywords = ["fed", "fomc", "inflation", "cpi", "interest rate", "rate cut",
                           "powell", "treasury", "yield", "tariff", "trade war",
                           "oil", "opec", "gold", "dollar"]
        all_news = []
        seen = set()
        for sym in macro_symbols:
            news = ds.fetch_yahoo_news(sym, max_n=6)
            for n in news:
                title = (n.get("title") or "").strip()
                if not title or title in seen:
                    continue
                t_low = title.lower()
                if any(kw in t_low for kw in macro_keywords):
                    seen.add(title)
                    all_news.append(n)
        all_news.sort(key=lambda x: x.get("providerPublishTime") or 0, reverse=True)
        if not all_news:
            return ""
        lines = ["🌐 <b>國際 / 總經</b>"]
        for n in all_news[:4]:
            title = (n.get("title") or "")[:90]
            publisher = n.get("publisher", "")
            link = n.get("link")
            if link:
                lines.append(f"  • <a href=\"{link}\">{title}</a> <i>({publisher})</i>")
            else:
                lines.append(f"  • {title} <i>({publisher})</i>")
        return "\n".join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Section 5: 台股推薦 Top 5 (從 upside_screener)
# ---------------------------------------------------------------------------
def _section_tw_picks() -> str:
    try:
        import upside_screener
        # 用 cache, 避免每天 8am 重複呼叫 (15 分鐘 cache + 排程必然 cache miss)
        result = upside_screener.run_upside_screen(
            market="all", max_stocks=120, use_cache=False,  # 排程 fresh
        )
        all_picks = result.get("all") or []
        if not all_picks:
            return ""
        lines = [f"🇹🇼 <b>台股推薦 Top 5</b> (掃描 {result.get('meta', {}).get('scanned', 0)} 檔)"]
        for i, p in enumerate(all_picks[:5], 1):
            sid = p.get("stock_id", "")
            name = p.get("name", "")
            cat = p.get("category", "")
            score = p.get("score", 0)
            cur = p.get("current", "—")
            upside = p.get("upside_pct", "—")
            lv = p.get("levels") or {}
            cat_zh = {
                "early_stage": "起漲", "momentum": "動能", "reversal": "反轉"
            }.get(cat, cat)
            line = f"  {i}. <b>{sid} {name}</b> [{cat_zh}] 分{score} 空間~{upside}%"
            if lv.get("entry_low") and lv.get("target") and lv.get("stop"):
                line += f"\n     進{lv['entry_low']}~{lv.get('entry_high')} 目{lv['target']} 損{lv['stop']}"
            lines.append(line)
            # 顯示主要 reason (1-2 個)
            reasons = p.get("reasons", [])
            if reasons:
                short_reason = reasons[0][:60]
                lines.append(f"     ✓ {short_reason}")
        return "\n".join(lines)
    except Exception as e:
        traceback.print_exc()
        return f"🇹🇼 <b>台股推薦</b>: (抓取失敗 {type(e).__name__})"


# ---------------------------------------------------------------------------
# Section 6: 今日盤前重點 (今天是否有財報日 / 重大事件)
# ---------------------------------------------------------------------------
def _section_today_focus() -> str:
    # Bug fix: dt.date.today() 是伺服器 UTC 日期。此推播固定在 UTC 23:32 (=TPE 07:32
    # 次日) 跑, 這剛好落在 TPE 00:00-08:00 這段「UTC 日期還沒跨過去」的窗口 — 週日
    # UTC 23:32 (代表週一 TPE 07:32, 正常開盤日) 時 dt.date.today() 會拿到「週日」,
    # weekday()>=5 直接誤判成週末, 導致每週一的早安推播都誤報「今日休市」。
    # 改用 TPE 校正後的日期, 跟 holiday_check._today_tpe() 同一套邏輯。
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    weekday = today.weekday()
    lines = [f"📅 <b>今日 ({today.strftime('%Y-%m-%d %a')})</b>"]
    # 台股是否開盤
    try:
        import holiday_check
        tw_closed = holiday_check.is_market_closed_today("TW")
        us_closed = holiday_check.is_market_closed_today("US")
        if tw_closed:
            lines.append("  ⚠ 台股今日休市")
        else:
            lines.append("  ✓ 台股正常開盤 (9:00 開盤 / 13:30 收盤)")
        if us_closed:
            lines.append("  ⚠ 美股今日休市")
    except Exception:
        if weekday >= 5:
            lines.append("  ⚠ 週末, 台美股皆休市")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section 7: 昨日盤後籌碼-價量交叉分析 (12 模式, 只顯示警示 + 看好)
# ---------------------------------------------------------------------------
def _section_chip_price_pattern() -> str:
    """分析 upside_screener top picks 的盤後籌碼 + 價量模式."""
    try:
        import upside_screener
        import chip_price_divergence as cpd
        result = upside_screener.run_upside_screen(market="all", max_stocks=80, use_cache=True)
        all_picks = (result.get("all") or [])[:15]  # 取 top 15 分析
        if not all_picks:
            return ""
        sids = [p.get("stock_id") for p in all_picks if p.get("stock_id")]
        if not sids:
            return ""
        patterns = cpd.analyze_batch(sids)
        # 只顯示「值得注意」的 (極強看好 + 警示 + 看壞)
        notable = cpd.filter_by_strength(
            patterns, ["strong_bullish", "bullish", "warning", "bearish"]
        )
        if not notable:
            return ""
        # 補上股名
        name_map = {p.get("stock_id"): p.get("name", "") for p in all_picks}
        lines = ["📊 <b>盤後籌碼-價量 12 模式</b>"]
        # 依嚴重度排序: warning 與 bearish 先, 再 strong_bullish, bullish
        order = {"warning": 0, "bearish": 1, "strong_bullish": 2, "bullish": 3}
        sorted_items = sorted(notable.items(),
                                key=lambda kv: order.get(kv[1].get("strength"), 9))
        for sid, r in sorted_items[:6]:  # 最多 6 檔避免訊息過長
            name = name_map.get(sid, "")
            emoji = r.get("emoji", "")
            pat = r.get("pattern", "")
            tp = r.get("raw", {}).get("today_pct", 0) or 0  # Bug fix: today_pct=None 時 :+.2f 會 TypeError → 整段盤後籌碼被吞掉
            lines.append(f"  {emoji} <b>{sid}</b> {name} — {pat} ({tp:+.2f}%)")
            rec = r.get("recommendation", "")
            if rec:
                lines.append(f"     💡 {rec[:80]}")
        return "\n".join(lines)
    except Exception as e:
        print(f"[morning_brief] chip_price_pattern failed: {e}", flush=True)
        return ""


# ---------------------------------------------------------------------------
# 重大事件預告 (FOMC / 非農 / CPI + 重要財報) — 週一推整週, 其他日推今明
# ---------------------------------------------------------------------------
def _section_events() -> str:
    try:
        import os
        import econ_calendar
        # 用戶自訂美股池 (US_WATCHLIST) 也納入財報涵蓋範圍
        us_wl = [s.strip().upper() for s in
                 os.environ.get("US_WATCHLIST", "").replace(",", " ").split() if s.strip()]
        mode = "week" if dt.datetime.utcnow().weekday() == 0 else "today"
        return econ_calendar.build_events_digest(mode=mode, watchlist=us_wl)
    except Exception as e:
        print(f"[morning_brief] events section failed: {e}", flush=True)
        return ""


# ---------------------------------------------------------------------------
# 組裝 + 推播
# ---------------------------------------------------------------------------
def compose_brief() -> str:
    """組整封晨報."""
    now = dt.datetime.now()
    header = f"☀️ <b>晨報</b> · {now.strftime('%m/%d %H:%M')}"
    sections = []
    sections.append(_safe("recap_tldr", _section_recap_and_tldr))  # 併入原 07:32 morning_recap 內容, 放最前
    sections.append(_safe("events", _section_events))  # 重大事件預告放最前面 (高優先)
    sections.append(_safe("us_overnight", _section_us_overnight))
    sections.append(_safe("sector_rotation", _section_sector_rotation))
    sections.append(_safe("ai_news", _section_ai_news))
    sections.append(_safe("macro_news", _section_macro_news))
    sections.append(_safe("tw_picks", _section_tw_picks))
    sections.append(_safe("chip_price_pattern", _section_chip_price_pattern))
    sections.append(_safe("today_focus", _section_today_focus))
    sections = [s for s in sections if s]
    body = "\n\n".join(sections)
    footer = "\n\n<i>※ 演算法產出, 不構成投資建議</i>"
    full = header + "\n\n" + body + footer
    if len(full) > MAX_TG_CHARS:
        body_max = MAX_TG_CHARS - len(header) - len(footer) - 50
        full = header + "\n\n" + body[:body_max] + "\n\n…(訊息過長, 已截斷)" + footer
    return full


def main():
    if not notifier.is_configured():
        print("[morning_brief] TG 未設定, 略過", flush=True)
        return 1
    print("[morning_brief] 組裝晨報…", flush=True)
    t0 = time.time()
    msg = compose_brief()
    elapsed = time.time() - t0
    print(f"[morning_brief] 組裝完成 (用時 {elapsed:.1f} 秒, {len(msg)} chars)", flush=True)
    ok, info = notifier.send_message(msg, disable_preview=True)
    if ok:
        print(f"[morning_brief] ✓ 推播成功", flush=True)
        return 0
    else:
        print(f"[morning_brief] ✗ 推播失敗: {info}", flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
