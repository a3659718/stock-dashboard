"""
smoke_test_push.py
推播管道健康度快速檢測 — 找出「為何整天沒收到任何 TG 推播」的根因.

用法:
  本機:  python scripts/smoke_test_push.py
  GH Actions: workflow_dispatch market=heartbeat (或新增 smoke_test option)
  或直接 cli: python scripts/smoke_test_push.py --send  (會發測試訊息到 TG)

檢查項目:
  1. Telegram secrets 是否設定 (TOKEN / CHAT_ID)
  2. FinMind token 是否設定
  3. Gemini API key 是否設定
  4. yfinance 是否能拿 ^TWII 即時資料
  5. FinMind 是否能呼叫 (TaiwanStockInfo)
  6. 可選: 發一封測試 TG 訊息 (--send)
  7. 印出今日 GH Actions 應該跑的 cron 時間 (對照用)
"""
from __future__ import annotations

import os
import sys
import types
import datetime as dt
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# Streamlit stub (本機沒裝 streamlit 也能跑)
try:
    import streamlit as _st  # noqa: F401
except ImportError:
    _stub = types.ModuleType("streamlit")

    def _passthrough(*a, **kw):
        if len(a) == 1 and callable(a[0]):
            return a[0]

        def deco(f):
            return f
        return deco

    _stub.cache_data = _passthrough
    _stub.cache_resource = _passthrough
    _stub.secrets = {}
    _stub.warning = lambda *a, **k: None
    _stub.info = lambda *a, **k: None
    _stub.error = lambda *a, **k: None
    sys.modules["streamlit"] = _stub


def _hr(title=""):
    print("\n" + "=" * 60)
    if title:
        print(title)
        print("=" * 60)


def check_env_vars() -> dict:
    """檢查 secrets / 環境變數."""
    _hr("1) 環境變數 / Secrets 檢查")
    items = {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID":   os.environ.get("TELEGRAM_CHAT_ID", ""),
        "FINMIND_TOKEN":      os.environ.get("FINMIND_TOKEN", ""),
        "GEMINI_API_KEY":     os.environ.get("GEMINI_API_KEY", ""),
    }
    status = {}
    for k, v in items.items():
        if v:
            print(f"  ✅ {k}: 已設定 (長度 {len(v)})")
            status[k] = True
        else:
            print(f"  ❌ {k}: 未設定 — 此項相關推播會直接 silent fail")
            status[k] = False
    return status


def check_yfinance() -> bool:
    """yfinance 是否能拿到 ^TWII."""
    _hr("2) yfinance 連線 (取 ^TWII 2 日 5m)")
    try:
        import data_sources as ds
        df = ds.fetch_yf_history("^TWII", period="2d", interval="5m")
        if df is None or df.empty:
            print("  ❌ 回空 DataFrame — yfinance 可能被擋或假日 / 盤前")
            return False
        print(f"  ✅ 拿到 {len(df)} 根 bar, 最新時間: {df.index[-1] if hasattr(df, 'index') else 'unknown'}")
        return True
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        return False


def check_finmind() -> bool:
    """FinMind 是否能 fetch 台股清單 (用 cache 不要直接打 API 避免吃額度)."""
    _hr("3) FinMind 連線 (TaiwanStockInfo)")
    try:
        import data_sources as ds
        df = ds.get_taiwan_stock_info()
        if df is None or df.empty:
            print("  ⚠️ 回空 — 可能 402 額度爆 + 無本地 fallback cache")
            return False
        print(f"  ✅ 拿到 {len(df)} 檔台股清單")
        return True
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        return False


def send_test_message() -> bool:
    """發測試訊息到 TG."""
    _hr("4) 發測試訊息到 Telegram")
    try:
        import notifier
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        msg = f"🧪 <b>Smoke Test</b>\n推播管道測試訊息\n時間: {now}"
        ok, info = notifier.send_message(msg)
        if ok:
            print(f"  ✅ 推送成功: {info}")
            return True
        else:
            print(f"  ❌ 推送失敗: {info}")
            return False
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
        return False


def print_expected_cron_times():
    """印出今日 GH Actions 應該跑的 cron 時間 (給用戶對照 Actions tab)."""
    _hr("5) 今日預期 cron 時間 (對照 GH Actions Runs)")
    now_utc = dt.datetime.now(dt.timezone.utc)
    print(f"  現在 UTC: {now_utc.strftime('%Y-%m-%d %H:%M')}")
    print(f"  現在 TPE: {(now_utc + dt.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')}")
    print()
    print("  morning_brief (TPE 08:00 = UTC 00:00, Mon-Fri):")
    print("    cron: 0 0 * * 1-5")
    print()
    print("  market_open_alert 關鍵 cron (今日應跑):")
    today_dow = now_utc.weekday()  # Mon=0 ... Sun=6
    if today_dow < 5:
        crons = [
            ("UTC 01:15 / TPE 09:15", "亞股 monitor (反轉 + 強勢族群)"),
            ("UTC 01:30 / TPE 09:30", "台股開盤分析"),
            ("UTC 03:00 / TPE 11:00", "台股中盤"),
            ("UTC 03:15 / TPE 11:15", "亞股 monitor"),
            ("UTC 05:15 / TPE 13:15", "亞股 monitor"),
            ("UTC 07:00 / TPE 15:00", "台股盤後"),
        ]
        for time, desc in crons:
            print(f"    {time:30s} → {desc}")
    else:
        print("    (今日週末, 只跑 weekend_recap)")


def main():
    print("=" * 60)
    print("Smoke Test — 推播管道健康度檢查")
    print("=" * 60)

    env = check_env_vars()
    yf_ok = check_yfinance()
    fm_ok = check_finmind()

    send = "--send" in sys.argv
    if send:
        if not env.get("TELEGRAM_BOT_TOKEN") or not env.get("TELEGRAM_CHAT_ID"):
            print("\n  ⏭️  --send 已要求但 TG secrets 沒設, 跳過")
            tg_ok = False
        else:
            tg_ok = send_test_message()
    else:
        print("\n  ⏭️  跳過 TG 測試訊息 (要送請加 --send)")
        tg_ok = None

    print_expected_cron_times()

    # 結論
    _hr("結論")
    if not env.get("TELEGRAM_BOT_TOKEN") or not env.get("TELEGRAM_CHAT_ID"):
        print("  🔴 致命: TG secrets 沒設 → 全部 send_message 都會 silent fail")
        print("     → 檢查 GH Actions repo settings → Secrets and variables → Actions")
        return 1
    if not env.get("FINMIND_TOKEN"):
        print("  🟠 警告: FINMIND_TOKEN 沒設 → 台股 FinMind 相關推播都會空 / 失敗")
    if not yf_ok:
        print("  🟠 警告: yfinance 不通 → 反轉 / 強勢族群推播都會抓不到資料")
    if not fm_ok:
        print("  🟠 警告: FinMind get_taiwan_stock_info 失敗")
    if tg_ok is True:
        print("  ✅ TG 推送 OK — 如果你沒收到, 檢查是否 chat_id 寫錯成別人/別群")
    elif tg_ok is False:
        print("  🔴 TG 推送失敗 — 看上面的 info 訊息找原因 (常見: bot 被 user 封鎖)")
    elif env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        print("  ℹ️  TG secrets OK 但沒實際測 — 加 --send 來實測")
    return 0


if __name__ == "__main__":
    sys.exit(main())
