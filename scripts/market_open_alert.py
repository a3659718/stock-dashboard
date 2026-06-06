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

        if market == "monitor":
            # monitor 不檢查假日 (24x7 都跑, 自然會無新警報)
            print(f"=== Holiday Check ===")
            print(f"monitor mode: 24x7 執行，不檢查假日")
            print(f"=====================\n")
        elif market in ("heartbeat",):
            # heartbeat 每天跑, 不檢查假日
            print(f"=== Holiday Check ===")
            print(f"heartbeat mode: 每日執行, 不檢查假日")
            print(f"=====================\n")
        elif market == "weekend_recap":
            # 只在 Sat / Sun fire
            import datetime as _dt
            if _dt.date.today().weekday() < 5:
                print(f"=== Weekend Recap Check ===")
                print(f"今日 weekday ({_dt.date.today().strftime('%A')}), weekend_recap mode 跳過此次推播.")
                return 0
            print(f"=== Weekend Recap Check ===")
            print(f"今日 weekend ({_dt.date.today().strftime('%A')}), 執行週末摘要推播")
            print(f"=====================\n")
        elif market == "holiday_news":
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
            # 帶 safety_settings 避免 finish_reason=2 (SAFETY) 假警報
            try:
                safety = ai_analyzer.get_safety_settings()
            except Exception:
                safety = None
            test_resp = test_model.generate_content(
                "What is 2 plus 3? Answer with just the number.",
                generation_config={"max_output_tokens": 20, "temperature": 0},
                safety_settings=safety,
            )
            # 不直接用 .text (會 raise), 改檢查 candidates
            cands = getattr(test_resp, "candidates", []) or []
            if cands:
                cand = cands[0]
                fr = getattr(cand, "finish_reason", None)
                # finish_reason: 1=STOP, 2=SAFETY, 3=MAX_TOKENS, 4=RECITATION
                if fr in (1, "STOP", None):
                    try:
                        text = test_resp.text.strip()
                        print(f"✓ Gemini test call OK: {text[:50]}")
                    except Exception:
                        print(f"✓ Gemini API 回應正常 (finish={fr}, 但 text 取不到, 可忽略)")
                elif fr == 2 or fr == "SAFETY":
                    print(f"⚠️ Gemini 測試被 safety filter 擋 (finish=2), API key 仍有效, 推播功能不受影響")
                else:
                    print(f"⚠️ Gemini finish_reason={fr}, 可能 quota 或其他, 但 key 有效")
            else:
                print(f"⚠️ Gemini 無 candidate 回應 — key 可能有問題")
        except Exception as e:
            print(f"✗ Gemini test failed: {type(e).__name__}: {e}")
            print(f"  → 可能是 key 無效、quota 用光、或網路問題")
    else:
        print("⊘ skipped (no GEMINI_API_KEY)")
    print(f"ai_analyzer.gemini_available(): {ai_analyzer.gemini_available()}")
    print(f"=======================\n")

    # 實測 FinMind Token 有效性 (避免後續 deep error)
    print(f"=== FinMind API Test ===")
    try:
        import requests as _req
        token = os.environ.get("FINMIND_TOKEN", "").strip()
        # 檢查 token 是否有 leading/trailing whitespace
        raw_token = os.environ.get("FINMIND_TOKEN", "")
        if raw_token != token:
            print(f"⚠️  Token 有前後空白 (raw len={len(raw_token)}, stripped len={len(token)}) — 已自動去除")
        if not token:
            print(f"✗ FINMIND_TOKEN 是空字串")
        else:
            r = _req.get(
                "https://api.finmindtrade.com/api/v4/data",
                params={"dataset": "TaiwanStockInfo", "token": token},
                timeout=10,
            )
            if r.status_code == 200:
                print(f"✓ FinMind token OK")
            elif r.status_code == 400 and "illegal" in r.text.lower():
                print(f"✗ FinMind token 被拒 (illegal): {r.text[:120]}")
                print(f"  → 請到 https://finmindtrade.com/ 重新生成, 並更新 GitHub secret")
            else:
                print(f"⚠️ FinMind HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"✗ FinMind test failed: {type(e).__name__}: {e}")
    print(f"========================\n")


    # === 🇺🇸 川普政策推播 (monitor 跑時, 60min cooldown) ===
    if market == "monitor":
        try:
            import trump_policy_alert as _tp
            tp_alerts = _tp.check_trump_policy_news() or []
            if tp_alerts:
                print(f"[trump_policy] triggered {len(tp_alerts)}", flush=True)
                gem = _tp.analyze_with_gemini(tp_alerts)
                tp_msg = notifier.fmt_trump_policy_alerts(tp_alerts, gem)
                if tp_msg:
                    ok_tp, info_tp = notifier.send_message(
                        tp_msg, disable_preview=False, disable_notification=False
                    )
                    if ok_tp:
                        _tp.mark_alerts_sent(tp_alerts)
                    print(f"[trump_policy] sent ok={ok_tp}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[trump_policy] failed: {_e}", flush=True)
            traceback.print_exc()

    # === 🌏 日韓 leading alert (monitor 跑時, 台股盤中) ===
    if market == "monitor":
        try:
            import asia_leading_alert as _al
            al_alerts = _al.check_asia_leading() or []
            if al_alerts:
                print(f"[asia_leading] triggered {len(al_alerts)}", flush=True)
                al_msg = notifier.fmt_asia_leading_alerts(al_alerts)
                if al_msg:
                    ok_al, info_al = notifier.send_message(
                        al_msg, disable_preview=True, disable_notification=False
                    )
                    if ok_al:
                        _al.mark_alerts_sent(al_alerts)
                    print(f"[asia_leading] sent ok={ok_al}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[asia_leading] failed: {_e}", flush=True)
            traceback.print_exc()

    # === 🏛 美股機構/內部人動向 (us_open 跑一次/天) ===
    if market == "us_open":
        try:
            import analyst_insider_alert as _ai
            ai_alerts = _ai.check_analyst_insider() or []
            if ai_alerts:
                print(f"[analyst_insider] triggered {len(ai_alerts)}", flush=True)
                gem = _ai.analyze_with_gemini(ai_alerts)
                ai_msg = notifier.fmt_analyst_insider_alerts(ai_alerts, gem)
                if ai_msg:
                    ok_a, info_a = notifier.send_message(
                        ai_msg, disable_preview=True, disable_notification=False
                    )
                    if ok_a:
                        _ai.mark_alerts_sent(ai_alerts)
                    print(f"[analyst_insider] sent ok={ok_a}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[analyst_insider] failed: {_e}", flush=True)
            traceback.print_exc()


    # === 💰 利率週期建議 (週一 us_open 推一次/週) ===
    if market == "us_open":
        try:
            import datetime as _dt2
            if _dt2.date.today().weekday() == 0:  # Monday only
                import rate_cycle_advisor as _rc
                cyc = _rc.detect_cycle()
                if cyc.get("cycle") != "unknown":
                    advice = _rc.get_sector_advice(cyc["cycle"])
                    # 加 Gemini 解讀看好族群
                    try:
                        gem = _rc.analyze_outperform_with_gemini(cyc["cycle"], advice)
                        if gem: advice["gemini_analysis"] = gem
                    except Exception:
                        pass
                    rc_msg = notifier.fmt_rate_cycle_advice(cyc, advice)
                    if rc_msg:
                        ok_rc, info_rc = notifier.send_message(
                            rc_msg, disable_preview=True, disable_notification=False
                        )
                        print(f"[rate_cycle] sent ok={ok_rc}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[rate_cycle] failed: {_e}", flush=True)
            traceback.print_exc()


    # === 🎲 投機股精選 (週三 us_open 推 1 次/週, 避免 cluttering) ===
    if market == "us_open":
        try:
            import datetime as _dt3
            if _dt3.date.today().weekday() == 2:  # Wednesday only
                import speculation_screener as _ss
                spec_picks = _ss.compute_speculation_picks(top_n=10, min_score=60) or []
                if spec_picks:
                    spec_msg = notifier.fmt_speculation_picks(spec_picks)
                    if spec_msg:
                        ok_sp, info_sp = notifier.send_message(
                            spec_msg, disable_preview=True, disable_notification=False
                        )
                        print(f"[speculation] sent ok={ok_sp}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[speculation] failed: {_e}", flush=True)
            traceback.print_exc()

    # === 🌅 morning_action 早盤情境推播 (tw_open / us_open 都跑) ===
    if market in ("tw_open", "us_open"):
        try:
            import morning_action_alert as _ma
            mk = "TW" if market == "tw_open" else "US"
            ma_msg = _ma.build_morning_action_msg(mk)
            if ma_msg:
                ok_ma, info_ma = notifier.send_message(
                    ma_msg, disable_preview=True, disable_notification=False
                )
                print(f"[morning_action] {mk} TG: ok={ok_ma}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[morning_action] failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

    # === 🚀 美股盤整突破 (US session 內每次 monitor 都掃) ===
    if market in ("us_open", "us_mid", "monitor"):
        try:
            import breakout_consolidation_alert as _bc
            bc_alerts = _bc.check_breakout_consolidation(top_n=5) or []
            if bc_alerts:
                print(f"[breakout] triggered {len(bc_alerts)}: "
                      + ", ".join(a.get("symbol", "") for a in bc_alerts),
                      flush=True)
                bc_msg = notifier.fmt_breakout_consolidation_alerts(bc_alerts)
                if bc_msg:
                    ok_bc, info_bc = notifier.send_message(
                        bc_msg, disable_preview=True, disable_notification=False
                    )
                    if ok_bc:
                        _bc.mark_alerts_sent(bc_alerts)
        except Exception as _e:
            import traceback
            print(f"[breakout] failed (non-fatal): {_e}", flush=True)



    # === 📊 台股盤後總結 (TPE 16:00) ===
    if market == "tw_post_market":
        try:
            import tw_post_market_summary as _pms
            pms_msg = _pms.build_post_market_msg()
            if pms_msg:
                ok_pms, info_pms = notifier.send_message(
                    pms_msg, disable_preview=True, disable_notification=False
                )
                print(f"[tw_post_market] sent ok={ok_pms}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[tw_post_market] failed: {_e}", flush=True)
            traceback.print_exc()
        print("=== Post-market done ===")
        return 0

    # === 🌅 pre_market_morning 推播 (TPE 08:15 / 08:30) ===
    if market in ("pre_market_815", "pre_market_830"):
        try:
            import pre_market_alert as _pm
            slot = "08:15" if market == "pre_market_815" else "08:30"
            pm_msg = _pm.build_pre_market_msg(slot)
            if pm_msg:
                ok_pm, info_pm = notifier.send_message(
                    pm_msg, disable_preview=True, disable_notification=False
                )
                print(f"[pre_market] {slot} sent ok={ok_pm}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[pre_market] failed: {_e}", flush=True)
            traceback.print_exc()
        print("=== Pre-market done ===")
        return 0

    if market in ("tw_open", "tw_mid"):
        label = "開盤後 30 分鐘 (09:30)" if market == "tw_open" else "中盤更新 (11:00)"
        print(f"Running TW {label}...")

        # ===== 主動式 ETF 持股變動 (只在 tw_open 跑, 09:30 時 MoneyDJ 應已更新昨日資料) =====
        if market == "tw_open":
            try:
                import active_etf_monitor
                etf_changes = active_etf_monitor.check_all_active_etfs()
                if etf_changes:
                    for diff in etf_changes:
                        etf_code = diff.get("etf_code", "?")
                        is_baseline = diff.get("is_baseline", False)
                        msg_etf = notifier.fmt_active_etf_change(diff)
                        if not msg_etf:
                            continue
                        if is_baseline:
                            print(f"[active_etf] sending baseline-init notification for {etf_code}", flush=True)
                        else:
                            print(
                                f"[active_etf] sending change alert for {etf_code}: "
                                f"+{len(diff.get('added', []))} -{len(diff.get('removed', []))} "
                                f"~{len(diff.get('changed', []))}",
                                flush=True,
                            )
                        # Hidden Bug #6 fix: 接 send_message 的 return tuple 並 log
                        ok_etf, info_etf = notifier.send_message(msg_etf)
                        print(f"[active_etf] TG result for {etf_code}: ok={ok_etf} info={info_etf}", flush=True)
                else:
                    print("[active_etf] no changes detected (data_date unchanged)", flush=True)
            except Exception as _e:
                import traceback
                print(f"[active_etf] check failed (non-fatal): {_e}", flush=True)
                traceback.print_exc()

        # 持倉停損預警 — 在主分析前先檢查, 跌破立刻另外推一封
        try:
            import holdings_tracker
            sl_breaches = holdings_tracker.check_stop_loss_breaches()
            if sl_breaches:
                sl_msg = notifier.fmt_stop_loss_alerts(sl_breaches)
                if sl_msg:
                    print(f"持倉停損警報: {len(sl_breaches)} 檔跌破", flush=True)
                    notifier.send_message(sl_msg)
        except Exception as e:
            print(f"停損檢查失敗 (non-fatal): {e}", flush=True)

        # 3 條件性: TW mid (11:00) 只在「相對昨收有明顯變化」才推, 避免每天固定發 1 封
        # MED-B2 fix: 改用「今日 vs 昨收 |pct| ≥ 0.5%」(門檻較寬), 比較直觀
        # HIGH-B1 fix: 任何錯誤 → silent skip 不推, 避免推「沒變化通知」誤導
        if market == "tw_mid":
            should_skip_tw_mid = False
            try:
                import data_sources as _ds
                # 抓今日 + 昨日 5m, 用收盤算 vs 昨收
                twii = _ds.fetch_yf_history("^TWII", period="2d", interval="5m")
                if twii is None or twii.empty:
                    print("[tw_mid] 無 TWII 數據, skip 不推", flush=True)
                    return 0
                import pandas as _pd
                date_col = "Datetime" if "Datetime" in twii.columns else twii.columns[0]
                twii = twii.copy()
                twii["_dt"] = _pd.to_datetime(twii[date_col], utc=True)
                twii["_d"] = twii["_dt"].dt.date
                today = twii["_d"].max()
                today_bars = twii[twii["_d"] == today].sort_values("_dt")
                prev_bars = twii[twii["_d"] < today].sort_values("_dt")
                if len(today_bars) < 3 or prev_bars.empty:
                    print("[tw_mid] today bars < 3 或無昨日 — skip 不推", flush=True)
                    return 0
                cur_p = float(today_bars["Close"].iloc[-1])
                prev_close = float(prev_bars["Close"].iloc[-1])
                if prev_close <= 0:
                    print("[tw_mid] prev_close 無效, skip", flush=True)
                    return 0
                pct_vs_prev = (cur_p / prev_close - 1) * 100
                if abs(pct_vs_prev) < 0.5:
                    print(
                        f"[tw_mid] TWII vs 昨收 {pct_vs_prev:+.2f}% (< ±0.5%), "
                        f"無明顯變化, 跳過推播",
                        flush=True,
                    )
                    return 0
                print(f"[tw_mid] TWII vs 昨收 {pct_vs_prev:+.2f}% (≥ ±0.5%), 推播", flush=True)
            except Exception as _e:
                # HIGH-B1 fix: 失敗時保守 skip, 不再 fallback 推
                print(f"[tw_mid] 變化檢測 raise {_e} — 保守 skip 不推", flush=True)
                return 0

        try:
            data = market_open_picks.get_tw_open_picks()
        except Exception as e:
            print(f"get_tw_open_picks fatal failure: {e}", flush=True)
            err_msg = (
                f"<b>台股 {label} 推播失敗</b>\n\n"
                f"原因: <code>{type(e).__name__}: {str(e)[:200]}</code>\n\n"
                f"建議檢查:\n"
                f"  • FinMind Token 是否有效 (常見: 過期/重新生成)\n"
                f"  • GitHub Actions log 看詳細 stack trace"
            )
            try:
                notifier.send_message(err_msg)
            except Exception:
                pass
            return 1
        if data.get("error"):
            print(f"data error: {data['error']}")
        else:
            print(f"Got {len(data.get('picks', []))} themes with picks")
            print(f"Prediction: {data.get('prediction', {}).get('pattern', 'N/A')}")
        ai_text = ""
        if ai_analyzer.gemini_available():
            print("Calling Gemini...")
            try:
                ok, ai_text = ai_analyzer.analyze_open_picks("TW", _summarize_tw_for_ai(data))
                if not ok:
                    print(f"AI failed: {ai_text}")
                    ai_text = ""
                else:
                    print(f"Gemini returned {len(ai_text)} chars")
            except Exception as e:
                print(f"Gemini exception: {e}")
                ai_text = ""
        else:
            print("Gemini not available - skipping AI section")
        msg = notifier.fmt_tw_open_picks(data, ai_text=ai_text)
        # 中盤版本標題改一下
        if market == "tw_mid":
            msg = msg.replace("台股開盤後 30 分鐘 · 資金流向", "台股中盤更新 11:00 · 資金流向")
    elif market == "tw_close":
        print("Running TW market close analysis (15:00)...")
        try:
            data = market_open_picks.get_tw_close_analysis()
        except Exception as e:
            print(f"get_tw_close_analysis fatal failure: {e}", flush=True)
            err_msg = (
                f"<b>台股盤後分析推播失敗</b>\n\n"
                f"原因: <code>{type(e).__name__}: {str(e)[:200]}</code>"
            )
            try:
                notifier.send_message(err_msg)
            except Exception:
                pass
            return 1
        if data.get("ai_text"):
            print(f"Gemini reasoning: {len(data['ai_text'])} chars")
        msg = notifier.fmt_tw_close_analysis(data)

        # 盤後籌碼-價量 12 模式分析 — 額外推一封 (極強看好/警示/看壞 標的)
        try:
            import chip_price_divergence as _cpd
            # 從 upside_screener top picks + 用戶 watchlist 一起分析
            cpd_stocks = []
            try:
                import upside_screener as _us
                up_result = _us.run_upside_screen(market="all", max_stocks=80, use_cache=True)
                cpd_stocks = [p.get("stock_id") for p in (up_result.get("all") or [])[:20]
                              if p.get("stock_id")]
            except Exception as _e:
                print(f"  cpd: upside_screener fetch failed: {_e}", flush=True)
            try:
                import watchlist_store as _ws
                wl = _ws.load_watchlist() or []
                cpd_stocks = list(dict.fromkeys(cpd_stocks + wl))
            except Exception:
                pass
            if cpd_stocks:
                print(f"  Running chip-price divergence on {len(cpd_stocks)} stocks", flush=True)
                cpd_results = _cpd.analyze_batch(cpd_stocks)
                cpd_msg = _cpd.fmt_summary_tg(cpd_results, only_strong=True)
                if cpd_msg and len(cpd_msg) > 80:  # 有實質內容才推
                    print(f"  Sending chip-price divergence ({len(cpd_msg)} chars)", flush=True)
                    notifier.send_message(cpd_msg)
        except Exception as _e:
            print(f"  chip_price_divergence section failed: {_e}", flush=True)

        # 持倉日報 — 額外推一封
        try:
            import holdings_analyzer
            import holdings_tracker

            # 先驗證舊預測 (隔日已收盤 → 對昨天的預測算對錯)
            try:
                n_validated = holdings_tracker.evaluate_pending_predictions()
                if n_validated:
                    print(f"驗證 {n_validated} 筆舊預測", flush=True)
            except Exception as _e:
                print(f"  evaluate_pending_predictions failed: {_e}", flush=True)

            # 跑今日分析
            holdings_results = holdings_analyzer.analyze_all_holdings()
            if holdings_results:
                # 紀錄預測 + 同步停損
                try:
                    holdings_tracker.save_predictions(holdings_results)
                    holdings_tracker.update_stop_loss_state(holdings_results)
                except Exception as _e:
                    print(f"  save_predictions/update_stop_loss failed: {_e}", flush=True)

                holdings_msg = notifier.fmt_holdings_daily(holdings_results)
                # 附準確率 (有歷史資料才顯示)
                try:
                    acc = holdings_tracker.accuracy_summary(lookback_days=30)
                    acc_msg = notifier.fmt_holdings_accuracy(acc)
                    if acc_msg:
                        holdings_msg = holdings_msg.rstrip() + "\n\n" + acc_msg
                except Exception as _e:
                    print(f"  accuracy_summary failed: {_e}", flush=True)

                if holdings_msg:
                    print(f"Sending holdings daily report: {len(holdings_results)} stocks")
                    notifier.send_message(holdings_msg)
                else:
                    print("Holdings analysis empty, skip daily report")
            else:
                print("No holdings configured, skip daily report")
        except Exception as e:
            print(f"Holdings daily report failed (non-fatal): {e}", flush=True)
    elif market in ("us_open", "us_mid"):
        label = "開盤後 30 分鐘 (10:00 EDT)" if market == "us_open" else "開盤後 2 小時 (11:30 EDT)"
        print(f"Running US {label}...")
        try:
            data = market_open_picks.get_us_open_picks()
        except Exception as e:
            print(f"get_us_open_picks fatal failure: {e}", flush=True)
            # 發一個簡訊告知失敗, 至少不要 silent
            err_msg = (
                f"<b>美股 {label} 推播失敗</b>\n\n"
                f"原因: <code>{type(e).__name__}: {str(e)[:200]}</code>\n\n"
                f"建議檢查:\n"
                f"  • FinMind Token 是否有效 (常見: 過期/重新生成)\n"
                f"  • Yahoo Finance 是否被 rate-limit\n"
                f"  • GitHub Actions log 看詳細 stack trace"
            )
            try:
                notifier.send_message(err_msg)
            except Exception:
                pass
            return 1
        if data.get("error"):
            print(f"data error: {data['error']}")
        else:
            print(f"Got {len(data.get('sector_picks', []))} sectors with picks")
            print(f"Prediction: {data.get('prediction', {}).get('pattern', 'N/A')}")
        ai_text = ""
        if ai_analyzer.gemini_available():
            print("Calling Gemini...")
            try:
                ok, ai_text = ai_analyzer.analyze_open_picks("US", _summarize_us_for_ai(data))
                if not ok:
                    print(f"AI failed: {ai_text}")
                    ai_text = ""
                else:
                    print(f"Gemini returned {len(ai_text)} chars")
            except Exception as e:
                print(f"Gemini exception: {e}")
                ai_text = ""
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
    elif market == "weekend_recap":
        # 週末重點摘要 (Sat/Sun 22:00 TPE) — 比 holiday_news 早 30 min
        # 觸發後設 state flag, holiday_news 30 min 後若 flag 是今天就 skip (dedup)
        print("Running weekend recap (Sat/Sun 22:00 TPE)...")
        import datetime as _dt
        today_str = _dt.date.today().strftime("%Y-%m-%d")
        try:
            data = market_open_picks.get_weekend_recap_summary()
            if data.get("next_week_outlook"):
                print(f"Gemini next_week_outlook: {len(data['next_week_outlook'])} chars")
            print(f"Got {len(data.get('news', []))} news items, "
                  f"{len(data.get('week_perf', {}))} indices, "
                  f"{len(data.get('etf_snapshot', []))} ETFs")
            msg = notifier.fmt_weekend_recap(data)
            # 設 dedup flag — 讓 30 min 後的 holiday_news cron 知道週末摘要已推, 不要重複
            try:
                import watchlist_store
                state = watchlist_store.load_monitor_state()
                state["weekend_recap_last_fired"] = today_str
                watchlist_store.save_monitor_state(state)
                print(f"[weekend_recap] dedup flag set: weekend_recap_last_fired={today_str}")
            except Exception as _e:
                print(f"[weekend_recap] flag set failed (non-fatal): {_e}")
        except Exception as e:
            import traceback
            print(f"weekend_recap fatal: {e}", flush=True)
            traceback.print_exc()
            return 1

    elif market == "holiday_news":
        print("Running TW holiday news summary (22:30 台北)...")
        # I2-like dedup: 若今天 weekend_recap 已推, 跳過 holiday_news 避免雙推
        try:
            import watchlist_store
            import datetime as _dt
            today_str = _dt.date.today().strftime("%Y-%m-%d")
            state = watchlist_store.load_monitor_state()
            last_recap = state.get("weekend_recap_last_fired")
            if last_recap == today_str:
                print(
                    f"[holiday_news] weekend_recap already fired today ({last_recap}), "
                    f"skip holiday_news 避免雙推"
                )
                return 0
        except Exception as _e:
            print(f"[holiday_news] dedup check failed (non-fatal, 繼續執行): {_e}")
        data = market_open_picks.get_holiday_news_summary()
        if data.get("ai_text"):
            print(f"Gemini reasoning: {len(data['ai_text'])} chars")
        print(f"Got {len(data.get('news', []))} news items")
        msg = notifier.fmt_holiday_news(data)
    elif market == "crypto_picks":
        # B: 用戶關閉加密貨幣推播 — 早期 return, 即使有 manual dispatch 也不推
        print("[B] crypto_picks 已用戶關閉, 跳過")
        return 0
        # 下面是舊邏輯, 保留供日後參考 (unreachable)
        print("Running crypto picks (daily noon)...")
        try:
            import crypto_picker
            data = crypto_picker.get_crypto_picks(top_n=5)
            n_picks = len(data.get("picks", []) or [])
            print(f"Got {n_picks} crypto picks (universe scanned: {data.get('universe_size', 0)})")
            if data.get("market_context"):
                print(f"Market context: {data['market_context']}")
            msg = crypto_picker.fmt_crypto_picks_tg(data)
        except Exception as e:
            print(f"crypto_picks fatal failure: {e}", flush=True)
            err_msg = (
                f"<b>加密貨幣推播失敗</b>\n\n"
                f"原因: <code>{type(e).__name__}: {str(e)[:200]}</code>\n\n"
                f"建議檢查:\n"
                f"  • Gemini API key 是否有效\n"
                f"  • yfinance 是否被 rate-limit"
            )
            try:
                notifier.send_message(err_msg)
            except Exception:
                pass
            return 1
    elif market == "heartbeat":
        # 系統健康日報: 探外部 API + 印 ETF 資料新鮮度
        print("Running heartbeat health check...")
        try:
            import heartbeat
            msg = heartbeat.build_heartbeat_message()
            print(f"Heartbeat message length: {len(msg)} chars / {len(msg.encode('utf-8'))} bytes")
            ok, info = notifier.send_message(msg)
            print(f"Heartbeat TG: ok={ok}, info={info}")
            return 0 if ok else 2
        except Exception as e:
            import traceback
            print(f"Heartbeat check failed: {e}", flush=True)
            traceback.print_exc()
            return 1

    elif market == "monitor":
        # 盤中監控: 自選股 / 大盤點數 / 加密貨幣
        print("Running monitor mode (intraday alerts)...")

        # ===== 防禦性 early-exit (省 GH Actions 額度) =====
        # 為什麼: cron 已限定在 session 時段, 但 GH cron 可能 drift 跨小時誤觸發.
        #         若觸發時所有 market 都沒 session 且不在 crypto 時段, 跑完整流程是 ~30s
        #         浪費 (yfinance / state I/O 等). 直接 exit 0 跳過.
        try:
            import index_alerts as _ia_pre
            import datetime as _dt
            now_utc = _dt.datetime.utcnow()
            cur_hour = now_utc.hour
            in_any_session = any(
                _ia_pre._is_market_in_session(c) for c in ["TW", "JP", "KR", "US"]
            )
            is_crypto_hour = cur_hour in getattr(_ia_pre, "CRYPTO_SCHEDULE_UTC_HOURS", {})
            if not in_any_session and not is_crypto_hour:
                print(
                    f"Monitor mode: 無 market session 且非 crypto 時段 "
                    f"(UTC hour={cur_hour}). Early-exit 省 ~30s API. "
                    f"in_session: TW={_ia_pre._is_market_in_session('TW')}, "
                    f"JP={_ia_pre._is_market_in_session('JP')}, "
                    f"KR={_ia_pre._is_market_in_session('KR')}, "
                    f"US={_ia_pre._is_market_in_session('US')}"
                )
                return 0
        except Exception as _e:
            print(f"Session pre-check failed (non-fatal, 繼續執行): {_e}", flush=True)

        # I1 fix: 開啟 batched state I/O — monitor mode 內所有 check 共用 cache,
        # context 結束時一次 flush 到 GSheet. 從每 tick ~12 個 GSheet API call
        # 降到 2 個 (1 read + 1 write). atexit 註冊保險, 確保即使中途 return/exception
        # 也會 close + flush.
        import watchlist_store as _ws_batch
        import atexit as _atexit
        _ws_batch.open_batched_state()
        _atexit.register(_ws_batch.close_batched_state)

        # ===== Phase 1: 三個 index-class 警報合併成一封 TG =====
        # 為什麼: 之前 crash / reversal / bucket-alert 各自 send TG, 美股開盤 1 hr 內
        #         同 symbol 可能觸發 3-5 封. 合併後同 symbol 在「一封」訊息裡顯示所有觸發類型,
        #         大幅減少 TG noise.
        # 順序: crash 先 (state 更新給 reversal dedup 用), 再 reversal, 再 bucket
        crash_data = None
        crash_ai_text = ""
        reversal_alerts = []
        bucket_alerts = []

        try:
            import index_alerts as _ia
            crash_data = _ia.check_systemic_crash()
            if crash_data:
                print(
                    f"[systemic crash] triggered: {len(crash_data['triggers'])} 檔, "
                    f"今日第 {crash_data.get('alert_index', '?')}/{crash_data.get('max_per_day', '?')} 次",
                    flush=True,
                )
                # Gemini 動作建議 (只在 crash 觸發時跑)
                if ai_analyzer.gemini_available():
                    try:
                        ok, crash_ai_text = ai_analyzer.analyze_systemic_crash(crash_data)
                        if not ok:
                            print(f"[systemic crash] Gemini failed: {crash_ai_text}", flush=True)
                            crash_ai_text = ""
                    except Exception as _e:
                        print(f"[systemic crash] Gemini exception: {_e}", flush=True)
                        crash_ai_text = ""
        except Exception as _e:
            print(f"[systemic crash] check failed (non-fatal): {_e}", flush=True)

        try:
            import index_alerts as _ia_rev
            reversal_alerts = _ia_rev.check_intraday_reversal() or []
            if reversal_alerts:
                print(
                    f"[intraday reversal] triggered {len(reversal_alerts)}: "
                    + ", ".join(
                        f"{a.get('symbol')}({a.get('type')})" for a in reversal_alerts
                    ),
                    flush=True,
                )
        except Exception as _e:
            import traceback
            print(f"[intraday reversal] check failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

        # M3: 開盤即弱/即強警報 (跟 reversal 互補, 抓開盤後沒給機會的場景)
        weak_open_alerts = []
        try:
            import index_alerts as _ia_wo
            weak_open_alerts = _ia_wo.check_weak_open_alerts() or []
            # M1 fix: 同 sym 同 tick reversal 已推 → suppress weak_open, 避免重複 2 封
            #         (reversal 訊息語境更完整, 含 severity/速度/同向股)
            if weak_open_alerts and reversal_alerts:
                rev_syms = {a.get("symbol") for a in reversal_alerts}
                before = len(weak_open_alerts)
                weak_open_alerts = [
                    a for a in weak_open_alerts if a.get("symbol") not in rev_syms
                ]
                if before != len(weak_open_alerts):
                    print(
                        f"[weak/strong open] suppressed {before - len(weak_open_alerts)} "
                        f"(同 sym reversal 已推)",
                        flush=True,
                    )
            if weak_open_alerts:
                print(
                    f"[weak/strong open] triggered {len(weak_open_alerts)}: "
                    + ", ".join(
                        f"{a.get('symbol')}({a.get('type')})" for a in weak_open_alerts
                    ),
                    flush=True,
                )
        except Exception as _e:
            import traceback
            print(f"[weak/strong open] check failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

        # E: 砍 bucket index alerts — 用戶選關 (反轉+系統性+開盤即弱已 cover)
        # 不再呼叫 check_index_alerts(), 省每次 monitor tick 5 個指數的 yfinance fetch
        bucket_alerts = []

        # === 新增: 大盤大漲 → 強勢股推播 (跨 tick 去重, 每天每 trigger 只推一次) ===
        try:
            import strong_stock_alert as _ssa
            ssa_result = _ssa.check_and_push_if_surge()
            if ssa_result:
                print(f"[strong stock alert] {ssa_result}", flush=True)
        except Exception as _e:
            print(f"[strong stock alert] check failed (non-fatal): {_e}", flush=True)

        # === 新增: 常態 intraday 強勢個股推播 (不需大盤大漲, cooldown 90min, daily cap 3) ===
        try:
            import strong_stock_alert as _ssa2
            ssa2_result = _ssa2.check_and_push_intraday_strong()
            if ssa2_result:
                print(f"[intraday strong] {ssa2_result}", flush=True)
        except Exception as _e:
            print(f"[intraday strong] check failed (non-fatal): {_e}", flush=True)

        # === 4: 持倉 intraday 風險警報 (今日 ≤ -3% / 從早高回吐 ≥ 5% / 跌破停損) ===
        holdings_intraday_alerts = []
        try:
            import holdings_intraday_alert as _hi
            holdings_intraday_alerts = _hi.check_holdings_intraday_risk() or []
            if holdings_intraday_alerts:
                print(
                    f"[holdings intraday] triggered {len(holdings_intraday_alerts)}: "
                    + ", ".join(a.get("stock_id", "") for a in holdings_intraday_alerts),
                    flush=True,
                )
        except Exception as _e:
            import traceback
            print(f"[holdings intraday] check failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

        # === 5 (新): 事件型新聞推播 (Trump/FDA/buyback/併購 等命中關鍵字) ===
        news_event_alerts = []
        try:
            import news_event_alert as _ne
            news_event_alerts = _ne.check_news_events() or []
            if news_event_alerts:
                print(
                    f"[news event] triggered {len(news_event_alerts)} 則: "
                    + ", ".join(a.get("symbol", "") for a in news_event_alerts),
                    flush=True,
                )
                # C: 對 HIGH urgency 跑 Gemini 影響分析 (加進 message)
                try:
                    import news_impact_analyzer as _nia
                    impact_block = _nia.analyze_news_impact(news_event_alerts)
                except Exception as _ie:
                    print(f"[news event] impact analyzer failed (skip): {_ie}", flush=True)
                    impact_block = ""

                ne_msg = notifier.fmt_news_event_alerts(news_event_alerts,
                                                          impact_analysis=impact_block)
                if ne_msg:
                    # B: 響鈴控制
                    #  - 有 HIGH/MED urgency 響鈴
                    #  - 純 LOW 新聞 → 靜音
                    #  - 但持倉股 (tag=hold) 即使 LOW 也響鈴 (用戶在意)
                    has_urgent = any(
                        a.get("urgency") in ("HIGH", "MED") or a.get("tag") == "hold"
                        for a in news_event_alerts
                    )
                    ok_n, info_n = notifier.send_message(
                        ne_msg,
                        disable_preview=False,
                        disable_notification=(not has_urgent),
                    )
                    print(f"[news event] TG result: ok={ok_n} info={info_n} urgent={has_urgent}",
                          flush=True)
                    if ok_n:
                        try:
                            _ne.mark_alerts_sent(news_event_alerts)
                            print(f"[news event] state updated", flush=True)
                        except Exception as _me:
                            print(f"[news event] mark_alerts_sent failed: {_me}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[news event] check/send failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

        # === P2-A: 量爆突破 (Tier 1 — 立即響鈴) ===
        try:
            import volume_breakout_alert as _vb
            vb_alerts = _vb.check_volume_breakout() or []
            if vb_alerts:
                print(f"[vol_breakout] triggered {len(vb_alerts)}: "
                      + ", ".join(a.get("symbol", "") for a in vb_alerts),
                      flush=True)
                vb_msg = notifier.fmt_volume_breakout_alerts(vb_alerts)
                if vb_msg:
                    ok_vb, info_vb = notifier.send_message(
                        vb_msg, disable_preview=False,
                        disable_notification=False,  # Tier 1 響鈴
                    )
                    if ok_vb:
                        _vb.mark_alerts_sent(vb_alerts)
                        print(f"[vol_breakout] sent ok", flush=True)
                    else:
                        print(f"[vol_breakout] send fail: {info_vb}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[vol_breakout] failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

        # === P2-B: 籌碼異常 (Tier 2 — 響鈴批次) ===
        try:
            import chip_anomaly_alert as _ca
            ca_alerts = _ca.check_chip_anomaly() or []
            if ca_alerts:
                print(f"[chip_anomaly] triggered {len(ca_alerts)}", flush=True)
                ca_msg = notifier.fmt_chip_anomaly_alerts(ca_alerts)
                if ca_msg:
                    ok_ca, info_ca = notifier.send_message(
                        ca_msg, disable_preview=False,
                        disable_notification=False,  # Tier 2 響鈴
                    )
                    if ok_ca:
                        _ca.mark_alerts_sent(ca_alerts)
        except Exception as _e:
            import traceback
            print(f"[chip_anomaly] failed (non-fatal): {_e}", flush=True)

        # === Q2: 盤中強勢族群推播 (per-day cap 1, 全域 60min cooldown) ===
        strong_sector_alerts = []
        try:
            import strong_sector_alert as _ssec
            strong_sector_alerts = _ssec.check_strong_sectors_intraday() or []
            if strong_sector_alerts:
                print(
                    f"[strong sector] triggered {len(strong_sector_alerts)}: "
                    + ", ".join(
                        f"{a.get('sector_name')}({a.get('sector_type')})"
                        for a in strong_sector_alerts
                    ),
                    flush=True,
                )
        except Exception as _e:
            import traceback
            print(f"[strong sector] check failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

        # === A fix: 合 monitor 推播 (反轉+開盤即弱+強勢族群+大跌+bucket) ===
        # H1: fmt_combined_intraday_super 回 list[str], 一般合 1 封;
        # 三段太長時自動拆多封, 避免 byte 截斷 silent 砍掉後面段.
        try:
            super_msgs = notifier.fmt_combined_intraday_super(
                crash_data=crash_data,
                reversal_alerts=reversal_alerts,
                bucket_alerts=bucket_alerts,
                weak_open_alerts=weak_open_alerts,
                strong_sector_alerts=strong_sector_alerts,
                holdings_intraday_alerts=holdings_intraday_alerts,
                crash_ai_text=crash_ai_text,
            )
            if super_msgs:
                all_ok = True
                for i, msg in enumerate(super_msgs, 1):
                    print(
                        f"[super combined] sending {i}/{len(super_msgs)} ({len(msg)} chars)",
                        flush=True,
                    )
                    ok_c, info_c = notifier.send_message(msg)
                    print(f"[super combined] TG #{i} result: ok={ok_c} info={info_c}", flush=True)
                    if not ok_c:
                        all_ok = False
                # 只有「全部 send 成功」才 mark state (HIGH 防 silent fail)
                if all_ok:
                    if strong_sector_alerts:
                        try:
                            import strong_sector_alert as _ssec_mark
                            _ssec_mark.mark_sectors_sent(strong_sector_alerts)
                            print(
                                f"[strong sector] state updated: "
                                f"{len(strong_sector_alerts)} 族群 marked sent",
                                flush=True,
                            )
                        except Exception as _me:
                            print(f"[strong sector] mark_sectors_sent failed: {_me}", flush=True)
        except Exception as _e:
            print(f"[strong sector] handle fail: {_e}", flush=True)

    print("=== Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
