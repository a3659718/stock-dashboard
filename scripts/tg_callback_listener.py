
from __future__ import annotations

import sys
import time
import traceback

import requests
import html as _html

# 專案根目錄加進 path (讓 scripts/ 底下能 import 上層模組)
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_sources as ds  # noqa: E402

API_BASE = "https://api.telegram.org/bot{token}/{method}"
_OFFSET_KEY = "tg_update_offset"
_STOP_KEY = "stop_loss"
STOP_PCT = 0.08  # 預設停損 = 現價 -8% (個股常用風控)


# ---------------------------------------------------------------------------
# Bot API 薄封裝
# ---------------------------------------------------------------------------
def _token() -> str:
    return (ds._secret("TELEGRAM_BOT_TOKEN") or "").strip()


def _api(method: str, payload: dict, timeout: int = 35) -> dict:
    """呼叫 Bot API, 回傳 parsed json (失敗回 {})."""
    token = _token()
    if not token:
        return {}
    try:
        r = requests.post(API_BASE.format(token=token, method=method),
                          data=payload, timeout=timeout)
        return r.json()
    except Exception as e:
        print(f"[tg_listener] _api {method} fail: {type(e).__name__}: {e}", flush=True)
        return {}


def _answer(callback_id: str, text: str = "", alert: bool = False) -> None:
    """回 answerCallbackQuery — 一定要呼叫, 否則用戶端轉圈直到逾時."""
    if not callback_id:
        return
    _api("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text[:200],
        "show_alert": bool(alert),
    }, timeout=15)


def _reply(chat_id, text: str) -> None:
    """送一則 follow-up 訊息 (用 notifier 以共用 escape / retry / 截斷)."""
    try:
        import notifier
        notifier.send_message(text)
    except Exception:
        _api("sendMessage", {"chat_id": chat_id, "text": text[:4000]})

def _reply(chat_id, text: str) -> None:
    try:
        import notifier
        notifier.send_message(text, chat_id=chat_id)
    except Exception:
        # 退路: 直接打 API，但使用 html.escape 防止包含特殊符號導致 HTTP 400 錯誤
        safe_text = _html.escape(text[:4000], quote=False)
        _api("sendMessage", {"chat_id": chat_id, "text": safe_text})



    
def _load_offset() -> int:
    try:
        import watchlist_store
        st = watchlist_store.load_monitor_state() or {}
        return int(st.get(_OFFSET_KEY, 0) or 0)
    except Exception:
        return 0


def _save_offset(offset: int) -> None:
    try:
        import watchlist_store
        st = watchlist_store.load_monitor_state() or {}
        st[_OFFSET_KEY] = int(offset)
        watchlist_store.save_monitor_state(st)
    except Exception as e:
        print(f"[tg_listener] save offset fail (non-fatal): {e}", flush=True)


# ---------------------------------------------------------------------------
# callback_data 解析
# ---------------------------------------------------------------------------
def parse_callback(data: str):
    """'wl:TW:2330' -> ('wl','TW','2330'); 'wl:2330' -> ('wl','TW'/'US' 推斷,'2330').

    回 (action, market, sid) 或 None (格式不符).
    """
    if not data or ":" not in data:
        return None
    parts = data.split(":")
    action = parts[0].strip().lower()
    if action not in ("wl", "ai", "sl"):
        return None
    if len(parts) >= 3:
        market = parts[1].strip().upper() or "TW"
        sid = parts[2].strip()
    else:
        sid = parts[1].strip()
        # 向後相容: 台股代號是純數字 (含 0050 這種), 否則當美股
        market = "TW" if sid.isdigit() else "US"
    if not sid:
        return None
    if market not in ("TW", "US"):
        market = "TW"
    return action, market, sid


def _handle_wl(market: str, sid: str):
    try:
        import watchlist_store
        ok = watchlist_store.add_to_watchlist(sid, market=market)
        if ok:
            return f"✅ {sid} 已加入自選", None
        return f"⚠️ {sid} 加入失敗 (可能自選已滿)", None
    except Exception as e:
        print(f"[tg_listener] wl {sid} err: {e}", flush=True)
        return "⚠️ 加自選失敗", None


def _latest_price(market: str, sid: str):
    """回最新收盤價 (float) 或 None — 失敗不 raise."""
    try:
        if market == "US":
            q = ds.fetch_yf_quote(sid) or {}
            return q.get("last")
        df = ds.fetch_tw_stock_daily_one(sid, days=10)
        if df is None or getattr(df, "empty", True):
            return None
        for col in ("close", "Close", "收盤價", "收盤"):
            if col in df.columns:
                return float(df[col].iloc[-1])
        return None
    except Exception as e:
        print(f"[tg_listener] price {sid} err: {e}", flush=True)
        return None


def _handle_sl(market: str, sid: str):
    price = _latest_price(market, sid)
    if not price:
        return "⚠️ 抓不到現價, 無法設停損", None
    stop = round(price * (1 - STOP_PCT), 2)
    # 存進 monitor_state["stop_loss"][sid]
    try:
        import watchlist_store, datetime as _dt
        st = watchlist_store.load_monitor_state() or {}
        sl_map = st.get(_STOP_KEY) or {}
        sl_map[str(sid).upper()] = {
            "market": market, "ref_price": price, "stop": stop,
            "pct": STOP_PCT, "ts": _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        st[_STOP_KEY] = sl_map
        watchlist_store.save_monitor_state(st)
    except Exception as e:
        print(f"[tg_listener] sl save {sid} err: {e}", flush=True)
        return "⚠️ 停損儲存失敗", None
    cur = ("$%.2f" % price) if market == "US" else ("%.2f 元" % price)
    stp = ("$%.2f" % stop) if market == "US" else ("%.2f 元" % stop)
    follow = (f"🛡️ <b>{sid}</b> 已設停損\n"
              f"參考現價 {cur} → 建議停損 <b>{stp}</b> (-{int(STOP_PCT*100)}%)\n"
              f"<i>※ 機械式 -{int(STOP_PCT*100)}% 風控建議, 實際請依個股波動與部位調整。</i>")
    return f"🛡️ {sid} 停損設於 {stp}", follow


def _handle_ai(market: str, sid: str):
    """跑深度分析 + (有 key 時) Gemini 觀點, 回一則完整訊息."""
    try:
        import stock_deep_analyzer as sda
        deep = sda.get_deep_analysis(sid, market=market)
        context = sda.fmt_deep_analysis_for_prompt(deep) or "（無足夠深度資料）"
    except Exception as e:
        print(f"[tg_listener] ai deep {sid} err: {e}", flush=True)
        context = "（深度分析抓取失敗）"

    ai_text = ""
    try:
        import ai_analyzer
        if ai_analyzer.gemini_available():
            ai_text = _gemini_quick(ai_analyzer, sid, market, context)
    except Exception as e:
        print(f"[tg_listener] ai gemini {sid} err: {e}", flush=True)

    if not ai_text:
        ai_text = "（無 GEMINI key 或 AI 暫時無回應）\n\n" + context

    try:
        import notifier
        msg = notifier.fmt_ai_analysis(sid, "", ai_text)
    except Exception:
        msg = f"🤖 {sid} AI 分析\n{ai_text}"
    return f"🤖 {sid} 分析完成, 已送出", msg


def _gemini_quick(ai_analyzer, sid: str, market: str, context: str) -> str:
    """用 ai_analyzer 既有的 genai 設定跑一段單股快速分析."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=ai_analyzer.get_gemini_key())
        prompt = (
            f"你是台/美股短線交易顧問。針對 {market} 股 {sid}, 根據以下資料給"
            f"3-5 句精簡觀點 (本業亮點 / 風險 / 進場與風控建議)，用繁體中文，不要 markdown 標題:\n\n"
            f"{context}"
        )
        m = genai.GenerativeModel(ai_analyzer.DEFAULT_MODEL)
        resp = m.generate_content(
            prompt,
            safety_settings=ai_analyzer.get_safety_settings(),
            generation_config={"temperature": 0.4, "max_output_tokens": 600},
        )
        return (getattr(resp, "text", "") or "").strip()
    except Exception as e:
        print(f"[tg_listener] gemini_quick {sid} err: {e}", flush=True)
        return ""


_DISPATCH = {"wl": _handle_wl, "sl": _handle_sl, "ai": _handle_ai}


def handle_callback(cq: dict) -> None:
    """處理單個 callback_query dict (來自 getUpdates)."""
    cid = cq.get("id")
    data = cq.get("data") or ""
    chat_id = (((cq.get("message") or {}).get("chat") or {}).get("id"))
    parsed = parse_callback(data)
    if not parsed:
        _answer(cid, "⚠️ 無法辨識的動作")
        return
    action, market, sid = parsed
    # ai 較慢 — 先回 toast 讓轉圈停掉, 再做事
    if action == "ai":
        _answer(cid, f"🤖 {sid} 分析中…")
    try:
        toast, follow = _DISPATCH[action](market, sid)
    except Exception as e:
        print(f"[tg_listener] handler {action} {sid} crash: {e}\n{traceback.format_exc()}", flush=True)
        toast, follow = "⚠️ 處理失敗", None
    if action != "ai":
        _answer(cid, toast)
    if follow:
        _reply(chat_id, follow)


# ---------------------------------------------------------------------------
# 主迴圈
# ---------------------------------------------------------------------------
def drain_once(timeout: int = 0) -> int:
    """抓一批 updates 並處理, 回傳處理的 callback 數量."""
    offset = _load_offset()
    resp = _api("getUpdates", {
        "offset": offset, "timeout": timeout,
        "allowed_updates": '["callback_query"]',
    }, timeout=timeout + 20)
    if not resp or not resp.get("ok"):
        return 0
    updates = resp.get("result") or []
    n = 0
    max_uid = offset
    for up in updates:
        uid = up.get("update_id", 0)
        max_uid = max(max_uid, uid)
        cq = up.get("callback_query")
        if cq:
            handle_callback(cq)
            n += 1
    if updates:
        _save_offset(max_uid + 1)  # +1 = ack, 下次不再收這些
    return n


def main_loop() -> None:
    if not _token():
        print("[tg_listener] 缺 TELEGRAM_BOT_TOKEN, 結束。", flush=True)
        return
    print("[tg_listener] 啟動 long-poll getUpdates… (Ctrl-C 結束)", flush=True)
    backoff = 1
    while True:
        try:
            got = drain_once(timeout=30)  # long-poll 30s
            if got:
                print(f"[tg_listener] 處理 {got} 個 callback", flush=True)
            backoff = 1
        except KeyboardInterrupt:
            print("\n[tg_listener] 收到中斷, 結束。", flush=True)
            return
        except Exception as e:
            print(f"[tg_listener] loop err: {e}; {backoff}s 後重試", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# 乾跑自測 (不碰網路) — 驗證 parse + dispatch 結構
# ---------------------------------------------------------------------------
def _selftest() -> int:
    cases = {
        "wl:TW:2330": ("wl", "TW", "2330"),
        "ai:US:AAPL": ("ai", "US", "AAPL"),
        "sl:TW:0050": ("sl", "TW", "0050"),
        "wl:2330":    ("wl", "TW", "2330"),   # 舊格式, 純數字 → TW
        "ai:AAPL":    ("ai", "US", "AAPL"),   # 舊格式, 非數字 → US
        "tv:2330":    None,                    # url action 不該被當 callback
        "garbage":    None,
        "":           None,
    }
    fails = 0
    for data, expect in cases.items():
        got = parse_callback(data)
        ok = got == expect
        print(f"  {'OK ' if ok else 'FAIL'} parse_callback({data!r}) = {got} (expect {expect})")
        if not ok:
            fails += 1
    assert set(_DISPATCH) == {"wl", "ai", "sl"}, "dispatch keys 不齊"
    print(f"[selftest] {'PASS' if fails == 0 else f'{fails} FAIL'}")
    return fails


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--selftest":
        sys.exit(1 if _selftest() else 0)
    elif arg == "--once":
        print(f"[tg_listener] drain once → 處理 {drain_once(timeout=0)} 個 callback", flush=True)
    else:
        main_loop()
