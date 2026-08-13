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


# === Monitor check timeout helper — 防單一 check 卡死 ===
def _run_with_timeout(fn, name: str, timeout_sec: int = 30, default=None):
    """跑一個函式, 給 timeout. 若超時或拋異常, log + return default.

    用 ThreadPoolExecutor 不用 signal (signal 在 thread / Windows 不能用).
    """
    import concurrent.futures as _cf
    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(fn)
            try:
                return fut.result(timeout=timeout_sec)
            except _cf.TimeoutError:
                print(f"[monitor timeout] {name} > {timeout_sec}s, skip", flush=True)
                # 不能 cancel running thread, 讓它自然結束 (背景跑完無影響)
                return default
            except Exception as e:
                print(f"[monitor error] {name}: {type(e).__name__}: {str(e)[:100]}", flush=True)
                return default
    except Exception as e:
        print(f"[monitor wrapper fail] {name}: {e}", flush=True)
        return default


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


def _run_us_open_main(market: str) -> bool:
    """美股開盤/中盤「主分析」(開盤後半小時) — 抽成獨立函式, 讓它在 handler 最前面優先送出。
    原本它排在 analyst/insider、利率週期、突破掃描等次要 alert 之後, 那些慢工 (各自抓網路)
    會把「開盤後半小時」主推延遲一小時級; 且配上 20 分 job timeout, 慢工吃光額度時主推會被
    整個砍掉、一則都收不到。改成「先送主推, 再跑次要 alert」→ 主推準時、且一定送得出去。
    回傳是否有送出。"""
    label = "開盤後 30 分鐘 (10:00 EDT)" if market == "us_open" else "開盤後 2 小時 (11:30 EDT)"
    print(f"Running US {label} (主分析優先送出)...", flush=True)
    try:
        data = market_open_picks.get_us_open_picks()
    except Exception as e:
        print(f"get_us_open_picks fatal failure: {e}", flush=True)
        try:
            notifier.send_message(
                f"<b>美股 {label} 推播失敗</b>\n\n原因: <code>{type(e).__name__}: {str(e)[:200]}</code>"
            )
        except Exception:
            pass
        return False
    if data.get("error"):
        print(f"data error: {data['error']}", flush=True)
    ai_text = ""
    if ai_analyzer.gemini_available():
        try:
            ok, ai_text = ai_analyzer.analyze_open_picks("US", _summarize_us_for_ai(data))
            if not ok:
                ai_text = ""
        except Exception as e:
            print(f"Gemini exception: {e}", flush=True)
            ai_text = ""
    try:
        msg = notifier.fmt_us_open_picks(data, ai_text=ai_text)
        if market == "us_mid":
            msg = msg.replace("美股開盤後 30 分鐘 · 資金流向",
                               "美股開盤後 2 小時 · 中盤更新 · 資金流向")
    except Exception as _fe:
        print(f"[{market}] fmt_us_open_picks 失敗 (non-fatal): {_fe}", flush=True)
        msg = ""
    if msg:
        ok_uo, info_uo = notifier.send_message(msg, disable_preview=True)
        print(f"[us_{('mid' if market == 'us_mid' else 'open')}] 主分析 TG send: ok={ok_uo}, info={info_uo}", flush=True)
        return True
    return False


def main() -> int:
    market = (sys.argv[1] if len(sys.argv) > 1 else "tw_open").lower()
    # 舊參數相容
    if market == "tw":
        market = "tw_open"
    if market == "us":
        market = "us_open"

    # 心跳: cron 每次啟動都記 (給 system_health 判定 cron 是否健康)
    try:
        import system_health as _sh_hb
        _sh_hb.record_cron_run(market)
    except Exception as _hbe:
        print(f"[heartbeat] record_cron_run fail (non-fatal): {_hbe}", flush=True)

    # === 排程去重守衛 (cron drift 防重複) ===
    # 一天只該推一次的 slot (tw_open / us_close / morning_recap …) 若因 cron drift
    # 被誤路由 / 兩個相鄰 cron 落進同一 slot → 同一則內容一天推兩次。這裡在送出前
    # claim 一次, window 內已送過就跳過。monitor 等「本來就多跑」的不套; 手動觸發
    # (workflow_dispatch) 也不套, 讓你能強制補推。fail-open: dedup 壞掉一律照送。
    try:
        import os as _os
        _is_manual = _os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        if not _is_manual:
            import push_dedup as _pd
            if _pd.should_guard(market) and not _pd.claim_slot(market):
                print(f"[push_dedup] '{market}' 視為重複 (cron drift), 本次跳過。", flush=True)
                return 0
    except Exception as _de:
        print(f"[push_dedup] 守衛例外, fail-open 照跑: {_de}", flush=True)

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
            # Bug fix: pre_market_* / morning_action_tw 等都是台股, 不是 US
            if (market.startswith("tw") or market.startswith("pre_market")
                    or market == "tw_post_market" or market == "morning_action_tw"):
                market_for_holiday = "TW"
            else:
                market_for_holiday = "US"
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
                _tp.mark_alerts_sent(tp_alerts)  # claim 在送出前 → 併發兩 tick 不會各送一次
                gem = _tp.analyze_with_gemini(tp_alerts)
                tp_msg = notifier.fmt_trump_policy_alerts(tp_alerts, gem)
                if tp_msg:
                    ok_tp, info_tp = notifier.send_message(
                        tp_msg, disable_preview=False, disable_notification=False
                    )
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
                _al.mark_alerts_sent(al_alerts)  # claim 在送出前 → 防併發重複
                al_msg = notifier.fmt_asia_leading_alerts(al_alerts)
                if al_msg:
                    ok_al, info_al = notifier.send_message(
                        al_msg, disable_preview=True, disable_notification=False
                    )
                    print(f"[asia_leading] sent ok={ok_al}", flush=True)
                    if not ok_al:
                        # 送失敗 → 回滾 claim, 讓下個 tick 重試 (否則整天漏掉這個日韓 leading)
                        try:
                            _al.unmark_alerts_sent(al_alerts)
                        except Exception:
                            pass
                else:
                    try:
                        _al.unmark_alerts_sent(al_alerts)
                    except Exception:
                        pass
        except Exception as _e:
            import traceback
            print(f"[asia_leading] failed: {_e}", flush=True)
            traceback.print_exc()

    # === 🏛 美股機構/內部人動向 (us_open 跑一次/天) ===
    # === 美股開盤/中盤『主分析』優先送出 (開盤後半小時) ===
    # 先送主推, 再跑下面的 analyst/insider、利率週期、突破掃描等次要 alert →
    # 「開盤後半小時」不再被那些慢工拖到一小時後, 也確保 timeout 內主推一定先出去。
    if market in ("us_open", "us_mid"):
        _run_us_open_main(market)

    if market == "us_open":
        try:
            import analyst_insider_alert as _ai
            ai_alerts = _ai.check_analyst_insider() or []
            if ai_alerts:
                print(f"[analyst_insider] triggered {len(ai_alerts)}", flush=True)
                _ai.mark_alerts_sent(ai_alerts)  # claim 在送出前 → 防併發重複
                gem = _ai.analyze_with_gemini(ai_alerts)
                ai_msg = notifier.fmt_analyst_insider_alerts(ai_alerts, gem)
                if ai_msg:
                    ok_a, info_a = notifier.send_message(
                        ai_msg, disable_preview=True, disable_notification=False,
                        category="analyst_insider",
                    )
                    print(f"[analyst_insider] sent ok={ok_a}", flush=True)
                    if not ok_a:
                        # 送失敗 / 被 daily cap 擋 → 回滾 claim, 讓下次能重試 (否則整天收不到)
                        try:
                            _ai.unmark_alerts_sent(ai_alerts)
                        except Exception:
                            pass
                else:
                    # fmt 回空 → 回滾 claim, 避免靜默吞掉
                    try:
                        _ai.unmark_alerts_sent(ai_alerts)
                    except Exception:
                        pass
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


    # === 🎯 美股走勢預測 (真正盤前: 跟 us_buy_picks 同時, 開盤前 ~1hr) ===
    # 原本掛在 us_open (開盤+30min) → 預測卻在開盤後才推, 名實不符。改掛 us_buy_picks,
    # 該 slot 已改為跟美股夏冬令連動、固定落在開盤前約 1 小時, 名副其實的盤前預測。
    if market == "us_buy_picks":
        try:
            import daily_outlook_advisor as _doa_us
            us_outlook = _doa_us.predict_us_outlook()
            us_ot_msg = _doa_us.format_outlook_for_tg(us_outlook)
            if us_ot_msg:
                ok_uo, _ = notifier.send_message(us_ot_msg, disable_preview=True)
                print(f"[us outlook] sent ok={ok_uo}", flush=True)
        except Exception as _ue:
            print(f"[us outlook] fail: {_ue}", flush=True)

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

    # === 🌅 morning_action 整合 ===
    # 砍掉獨立推播 — 內容已被 tw_open / us_open 主訊息涵蓋:
    #   - 大盤開盤實況 → tw_open / us_open 都已包含
    #   - 強勢族群 → strong_sector_alert 已推
    #   - BUY picks → actionable_picks / us_actionable 已推
    # morning_action_alert 模組保留供 dashboard 內顯示用

    # === 🚀 美股盤整突破 (US session 內每次 monitor 都掃) ===
    if market in ("us_open", "us_mid", "monitor"):
        try:
            import breakout_consolidation_alert as _bc
            bc_alerts = _bc.check_breakout_consolidation(top_n=5) or []
            if bc_alerts:
                print(f"[breakout] triggered {len(bc_alerts)}: "
                      + ", ".join(a.get("symbol", "") for a in bc_alerts),
                      flush=True)
                _bc.mark_alerts_sent(bc_alerts)  # claim 在送出前 → 防併發重複
                bc_msg = notifier.fmt_breakout_consolidation_alerts(bc_alerts)
                if bc_msg:
                    ok_bc, info_bc = notifier.send_message(
                        bc_msg, disable_preview=True, disable_notification=False
                    )
                    if not ok_bc:
                        # 送失敗 / 被 daily cap 擋 → 回滾 claim, 讓下個 tick 重試
                        try:
                            _bc.unmark_alerts_sent(bc_alerts)
                        except Exception:
                            pass
                else:
                    try:
                        _bc.unmark_alerts_sent(bc_alerts)
                    except Exception:
                        pass
        except Exception as _e:
            import traceback
            print(f"[breakout] failed (non-fatal): {_e}", flush=True)



    # === 📊 台股盤後總結 (TPE 16:00) ===
    if market == "tw_post_market":
        try:
            import tw_post_market_summary as _pms
            pms_msg = _pms.build_post_market_msg()
            if pms_msg:
                # 內容豐富 → 超過 TG 4096 就拆多封送 (以前直接截斷, Gemini 隔日策略整段被砍)
                _pms_parts = notifier._split_tg_msg(pms_msg)
                for _i, _pp in enumerate(_pms_parts, 1):
                    ok_pms, info_pms = notifier.send_message(
                        _pp, disable_preview=True,
                        disable_notification=(_i > 1),  # 只有第一封響鈴, 後續靜音
                    )
                    print(f"[tw_post_market] part {_i}/{len(_pms_parts)} sent ok={ok_pms} "
                          f"info={info_pms}", flush=True)
            else:
                print("[tw_post_market] 訊息為空 → 不送 (台股休市? 或資料全空)", flush=True)
        except Exception as _e:
            import traceback
            print(f"[tw_post_market] failed: {_e}", flush=True)
            traceback.print_exc()
        print("=== Post-market done ===")
        return 0

    # === 🏦 台股外資出貨嫌疑 (TPE 16:32, 等三大法人買賣超出齊才推) ===
    # 從 tw_close(15:03) 移出: 15:03 時今日三大法人還沒釋出, 只能拿到昨日的 5 日累計;
    # 16:32 三大法人已出齊 → 含今日外資動向, 名副其實。
    if market == "tw_foreign_chips":
        try:
            import holiday_check as _hc
            if _hc.is_market_closed_today("TW"):
                print("[tw_foreign_chips] TW 今日休市, skip", flush=True)
                return 0
        except Exception as _hce:
            print(f"[tw_foreign_chips] holiday check fail (continue anyway): {_hce}", flush=True)
        try:
            import closing_analyzer as _ca
            dumping = _ca.analyze_foreign_dumping(top_n=5, max_scan=80) or []
            fc_msg = notifier.fmt_foreign_dumping_alert(dumping)
            if fc_msg:
                ok_fc, info_fc = notifier.send_message(fc_msg, disable_preview=True)
                print(f"[tw_foreign_chips] sent {len(dumping)} 檔 ok={ok_fc} info={info_fc}", flush=True)
            else:
                print("[tw_foreign_chips] 今日無外資出貨嫌疑 → 不送", flush=True)
        except Exception as _e:
            import traceback
            print(f"[tw_foreign_chips] failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()
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
                print(f"[pre_market] {slot} sent ok={ok_pm} info={info_pm}", flush=True)
            else:
                # 之前這裡靜默不送、連原因都不印 → 「完全沒收到」卻查不到為什麼。
                print(f"[pre_market] {slot} 訊息為空 → 不送。可能原因: 台股休市判定 / "
                      f"美股+日韓資料全抓不到", flush=True)
        except Exception as _e:
            import traceback
            print(f"[pre_market] failed: {_e}", flush=True)
            traceback.print_exc()
        print("=== Pre-market done ===")
        return 0

    # 台股主推是否已「優先送出」(tw_close 會在下面合併後立刻送, 不等 chip_div/holdings 慢工)
    _tw_main_sent = False

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
        # 包 try: formatter 若在 FinMind 降級資料上炸, 不該讓整個 run exit 1 (下方 807 送出區已能處理空 msg)
        try:
            msg = notifier.fmt_tw_open_picks(data, ai_text=ai_text)
            # 中盤版本標題改一下
            if market == "tw_mid":
                msg = msg.replace("台股開盤後 30 分鐘 · 資金流向", "台股中盤更新 (11:00) · 資金流向")
        except Exception as _fe:
            print(f"[{market}] fmt_tw_open_picks 失敗 (non-fatal), 不送主分析: {_fe}", flush=True)
            msg = ""
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
        try:
            msg = notifier.fmt_tw_close_analysis(data)
        except Exception as _fe:
            print(f"[tw_close] fmt_tw_close_analysis 失敗 (non-fatal), 不送主分析: {_fe}", flush=True)
            msg = ""

        # === 合併 16:00「今日總結 + 隔日策略」進這封 15:00 盤後 (用戶要求 15:00+16:00 整合成一個) ===
        # 兩者資料 15:00 都已就緒 (加權/日韓已收盤); 過長會由 send_message 自動分段, 不截斷。
        try:
            import tw_post_market_summary as _pms
            _pm_msg = _pms.build_post_market_msg()
            if _pm_msg:
                msg = (msg + "\n\n━━━━━━━━━━━━━━━━━\n\n" + _pm_msg) if msg else _pm_msg
        except Exception as _pme:
            print(f"[tw_close] 併入今日總結 (non-fatal) 失敗: {_pme}", flush=True)

        # 主推優先送出 — 不等下面 chip_div (掃 80 檔) / holdings 慢工, 也確保 timeout 內先送出。
        if msg:
            ok_c, info_c = notifier.send_message(msg, disable_preview=True)
            print(f"[tw_close] 盤後總結(合併)優先送出: ok={ok_c} {info_c}", flush=True)
            _tw_main_sent = True

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

    # Bug fix (重大): tw_open/tw_mid/tw_close 組好主分析訊息 msg 卻「從未送出」, 而且這三個
    #   market 跑完都沒有 return → 一路 fall through 到結尾的 "Unknown market" → exit 2。
    #   結果台股開盤後30分 / 中盤更新 / 盤後分析三封主推播長期都沒送出還報錯。補上送出 + return。
    if market in ("tw_open", "tw_mid", "tw_close"):
        if _tw_main_sent:
            # tw_close 已在上面優先送出 (先送主推再跑 chip_div/holdings), 這裡不重送。
            print(f"[{market}] 主分析已優先送出, 略過重送", flush=True)
        elif msg:
            ok_tw, info_tw = notifier.send_message(msg, disable_preview=True)
            print(f"[{market}] 主分析 TG send: ok={ok_tw} {info_tw}", flush=True)
        else:
            print(f"[{market}] 主分析訊息為空, 不送 (可能 FinMind 失效/無資料)", flush=True)
        return 0

    # === IPO 今日上市推播 (pre_market_830 後跑) ===
    # 沒上市 silent skip; 有上市 → 推一封獨立含 Gemini 進場分析
    if market == "pre_market_830":
        try:
            import ipo_calendar_alert as _ipo
            ipo_result = _ipo.check_and_push(mode="today")
            print(f"[ipo_today] check: {ipo_result}", flush=True)
        except Exception as _ipoe:
            print(f"[ipo_today] check failed (non-fatal): {_ipoe}", flush=True)

    # === IPO 下週預告 (週五 us_open 跑, 14:00 UTC = 22:00 TPE) ===
    # 用戶要的「週五晚 22:00 推下週上市」— 不加新 cron, 用 us_open + Friday 判斷
    if market == "us_open":
        try:
            import datetime as _dt
            if _dt.datetime.utcnow().weekday() == 4:  # 週五
                import ipo_calendar_alert as _ipo
                # 週五推台股下週 IPO
                ipo_result = _ipo.check_and_push(mode="next_week", market="TW")
                print(f"[ipo_tw_next_week] friday preview: {ipo_result}", flush=True)
        except Exception as _ipoe:
            print(f"[ipo_next_week] check failed (non-fatal): {_ipoe}", flush=True)

    # 手動觸發 ipo_weekly (workflow_dispatch) — 推台股下週預告 (跟週五自動一致)
    if market == "ipo_weekly":
        try:
            import ipo_calendar_alert as _ipo
            ipo_result = _ipo.check_and_push(mode="next_week", market="TW")
            print(f"[ipo_tw_next_week] manual push: {ipo_result}", flush=True)
        except Exception as _ipoe:
            print(f"[ipo_tw_next_week] manual failed: {_ipoe}", flush=True)
        return 0

    # 早安推播 (UTC 23:30 = TPE 07:30 隔日) — 摘要昨夜美股 + 昨夜推播
    if market == "morning_recap":
        try:
            import morning_recap_alert as _mr
            r = _mr.check_and_push()
            print(f"[morning_recap] result: {r}", flush=True)
        except Exception as _mre:
            print(f"[morning_recap] failed: {_mre}", flush=True)
        return 0

    # 美股新興突破掃描 (平日 19:00 TPE = 11:00 UTC) — 池外廣掃, 獨立於核心 Top10
    if market == "us_emerging":
        print("Running US emerging breakout scan (19:00 TPE)...")
        try:
            import us_screener as _uss
            data = _uss.run_emerging_breakout(top_n=10)
            print(f"[us_emerging] scanned {data.get('scanned', 0)} / "
                  f"universe {data.get('universe_size', 0)}", flush=True)
            msg = notifier.fmt_emerging_breakout(data)
            if msg:
                ok_eb, info_eb = notifier.send_message(msg, disable_preview=True)
                print(f"[us_emerging] TG send: ok={ok_eb} {info_eb}", flush=True)
            else:
                print("[us_emerging] empty msg, skip send", flush=True)
        except Exception as e:
            import traceback
            print(f"[us_emerging] failed: {e}", flush=True)
            traceback.print_exc()
            return 1
        return 0

    # 美股盤前 BUY Top 5 (跟美股夏冬令連動, NYSE 開盤前 ~1hr; EDT 20:32 / EST 21:32 台北)
    # 只推 entry_label=BUY + score≥70 的; 沒符合就明確說「無高品質 BUY」
    if market == "us_buy_picks":
        # BUG #2 fix: 美股假日 (7/4, Thanksgiving, Christmas) 跳過, 避免推過時資料
        try:
            import holiday_check as _hc
            if _hc.is_market_closed_today("US"):
                print("[us_buy_picks] US market closed today, skip", flush=True)
                return 0
        except Exception as _hce:
            print(f"[us_buy_picks] holiday check fail (continue anyway): {_hce}", flush=True)
        try:
            import us_actionable as _ua
            import html as _html
            picks = _ua.compute_us_actionable_picks(top_n=10) or []
            buy_picks = [
                p for p in picks
                if p.get("symbol")
                and p.get("entry_label") == "BUY"
                and (p.get("entry_score") is None or float(p.get("entry_score") or 0) >= 70)
            ][:5]

            def _esc(s):
                return _html.escape(str(s) if s is not None else "", quote=False)

            lines = ["🇺🇸 <b>美股盤前 BUY Top 5</b> (NYSE 開盤前 ~1hr)"]
            lines.append("━━━━━━━━━━━━━━━━━")
            if not buy_picks:
                lines.append("🎯 <i>今日無高品質 BUY (entry_label=BUY + 分數 ≥70), 觀望為主</i>")
            else:
                for i, p in enumerate(buy_picks, 1):
                    sym = _esc(p.get("symbol", ""))
                    name = _esc(p.get("name", "") or p.get("company", ""))
                    sector = _esc(p.get("sector", "—"))
                    cur = p.get("current") or p.get("price")
                    el = p.get("entry_low")
                    eh = p.get("entry_high")
                    tgt = p.get("target") or p.get("target_mid")
                    stop = p.get("stop")
                    score = p.get("entry_score")
                    head = f"<b>{i}. {sym} {name}</b> [{sector}]"
                    if score is not None:
                        head += f" · 入場分 {float(score):.0f}"
                    lines.append(head)
                    if cur is not None and el and eh:
                        lines.append(f"   現價 ${cur} · 進場 ${el}~${eh}")
                    if tgt:
                        lines.append(f"   目標 ${tgt} · 停損 ${_esc(stop or '—')}")
                    if p.get("win_prob"):
                        lines.append(f"   勝率 {_esc(p['win_prob'])} · 持有 {_esc(p.get('hold_period','—'))}")
            msg = "\n".join(lines)
            notifier.send_message(msg)
            print(f"[us_buy_picks] sent {len(buy_picks)} BUY picks", flush=True)
        except Exception as _ube:
            print(f"[us_buy_picks] failed (non-fatal): {_ube}", flush=True)
        return 0

    if market in ("us_open", "us_mid"):
        # 主分析已在 handler 最前面 _run_us_open_main() 優先送出; 這裡的次要 alert 跑完就結束。
        return 0

    elif market == "us_close":
        print("Running US market close analysis (+2h, 18:00 EDT)...")
        try:
            data = market_open_picks.get_us_close_analysis()
            if data.get("ai_text"):
                print(f"Gemini reasoning: {len(data['ai_text'])} chars")
            msg = notifier.fmt_us_close_analysis(data)
            # BUG FIX (CRITICAL): 之前缺 send_message → us_close 不推
            if msg:
                ok_uc, info_uc = notifier.send_message(msg, disable_preview=True)
                print(f"[us_close] TG send: ok={ok_uc}, info={info_uc}")
                return 0 if ok_uc else 2
            else:
                print("[us_close] empty msg, skip send")
                return 0
        except Exception as e:
            import traceback
            print(f"us_close fatal: {e}", flush=True)
            traceback.print_exc()
            return 1

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
            # BUG FIX (CRITICAL): 之前只 build 沒 send → 用戶完全收不到週末推播
            if msg:
                ok, info = notifier.send_message(msg)
                print(f"[weekend_recap] TG send: ok={ok}, info={info}")
                if not ok:
                    return 2
            else:
                print("[weekend_recap] empty msg, skip send")
                return 0
            # 設 dedup flag — 讓 30 min 後的 holiday_news cron 知道週末摘要已推, 不要重複
            try:
                import watchlist_store
                state = watchlist_store.load_monitor_state()
                state["weekend_recap_last_fired"] = today_str
                watchlist_store.save_monitor_state(state)
                print(f"[weekend_recap] dedup flag set: weekend_recap_last_fired={today_str}")
            except Exception as _e:
                print(f"[weekend_recap] flag set failed (non-fatal): {_e}")
            return 0
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
        try:
            data = market_open_picks.get_holiday_news_summary()
            if data.get("ai_text"):
                print(f"Gemini reasoning: {len(data['ai_text'])} chars")
            print(f"Got {len(data.get('news', []))} news items")
            msg = notifier.fmt_holiday_news(data)
            # BUG FIX (CRITICAL): 之前 build 完沒 send → 假日新聞沒推
            if msg:
                ok, info = notifier.send_message(msg)
                print(f"[holiday_news] TG send: ok={ok}, info={info}")
                return 0 if ok else 2
            else:
                print("[holiday_news] empty msg, skip send")
                return 0
        except Exception as e:
            import traceback
            print(f"holiday_news fatal: {e}", flush=True)
            traceback.print_exc()
            return 1

    elif market == "crypto_picks":
        print("[crypto_picks] 已停用 (用戶要求取消加密貨幣)")
        return 0

    elif market == "heartbeat":
        # 系統健康日報: 探外部 API + 印 ETF 資料新鮮度
        # Bug fix #3: 順便跑 signal_tracker.evaluate_pending() — 累積真實勝率
        print("Running heartbeat health check...")
        try:
            import signal_tracker as _st_eval
            n_validated = _st_eval.evaluate_pending()
            print(f"[signal_tracker] auto-evaluate validated {n_validated} signals", flush=True)
        except Exception as _se:
            print(f"[signal_tracker] auto-evaluate failed (non-fatal): {_se}", flush=True)
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
            now_utc = _dt.datetime.now(_dt.timezone.utc)
            cur_hour = now_utc.hour
            in_any_session = any(
                _ia_pre._is_market_in_session(c) for c in ["TW", "JP", "KR", "US"]
            )
            # Bug fix: 加密貨幣已停用, 不再保留 crypto_hour fallback
            if not in_any_session:
                print(
                    f"Monitor mode: 無 market session "
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
            # B: 跨類去重 — 同 symbol 30min 內已推過 → skip
            try:
                import alert_priority as _ap
                original_n = len(reversal_alerts)
                reversal_alerts = _ap.filter_dedup_picks(
                    reversal_alerts, "intraday_reversal", "down"
                )
                if original_n > len(reversal_alerts):
                    print(f"[intraday reversal] dedup 過濾 {original_n - len(reversal_alerts)}", flush=True)
            except Exception:
                pass
            if reversal_alerts:
                print(
                    f"[intraday reversal] triggered {len(reversal_alerts)}: "
                    + ", ".join(
                        f"{a.get('symbol')}({a.get('type')})" for a in reversal_alerts
                    ),
                    flush=True,
                )
                # mark 已推 (送 TG 成功後再標, 但 super combined 流程後段才送, 在此先標保險)
                try:
                    import alert_priority as _ap2
                    _ap2.mark_picks_pushed(reversal_alerts, "intraday_reversal", "down")
                except Exception:
                    pass
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
            # B: 跨類去重 — 同 symbol+方向 30min 內已推
            if weak_open_alerts:
                try:
                    import alert_priority as _ap_wo
                    # weak_open 含 type=weak/strong, direction 對應 down/up
                    weak_subset = [a for a in weak_open_alerts if a.get("type") in ("weak",)]
                    strong_subset = [a for a in weak_open_alerts if a.get("type") in ("strong",)]
                    weak_subset = _ap_wo.filter_dedup_picks(weak_subset, "weak_open", "down")
                    strong_subset = _ap_wo.filter_dedup_picks(strong_subset, "strong_open", "up")
                    weak_open_alerts = weak_subset + strong_subset
                except Exception:
                    pass
            if weak_open_alerts:
                print(
                    f"[weak/strong open] triggered {len(weak_open_alerts)}: "
                    + ", ".join(
                        f"{a.get('symbol')}({a.get('type')})" for a in weak_open_alerts
                    ),
                    flush=True,
                )
                # mark 已推
                try:
                    import alert_priority as _ap_wo2
                    for a in weak_open_alerts:
                        d = "down" if a.get("type") == "weak" else "up"
                        _ap_wo2.mark_picks_pushed([a],
                                                    "weak_open" if d == "down" else "strong_open", d)
                except Exception:
                    pass
        except Exception as _e:
            import traceback
            print(f"[weak/strong open] check failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

        # E: 砍 bucket index alerts — 用戶選關 (反轉+系統性+開盤即弱已 cover)
        # 不再呼叫 check_index_alerts(), 省每次 monitor tick 5 個指數的 yfinance fetch
        bucket_alerts = []

        # === Phase 1 送出: 把 crash + 反轉 + 開盤即弱 合併成 TG (可能多封) ===
        # Bug fix (重大): 之前這三類只「偵測 + 標記已推」(reversal 在 1132 行先標保險),
        #   但「組合 + send」整段在某次重構被刪掉 → 費半反轉 / 系統性大跌 / 開盤即弱 全部被
        #   偵測卻從未送出, 而且還被標成已推 → 下次直接被去重濾掉。這裡把缺失的送出補回來。
        # 反轉 / 開盤即弱強 也接 Gemini: 若 crash 沒觸發 AI 但反轉/開盤即弱強有觸發, 補一份 AI 快評
        if not crash_ai_text and (reversal_alerts or weak_open_alerts) and ai_analyzer.gemini_available():
            try:
                _okr, _rev_ai = ai_analyzer.analyze_reversal_alerts(reversal_alerts, weak_open_alerts)
                if _okr and _rev_ai:
                    crash_ai_text = _rev_ai  # 共用同一個 AI 欄位傳給合併格式化
                    print("[reversal] Gemini 快評已生成", flush=True)
                else:
                    print(f"[reversal] Gemini 無回應/失敗: {_rev_ai}", flush=True)
            except Exception as _re:
                print(f"[reversal] Gemini exception: {_re}", flush=True)

        try:
            if crash_data or reversal_alerts or weak_open_alerts:
                _combined_parts = notifier.fmt_combined_intraday_super(
                    crash_data=crash_data,
                    reversal_alerts=reversal_alerts,
                    bucket_alerts=bucket_alerts,
                    weak_open_alerts=weak_open_alerts,
                    crash_ai_text=crash_ai_text,
                )
                for _cp in (_combined_parts or []):
                    if _cp:
                        _okc, _infoc = notifier.send_message(_cp, disable_preview=True)
                        print(f"[combined intraday] sent ok={_okc}: {_infoc}", flush=True)
        except Exception as _ce:
            import traceback
            print(f"[combined intraday] send failed (non-fatal): {_ce}", flush=True)
            traceback.print_exc()

        # === 新增: 大盤大漲 → 強勢股推播 (跨 tick 去重, 每天每 trigger 只推一次) ===
        # 關鍵: 先初始化 ssa_result。否則 check_and_push_if_surge() 一旦 raise (FinMind 掛時很常見),
        # except 雖吞掉原例外, 但 ssa_result 從未綁定 → 下方 line 1318 `if ssa_result:` 會 NameError
        # 且「不在 try 內」→ 整個 monitor run exit 1, 後面所有 monitor 推播 (強弱勢股/台指期/自選股/
        # 持倉/新聞事件/量爆/籌碼異常) 全部被連坐掉。
        ssa_result = None
        try:
            import strong_stock_alert as _ssa
            ssa_result = _ssa.check_and_push_if_surge()
            if ssa_result:
                print(f"[strong stock alert] {ssa_result}", flush=True)
        except Exception as _e:
            print(f"[strong stock alert] check failed (non-fatal): {_e}", flush=True)

        # === 常態 intraday 強勢個股推播 (帶 timeout 防卡死) ===
        # 合併重疊: 「大盤大漲警報」本身已含『當下強勢股』。若本 tick 已推大盤大漲,
        #           就跳過常態強勢股, 避免同一批強勢股在兩封訊息重複出現;
        #           大盤平淡 (surge 未觸發) 時才推常態強勢股。
        if ssa_result:
            print("[intraday strong] 本 tick 已推大盤大漲(含當下強勢股) → 跳過常態強勢股避免重複",
                  flush=True)
        else:
            def _strong_check():
                import strong_stock_alert as _ssa2
                return _ssa2.check_and_push_intraday_strong()
            ssa2_result = _run_with_timeout(_strong_check, "intraday_strong", timeout_sec=60)
            if ssa2_result:
                print(f"[intraday strong] {ssa2_result}", flush=True)

        # === 新增: 常態 intraday 弱勢個股推播 (短空候選, 多空雙向; 帶 timeout) ===
        def _weak_check():
            import short_candidates as _sc
            return _sc.check_and_push_intraday_weak()
        sc_result = _run_with_timeout(_weak_check, "intraday_weak", timeout_sec=60)
        if sc_result:
            print(f"[intraday weak] {sc_result}", flush=True)

        # === A: 台指期 / 微台專屬 alert (升貼水 + 法人 + 散戶反指標) ===
        def _tx_check():
            import tx_futures_alert as _tx
            return _tx.check_and_push()
        tx_result = _run_with_timeout(_tx_check, "tx_futures", timeout_sec=60)
        if tx_result:
            print(f"[tx_futures] {tx_result}", flush=True)

        # === watchlist_triggers 整合到 16:00 盤後 ===
        # 盤中不再獨立推播 (避免盤中分心), 改成 tw_post_market_summary 一次顯示當天累積觸發
        # 仍 check + 寫 state, 給盤後用
        try:
            import watchlist_triggers as _wt
            fired = _wt.check_triggers() or []
            if fired:
                print(f"[watchlist_triggers] {len(fired)} fired (queued for 16:00 summary)", flush=True)
                # mark 觸發 → 累積到 state, 給 16:00 盤後拉
                try:
                    import watchlist_store
                    state = watchlist_store.load_monitor_state()
                    wt_today = state.setdefault("watchlist_triggers_today", [])
                    import datetime as _dt
                    today_str = _dt.date.today().strftime("%Y-%m-%d")
                    # 過濾掉舊日的, 只留今天
                    wt_today = [t for t in wt_today if t.get("date") == today_str]
                    # 加入新觸發
                    for f in fired:
                        if not any(t.get("stock_id") == f.get("stock_id") and
                                   t.get("trigger_type") == f.get("trigger_type")
                                   for t in wt_today):
                            wt_today.append({
                                "date": today_str,
                                "stock_id": f.get("stock_id"),
                                "trigger_type": f.get("trigger_type"),
                                "current": f.get("current"),
                                "value": f.get("value"),
                            })
                    state["watchlist_triggers_today"] = wt_today
                    watchlist_store.save_monitor_state(state)
                except Exception:
                    pass
        except Exception as _wte:
            print(f"[watchlist_triggers] check failed (non-fatal): {_wte}", flush=True)

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
                # 防重複: 送出「之前」就先 claim (標記已推), 把併發/連跑兩 run 的競爭視窗從
                #         「Gemini+送出數秒」縮到毫秒; 送失敗再 unmark 回滾, 不會漏推。
                _ne_claimed = False
                try:
                    _ne.mark_alerts_sent(news_event_alerts)
                    _ne_claimed = True
                except Exception as _me:
                    print(f"[news event] pre-claim failed (continue): {_me}", flush=True)

                # C: 對 HIGH urgency 跑 Gemini 影響分析 (加進 message)
                try:
                    import news_impact_analyzer as _nia
                    impact_block = _nia.analyze_news_impact(news_event_alerts)
                except Exception as _ie:
                    print(f"[news event] impact analyzer failed (skip): {_ie}", flush=True)
                    impact_block = ""

                # 用戶要求: 急報(HIGH) + 注意(MED) 都只送「有 AI 分析」的版本。若這批含 HIGH/MED
                # 事件卻拿不到 AI 分析 (Gemini 失敗/不可用), 不送這封無 AI 版, 回滾 claim 讓下個 tick
                # (Gemini 恢復時) 補送完整版; 純 LOW 一般快訊無 AI 屬正常, 照送不受影響。
                _need_ai = any(a.get("urgency") in ("HIGH", "MED") for a in news_event_alerts)
                ne_msg = notifier.fmt_news_event_alerts(news_event_alerts,
                                                          impact_analysis=impact_block)
                if _need_ai and not impact_block:
                    print("[news event] 急報/注意 缺 AI 分析 → 跳過送出 + 回滾 claim, 等下個 tick 補 AI 版",
                          flush=True)
                    if _ne_claimed:
                        try:
                            _ne.unmark_alerts_sent(news_event_alerts)
                        except Exception as _me:
                            print(f"[news event] unmark after skip failed: {_me}", flush=True)
                elif ne_msg:
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
                        category="news_event",
                    )
                    print(f"[news event] TG result: ok={ok_n} info={info_n} urgent={has_urgent}",
                          flush=True)
                    # 已在送出前 claim; 送失敗才回滾 (讓下次能重試), 成功則維持已標記.
                    if not ok_n and _ne_claimed:
                        try:
                            _ne.unmark_alerts_sent(news_event_alerts)
                            print(f"[news event] send failed → 回滾 claim", flush=True)
                        except Exception as _me:
                            print(f"[news event] unmark failed: {_me}", flush=True)
                elif _ne_claimed:
                    # fmt 回空 (無可顯示內容) 但已 claim → 回滾, 避免靜默吞掉這批事件.
                    try:
                        _ne.unmark_alerts_sent(news_event_alerts)
                    except Exception:
                        pass
        except Exception as _e:
            import traceback
            print(f"[news event] check/send failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

        # === P2-A: 量爆突破 (Tier 1 — 立即響鈴) ===
        try:
            import volume_breakout_alert as _vb
            vb_alerts = _vb.check_volume_breakout() or []
            # 跨類去重: 濾掉本 tick 已被強勢股/大盤大漲等看多推播推過的同一檔 (共用去重命名空間)
            if vb_alerts:
                try:
                    import alert_priority as _apx
                    _n0 = len(vb_alerts)
                    vb_alerts = _apx.filter_dedup_picks(vb_alerts, "intraday_strong_stock", "up")
                    if _n0 != len(vb_alerts):
                        print(f"[vol_breakout] 跨類去重濾掉 {_n0 - len(vb_alerts)} 檔 (已被看多推播推過)",
                              flush=True)
                except Exception:
                    pass
            if vb_alerts:
                print(f"[vol_breakout] triggered {len(vb_alerts)}: "
                      + ", ".join(a.get("symbol", "") for a in vb_alerts),
                      flush=True)
                _vb.mark_alerts_sent(vb_alerts)  # claim 在送出前 → 防併發重複
                vb_msg = notifier.fmt_volume_breakout_alerts(vb_alerts)
                if vb_msg:
                    ok_vb, info_vb = notifier.send_message(
                        vb_msg, disable_preview=False,
                        disable_notification=False,  # Tier 1 響鈴
                        category="volume_breakout",
                    )
                    if ok_vb:
                        print(f"[vol_breakout] sent ok", flush=True)
                        try:
                            import alert_priority as _apx2
                            _apx2.mark_picks_pushed(vb_alerts, "intraday_strong_stock", "up")
                        except Exception:
                            pass
                    else:
                        # 送失敗 / 被 daily cap 擋 → 回滾 claim, 讓下個 tick 重試 (否則永不送出)
                        print(f"[vol_breakout] send fail: {info_vb} → 回滾 claim", flush=True)
                        try:
                            _vb.unmark_alerts_sent(vb_alerts)
                        except Exception as _ue:
                            print(f"[vol_breakout] unmark failed: {_ue}", flush=True)
                else:
                    # fmt 回空 → 回滾 claim, 避免靜默吞掉這批 alert
                    try:
                        _vb.unmark_alerts_sent(vb_alerts)
                    except Exception:
                        pass
        except Exception as _e:
            import traceback
            print(f"[vol_breakout] failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()


        # === P2-B: chip anomaly (Tier 2) ===
        try:
            import chip_anomaly_alert as _ca
            ca_alerts = _ca.check_chip_anomaly() or []
            # 跨類去重: 濾掉本 tick 已被強勢股/量爆突破等看多推播推過的同一檔
            if ca_alerts:
                try:
                    import alert_priority as _apc
                    _nc0 = len(ca_alerts)
                    ca_alerts = _apc.filter_dedup_picks(ca_alerts, "intraday_strong_stock", "up")
                    if _nc0 != len(ca_alerts):
                        print(f"[chip_anomaly] 跨類去重濾掉 {_nc0 - len(ca_alerts)} 檔 (已被看多推播推過)",
                              flush=True)
                except Exception:
                    pass
            if ca_alerts:
                print(f"[chip_anomaly] triggered {len(ca_alerts)}", flush=True)
                _ca.mark_alerts_sent(ca_alerts)  # claim 在送出前 → 防併發重複
                ca_msg = notifier.fmt_chip_anomaly_alerts(ca_alerts)
                if ca_msg:
                    ok_ca, info_ca = notifier.send_message(
                        ca_msg, disable_preview=False,
                        disable_notification=False,
                        category="chip_anomaly",
                    )
                    if ok_ca:
                        print("[chip_anomaly] sent ok", flush=True)
                        try:
                            import alert_priority as _apc2
                            _apc2.mark_picks_pushed(ca_alerts, "intraday_strong_stock", "up")
                        except Exception:
                            pass
                    else:
                        # 送失敗 / 被 daily cap 擋 → 回滾 claim, 讓下個 tick 重試 (否則永不送出)
                        print(f"[chip_anomaly] send fail: {info_ca} → 回滾 claim", flush=True)
                        try:
                            _ca.unmark_alerts_sent(ca_alerts)
                        except Exception as _ue:
                            print(f"[chip_anomaly] unmark failed: {_ue}", flush=True)
                else:
                    # fmt 回空 → 回滾 claim, 避免靜默吞掉這批 alert
                    try:
                        _ca.unmark_alerts_sent(ca_alerts)
                    except Exception:
                        pass
        except Exception as _e:
            import traceback
            print(f"[chip_anomaly] failed (non-fatal): {_e}", flush=True)
            traceback.print_exc()

        return 0

    print(f"Unknown market: {market}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
