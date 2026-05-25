"""
scripts/check_push_health.py

診斷各推播功能是否正常 (用戶本機跑).

檢查項目:
  1. TG 設定 (token / chat_id)
  2. FinMind token
  3. yfinance 抓資料
  4. 排程功能 import 測試
  5. 寄送一封測試訊息到 TG

用法:
    python scripts/check_push_health.py
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    ok_count = 0
    fail_count = 0

    def chk(label, fn):
        nonlocal ok_count, fail_count
        try:
            r = fn()
            if r:
                print(f"  ✓ {label}")
                ok_count += 1
            else:
                print(f"  ✗ {label} (回 falsy)")
                fail_count += 1
        except Exception as e:
            print(f"  ✗ {label}: {type(e).__name__}: {e}")
            fail_count += 1

    print("=== Phase 1: 環境變數 / secrets ===")
    import os
    chk("TELEGRAM_BOT_TOKEN", lambda: bool(os.environ.get("TELEGRAM_BOT_TOKEN")))
    chk("TELEGRAM_CHAT_ID", lambda: bool(os.environ.get("TELEGRAM_CHAT_ID")))
    chk("FINMIND_TOKEN", lambda: bool(os.environ.get("FINMIND_TOKEN")))
    chk("GEMINI_API_KEY (optional)", lambda: bool(os.environ.get("GEMINI_API_KEY")))

    print("\n=== Phase 2: 核心模組可 import ===")
    chk("import notifier", lambda: __import__("notifier"))
    chk("import data_sources", lambda: __import__("data_sources"))
    chk("import upside_screener", lambda: __import__("upside_screener"))
    chk("import us_upside_screener", lambda: __import__("us_upside_screener"))
    chk("import strong_stock_alert", lambda: __import__("strong_stock_alert"))
    chk("import scripts.morning_brief", lambda: __import__("scripts.morning_brief"))

    print("\n=== Phase 3: 推播函式可呼叫 ===")
    import notifier
    chk("notifier.is_configured()", notifier.is_configured)

    print("\n=== Phase 4: 抓資料測試 (yfinance / FinMind) ===")
    import data_sources as ds
    chk("yfinance ^TWII 5d", lambda: not ds.fetch_yf_history("^TWII", period="5d").empty)
    chk("yfinance ^SOX 5d", lambda: not ds.fetch_yf_history("^SOX", period="5d").empty)
    chk("yfinance NVDA news", lambda: len(ds.fetch_yahoo_news("NVDA", max_n=3)) >= 0)
    chk("yfinance fear&greed", lambda: bool(ds.fetch_fear_greed()))
    chk("FinMind TaiwanStockInfo", lambda: not ds.get_taiwan_stock_info().empty)

    print("\n=== Phase 5: 寄一封測試訊息到 TG ===")
    if notifier.is_configured():
        import datetime as dt
        test_msg = f"🔧 <b>健康檢查測試</b>\n<i>{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}</i>\n\n推播管道正常 ✓"
        chk("notifier.send_message()", lambda: notifier.send_message(test_msg)[0])
    else:
        print("  ⊘ TG 未設定, 跳過")

    print(f"\n{'='*40}")
    print(f"通過 {ok_count}, 失敗 {fail_count}")
    if fail_count == 0:
        print("✅ 所有推播功能應該都正常")
    else:
        print(f"⚠ 有 {fail_count} 項失敗, 上面的錯誤訊息看哪邊壞掉")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
