"""
scripts/market_open_alert.py
Standalone script — 由 GitHub Actions cron 在台股 / 美股開盤後 30 分鐘呼叫。

用法:
    python scripts/market_open_alert.py tw   # 台股
    python scripts/market_open_alert.py us   # 美股
"""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path

# 抑制 streamlit cache 在非 runtime 環境下的警告
logging.getLogger("streamlit").setLevel(logging.ERROR)
logging.getLogger("streamlit.runtime").setLevel(logging.ERROR)
logging.getLogger("streamlit.runtime.caching").setLevel(logging.ERROR)

# 加入上層目錄到 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# 1) 在 import 任何模組前，先確認 streamlit 可用 (script 環境也會裝)
# ---------------------------------------------------------------------------
try:
    import streamlit as st
    # streamlit 在非 streamlit runtime 下，cache_data 仍會 work，只是不會持久 cache
    # secrets 在非 runtime 下會 throw，所以 _secret 會 fall back 到 os.environ
except ImportError:
    # 萬一沒裝，給個 stub 避免 crash
    st_stub = types.ModuleType("streamlit")
    def _passthrough_decorator(*a, **kw):
        if len(a) == 1 and callable(a[0]):
            return a[0]
        def deco(f):
            return f
        return deco
    st_stub.cache_data = _passthrough_decorator       # type: ignore
    st_stub.cache_resource = _passthrough_decorator   # type: ignore
    st_stub.secrets = {}                              # type: ignore
    st_stub.warning = lambda *a, **k: None            # type: ignore
    st_stub.info = lambda *a, **k: None               # type: ignore
    st_stub.error = lambda *a, **k: None              # type: ignore
    sys.modules["streamlit"] = st_stub


# ===== 自我診斷：依序 import 所有需要的模組 =====
print("=== Module Imports ===")
_required_modules = [
    "data_sources", "tw_screener", "sector_pulse", "us_screener",
    "notifier", "ai_analyzer", "market_predictor",
    "stock_catalyst", "news_sources", "earnings_calendar",
    "market_open_picks",
]
_missing = []
for _mod in _required_modules:
    try:
        __import__(_mod)
        print(f"  [OK]   {_mod}")
    except Exception as _e:
        print(f"  [FAIL] {_mod}: {_e}")
        _missing.append(_mod)
if _missing:
    print(f"❌ 失敗的 modules: {_missing}")
    print("→ 請確認以下檔案都在 GitHub repo 根目錄:")
    for _m in _missing:
        print(f"   - {_m}.py")
    sys.exit(3)
print("==================\n")

import ai_analyzer
import market_open_picks
import notifier


def _summarize_tw_for_ai(data: dict) -> str:
    if data.get("error"):
        return ""
    lines = []
    # 美股隔夜行情 (重要 macro context)
    us_ov = data.get("us_overnight") or {}
    if us_ov:
        us_lines = []
        for sym in ["SPY", "QQQ", "DIA"]:
            if sym in us_ov and us_ov[sym].get("pct") is not None:
                us_lines.append(f"{sym} {us_ov[sym]['pct']:+.2f}%")
        if us_lines:
            lines.append("美股隔夜: " + " / ".join(us_lines))
        sec_df = us_ov.get("sectors")
        if sec_df is not None and not sec_df.empty:
            top_sec = ", ".join(f"{r['symbol']} ({r['1d_%']:+.2f}%)" for _, r in sec_df.head(3).iterrows())
            lines.append(f"美股強勢板塊: {top_sec}")
        fg_d = us_ov.get("fg") or {}
        if fg_d.get("score") is not None:
            lines.append(f"CNN F&G: {fg_d['score']:.0f} ({fg_d.get('rating')})")
        lines.append("")

    # 日韓 leading indicator (比台股早 1 小時開盤)
    asia = data.get("asia") or {}
    asia_snapshot = asia.get("snapshot") or []
    asia_events = asia.get("events") or []
    if asia_snapshot or asia_events:
        lines.append("日韓盤中行情 (比台股早 1 小時開盤，可作 leading indicator):")
        for s in asia_snapshot:
            name = s.get("market", "")
            dp = s.get("daily_pct", 0)
            five = s.get("5d_pct", 0)
            lines.append(f"  {name}: 今日 {dp:+.2f}% / 5d {five:+.2f}%")
        if asia_events:
            lines.append("  異常事件:")
            for ev in asia_events[:5]:
                name = ev.get("market", "")
                event = ev.get("event", "")
                msg = ev.get("msg", "")
                lines.append(f"    - {name} [{event}] {msg}")
        lines.append("")

    themes_df = data.get("themes")
    if themes_df is not None and not themes_df.empty:
        lines.append("熱門題材排行:")
        for _, r in themes_df.iterrows():
            lines.append(f"  {r.get('題材')}: 平均 {r.get('平均%')}% 上漲 {int(r.get('上漲家數',0))}/{int(r.get('樣本數',0))}")
    for p in data.get("picks", []):
        theme = p.get("theme")
        stocks = p.get("stocks")
        if stocks is None or stocks.empty:
            continue
        names = ", ".join(f"{r.get('stock_id')} {r.get('stock_name','')} ({r.get('今日%')}%)"
                            for _, r in stocks.iterrows())
        lines.append(f"  [{theme}] 候選: {names}")
    return "\n".join(lines)


def _summarize_us_for_ai(data: dict) -> str:
    if data.get("error"):
        return ""
    lines = []
    sectors = data.get("sectors")
    if sectors is not None and not sectors.empty:
        lines.append("板塊輪動 (1d):")
        for _, r in sectors.iterrows():
            lines.append(f"  {r.get('symbol')} {r.get('sector','')}: {r.get('1d_%'):.2f}%")
    for sp in data.get("sector_picks", []):
        sec = sp.get("sector")
        stocks = sp.get("stocks")
        if stocks is None or stocks.empty:
            continue
        names = ", ".join(f"{r.get('symbol')} ({r.get('今日%')}%)" for _, r in stocks.iterrows())
        lines.append(f"  [{sec}] 候選: {names}")
    growth = data.get("growth")
    if growth is not None and not growth.empty:
        names = ", ".join(f"{r.get('symbol')}" for _, r in growth.head(5).iterrows())
        lines.append(f"  成長動能 IPO 池: {names}")
    return "\n".join(lines)


def main() -> int:
    market = (sys.argv[1] if len(sys.argv) > 1 else "tw_open").lower()
    # 舊參數相容
    if market == "tw":
        market = "tw_open"
    if market == "us":
        market = "us_open"

    # === 假日檢查 ===
    try:
        import holiday_check

        if market == "holiday_news":
            # 反向邏輯: TW 開盤日才跳過 (其他正常推播會處理), 休市日才跑
            if not holiday_check.is_market_closed_today("TW"):
                print(f"=== Holiday Check ===")
                print(f"今日 TW 開盤交易日，holiday_news 跳過此次推播。")
                print(f"市場狀態: {holiday_check.market_status_summary()}")
                return 0
            print(f"=== Holiday Check ===")
            print(f"今日 TW 休市，執行假日重大消息推播")
            print(f"市場狀態: {holiday_check.market_status_summary()}")
            print(f"=====================\n")
        else:
            market_for_holiday = "TW" if market.startswith("tw") else "US"
            if holiday_check.is_market_closed_today(market_for_holiday):
                print(f"=== Holiday Check ===")
                print(f"今日 {market_for_holiday} 休市，跳過此次推播。")
                print(f"市場狀態: {holiday_check.market_status_summary()}")
                return 0
            print(f"=== Holiday Check ===")
            print(f"{market_for_holiday} 開盤中，繼續執行")
            print(f"=====================\n")
    except Exception as e:
        print(f"⚠️ Holiday check failed: {e} - 忽略假日檢查繼續執行")

    # 診斷 log
    print(f"=== Secrets Check ===")
    finmind_ok = bool(os.environ.get('FINMIND_TOKEN'))
    tg_bot_ok = bool(os.environ.get('TELEGRAM_BOT_TOKEN'))
    tg_chat_ok = bool(os.environ.get('TELEGRAM_CHAT_ID'))
    gemini_ok = bool(os.environ.get('GEMINI_API_KEY'))
    print(f"FINMIND_TOKEN:      {'✓ set' if finmind_ok else '✗ MISSING'}")
    print(f"TELEGRAM_BOT_TOKEN: {'✓ set' if tg_bot_ok else '✗ MISSING'}")
    print(f"TELEGRAM_CHAT_ID:   {'✓ set' if tg_chat_ok else '✗ MISSING'}")
    print(f"GEMINI_API_KEY:     {'✓ set' if gemini_ok else '✗ not set (optional)'}")
    if not (finmind_ok and tg_bot_ok and tg_chat_ok):
        print("❌ 必要 secrets 缺失，無法繼續。請到 GitHub → Settings → Secrets → Actions 補上。")
        return 4
    print(f"")

    # 實測 Gemini API key 有效性
    print(f"=== Gemini API Test ===")
    if gemini_ok:
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            test_model = genai.GenerativeModel("gemini-2.5-flash")
            test_resp = test_model.generate_content(
                "Reply with exactly the word: OK",
                generation_config={"max_output_tokens": 10, "temperature": 0},
            )
            test_text = (test_resp.text or "").strip()
            if test_text:
                print(f"✓ Gemini test call OK: {test_text[:50]}")
            else:
                print(f"✗ Gemini returned empty (可能 safety filter)")
        except Exception as e:
            print(f"✗ Gemini test failed: {type(e).__name__}: {e}")
            print(f"  → 可能是 key 無效、quota 用光、或網路問題")
    else:
        print("⊘ skipped (no GEMINI_API_KEY)")
    print(f"ai_analyzer.gemini_available(): {ai_analyzer.gemini_available()}")
    print(f"=======================\n")

    if market in ("tw_open", "tw_mid"):
        label = "開盤後 30 分鐘 (09:30)" if market == "tw_open" else "中盤更新 (11:00)"
        print(f"Running TW {label}...")
        data = market_open_picks.get_tw_open_picks()
        if data.get("error"):
            print(f"data error: {data['error']}")
        else:
            print(f"Got {len(data.get('picks', []))} themes with picks")
            print(f"Prediction: {data.get('prediction', {}).get('pattern', 'N/A')}")
        ai_text = ""
        if ai_analyzer.gemini_available():
            print("Calling Gemini...")
            ok, ai_text = ai_analyzer.analyze_open_picks("TW", _summarize_tw_for_ai(data))
            if not ok:
                print(f"AI failed: {ai_text}")
                ai_text = ""
            else:
                print(f"Gemini returned {len(ai_text)} chars")
        else:
            print("Gemini not available - skipping AI section")
        msg = notifier.fmt_tw_open_picks(data, ai_text=ai_text)
        # 中盤版本標題改一下
        if market == "tw_mid":
            msg = msg.replace("台股開盤後 30 分鐘 · 資金流向", "台股中盤更新 11:00 · 資金流向")
    elif market == "tw_close":
        print("Running TW market close analysis (15:00)...")
        data = market_open_picks.get_tw_close_analysis()
        if data.get("ai_text"):
            print(f"Gemini reasoning: {len(data['ai_text'])} chars")
        msg = notifier.fmt_tw_close_analysis(data)
    elif market in ("us_open", "us_mid"):
        label = "開盤後 30 分鐘 (10:00 EDT)" if market == "us_open" else "開盤後 2 小時 (11:30 EDT)"
        print(f"Running US {label}...")
        data = market_open_picks.get_us_open_picks()
        if data.get("error"):
            print(f"data error: {data['error']}")
        else:
            print(f"Got {len(data.get('sector_picks', []))} sectors with picks")
            print(f"Prediction: {data.get('prediction', {}).get('pattern', 'N/A')}")
        ai_text = ""
        if ai_analyzer.gemini_available():
            print("Calling Gemini...")
            ok, ai_text = ai_analyzer.analyze_open_picks("US", _summarize_us_for_ai(data))
            if not ok:
                print(f"AI failed: {ai_text}")
                ai_text = ""
            else:
                print(f"Gemini returned {len(ai_text)} chars")
        else:
            print("Gemini not available - skipping AI section")
        msg = notifier.fmt_us_open_picks(data, ai_text=ai_text)
        if market == "us_mid":
            msg = msg.replace("美股開盤後 30 分鐘 · 資金流向",
                               "美股開盤後 2 小時 · 中盤更新 · 資金流向")
    elif market == "us_close":
        print("Running US market close analysis (+2h, 18:00 EDT)...")
        data = market_open_picks.get_us_close_analysis()
        if data.get("ai_text"):
            print(f"Gemini reasoning: {len(data['ai_text'])} chars")
        msg = notifier.fmt_us_close_analysis(data)
    elif market == "holiday_news":
        print("Running TW holiday news summary (22:30 台北)...")
        data = market_open_picks.get_holiday_news_summary()
        if data.get("ai_text"):
            print(f"Gemini reasoning: {len(data['ai_text'])} chars")
        print(f"Got {len(data.get('news', []))} news items")
        msg = notifier.fmt_holiday_news(data)
    else:
        print(f"Unknown market: {market}", file=sys.stderr)
        return 1

    ok, info = notifier.send_message(msg)
    if ok:
        print("Telegram sent OK")
        return 0
    print(f"Telegram failed: {info}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
