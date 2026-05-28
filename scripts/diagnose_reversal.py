"""
diagnose_reversal.py
診斷今日台股反轉警報「應觸發卻沒推」的原因.

用法:
  python scripts/diagnose_reversal.py
  python scripts/diagnose_reversal.py ^TWII

從 yfinance 抓今日 5m bars, 算 drawdown_pct vs reversal_pct threshold,
比對 monitor_state 該 sym 的 ratchet/cooldown/daily-cap 狀態.

輸出:
  ✅ 應觸發 — 顯示具體原因; 沒推大概是 cron 沒跑到、push 沒生效、或 state stale
  ❌ 不應觸發 — 顯示距離 threshold 多遠

可用於 GH Actions 手動 dispatch (workflow_dispatch market=monitor) 後檢查.
"""
from __future__ import annotations

import sys
import types
import datetime as dt
from pathlib import Path

# 讓 repo root 可被 import
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

# MED fix: 沒裝 streamlit 的環境 (local CLI 開發) 也能跑診斷
# 對齊 scripts/market_open_alert.py 的 streamlit stub
try:
    import streamlit as _st  # noqa: F401
except ImportError:
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

import index_alerts as ia
import data_sources as ds
import watchlist_store


def diagnose_sym(sym: str = "^TWII") -> None:
    cfg = ia.INDEX_CONFIG.get(sym)
    if not cfg:
        print(f"❌ {sym} 不在 INDEX_CONFIG, 跳過")
        return

    threshold = float(cfg.get("reversal_pct", ia.REVERSAL_THRESHOLD_PCT))
    print(f"\n{'='*60}")
    print(f"診斷 {sym} ({cfg.get('name')}) — reversal_pct threshold: {threshold}%")
    print(f"{'='*60}")

    # 1) 抓今日數據
    snap = ia._fetch_intraday_anchor_data(sym)
    if not snap:
        print(f"❌ _fetch_intraday_anchor_data 回 None — 可能 yfinance 資料 stale 或假日")
        print(f"   sys_today (UTC date): {dt.date.today()}")
        print(f"   提示: 用 fetch_yf_history('{sym}', period='2d', interval='5m') 看實際 bar")
        return

    today_open = snap["today_open"]
    current = snap["current"]
    today_high = snap["today_high"]
    today_low = snap["today_low"]
    mins_since_high = snap.get("mins_since_high")
    mins_since_open = snap.get("mins_since_open")
    bar_count = snap.get("bar_count", 0)
    vol_ratio = snap.get("vol_ratio")

    drawdown_pct = (current / today_high - 1) * 100 if today_high > 0 else 0
    rebound_pct = (current / today_low - 1) * 100 if today_low > 0 else 0
    pct_vs_open = (current / today_open - 1) * 100 if today_open > 0 else 0

    print(f"  today_open  = {today_open:.2f}")
    print(f"  today_high  = {today_high:.2f}  (mins ago: {mins_since_high})")
    print(f"  today_low   = {today_low:.2f}")
    print(f"  current     = {current:.2f}")
    print(f"  bar_count   = {bar_count} (5m bars)")
    print(f"  mins_since_open = {mins_since_open}")
    print(f"  vol_ratio   = {vol_ratio}")
    print()
    print(f"  drawdown_pct = {drawdown_pct:+.2f}%  (threshold ≤ -{threshold}%)")
    print(f"  rebound_pct  = {rebound_pct:+.2f}%  (threshold ≥ +{threshold}%)")
    print(f"  pct_vs_open  = {pct_vs_open:+.2f}%")

    # 2) 比對 threshold
    drawdown_qualifies = drawdown_pct <= -threshold
    rebound_qualifies = rebound_pct >= threshold

    print()
    print("📊 條件比對:")
    print(f"  drawdown qualifies: {'✅' if drawdown_qualifies else '❌'} "
          f"(差 threshold {drawdown_pct + threshold:+.2f}pp)")
    print(f"  rebound qualifies:  {'✅' if rebound_qualifies else '❌'} "
          f"(差 threshold {rebound_pct - threshold:+.2f}pp)")

    # 3) 比對 state (ratchet / cooldown / daily cap)
    try:
        state = watchlist_store.load_monitor_state()
        rev_state = state.get("intraday_reversal", {})
        sym_state = rev_state.get(sym, {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        if sym_state.get("date") != today_str:
            print()
            print("📦 monitor_state: 今日尚未有任何反轉記錄")
        else:
            print()
            print("📦 monitor_state (今日記錄):")
            print(f"  drawdown_alerts_today: {sym_state.get('drawdown_alerts_today', 0)}/3 (daily cap)")
            print(f"  rebound_alerts_today:  {sym_state.get('rebound_alerts_today', 0)}/3 (daily cap)")
            print(f"  last_drawdown_pct: {sym_state.get('last_drawdown_pct')}")
            print(f"  last_drawdown_at:  {sym_state.get('last_drawdown_at')}")
            print(f"  last_rebound_pct:  {sym_state.get('last_rebound_pct')}")
            print(f"  last_rebound_at:   {sym_state.get('last_rebound_at')}")
    except Exception as e:
        print(f"⚠️ 無法讀 monitor_state: {e}")

    # 4) market session 判斷
    in_session = ia._is_market_in_session(cfg.get("country"))
    print()
    print(f"🕐 _is_market_in_session({cfg.get('country')}) = {in_session}")
    if not in_session:
        print("   ⚠️ 此刻不在 session, check_intraday_reversal 會直接跳過該 sym")

    # 5) holiday check
    try:
        import holiday_check
        closed = holiday_check.is_market_closed_today(cfg.get("country", "TW"))
        print(f"🗓  holiday_check ({cfg.get('country')}) closed today: {closed}")
        if closed:
            print("   ⚠️ holiday_check 認為今日休市, reversal 整段跳過")
    except Exception as e:
        print(f"   (holiday_check 不可用: {e})")

    # 6) 結論
    print()
    if drawdown_qualifies or rebound_qualifies:
        print("✅ 條件本身有達到! 沒推可能原因:")
        print("   (a) GH Actions cron 沒跑到該時段 (check Actions tab)")
        print("   (b) push 還沒生效 (workflow checkout 的是舊 commit)")
        print("   (c) state daily_cap 已達 3, 或 ratchet 0.5pp 沒過")
        print("   (d) combined_msg formatter 出錯 (看 logs '[combined intraday]')")
    else:
        print("❌ 條件本身沒達到, 不該推也沒推 — 正常.")
        print(f"   差最近 threshold 還有 {min(abs(drawdown_pct + threshold), abs(rebound_pct - threshold)):.2f}pp")


def main() -> int:
    syms = sys.argv[1:] if len(sys.argv) > 1 else ["^TWII"]
    for sym in syms:
        diagnose_sym(sym)
    return 0


if __name__ == "__main__":
    sys.exit(main())
