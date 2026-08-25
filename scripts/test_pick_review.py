# -*- coding: utf-8 -*-
"""scripts/test_pick_review.py — 離線回歸測試 (不打網路, 不需 secrets)

用法: python scripts/test_pick_review.py   (exit 0 = 全過)

原離線測試: 用假的 data_sources / watchlist_store 驗證 signal_tracker + pick_review。

模擬真實時間軸:
  8/20(四) 8/21(五) [週末] 8/24(一) 8/25(二)
  T1 = 台北 8/24 08:00  → 晨報/盤前推台股 (現價 = 8/21 收盤)
  T2 = 台北 8/24 09:32  → 開盤選股推台股 (現價 = 8/24 盤中)
  T3 = 台北 8/24 20:32  → 美股盤前推美股 (現價 = 8/21 美股收盤)
  T4 = 台北 8/25 08:00  → 晨報驗收 (TW 8/24 已收盤 / US 8/24 已收盤; 8/25 兩邊都還沒)
"""
import sys, types, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd

# ---------- stub watchlist_store ----------
ws = types.ModuleType("watchlist_store")
ws.MONITOR_STATE_FILE = Path("/tmp/_t_state.json")
_STATE = {}
ws.load_monitor_state = lambda: json.loads(json.dumps(_STATE))
def _save(s):
    _STATE.clear(); _STATE.update(json.loads(json.dumps(s)))
ws.save_monitor_state = _save
sys.modules["watchlist_store"] = ws

# ---------- stub data_sources (時間感知) ----------
BARS = {
    "2330.TW":  [("2026-08-20", 1000.0), ("2026-08-21", 1010.0), ("2026-08-24", 1030.0), ("2026-08-25", 1025.0)],
    "2454.TW":  [("2026-08-20", 500.0),  ("2026-08-21", 505.0),  ("2026-08-24", 495.0),  ("2026-08-25", 500.0)],
    "6488.TW":  [],   # .TW 抓不到 → 應自動 fallback .TWO
    "6488.TWO": [("2026-08-20", 300.0), ("2026-08-21", 310.0), ("2026-08-24", 316.0), ("2026-08-25", 320.0)],
    "^TWII":    [("2026-08-20", 1.0), ("2026-08-21", 1.0), ("2026-08-24", 1.0), ("2026-08-25", 1.0)],
    "NVDA":     [("2026-08-20", 100.0), ("2026-08-21", 102.0), ("2026-08-24", 108.0)],
    "AMD":      [("2026-08-20", 200.0), ("2026-08-21", 198.0), ("2026-08-24", 190.0)],
    "SPY":      [("2026-08-20", 1.0), ("2026-08-21", 1.0), ("2026-08-24", 1.0)],
    "DEAD":     [],
}
ds = types.ModuleType("data_sources")
def fetch_yf_history(symbol, period="6mo", interval="1d", max_retries=3):
    rows = BARS.get(symbol, [])
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame({"Date": [r[0] for r in rows],
                          "Close": [r[1] for r in rows],
                          "High": [r[1] * 1.02 for r in rows],
                          "Low": [r[1] * 0.98 for r in rows]})
ds.fetch_yf_history = fetch_yf_history
sys.modules["data_sources"] = ds

import signal_tracker as st
import pick_review as pr

CLOCK = {"tpe": "2026-08-24", "tw_partial": "2026-08-24", "us_partial": "2026-08-24"}
st._today_str = lambda: CLOCK["tpe"]
st._partial_bar_date = lambda market: (CLOCK["us_partial"] if str(market).upper() == "US"
                                        else CLOCK["tw_partial"])
def set_clock(tpe, tw_partial, us_partial, label=""):
    CLOCK.update(tpe=tpe, tw_partial=tw_partial, us_partial=us_partial)
    st.clear_bars_cache()
    if label:
        print("\n--- %s (台北 %s) ---" % (label, tpe))

def show(t):
    print("\n" + "=" * 72); print(t); print("=" * 72)

FAILS = []
def chk(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)

show("時間軸模擬")

set_clock("2026-08-24", "2026-08-24", "2026-08-24", "T1 晨報 08:00 推台股 Top 5")
n1 = pr.record_picks("morning_tw_top5",
    [{"stock_id": "2330", "name": "台積電", "current": 1010.0},
     {"stock_id": "2454", "name": "聯發科", "current": 505.0},
     {"stock_id": "6488", "name": "環球晶", "current": 310.0}],
    market="TW", source="晨報台股 Top 5", price_is_last_close=True)
print("  記錄 %d 檔, asof=%s" % (n1, pr.last_trading_day("TW")))

set_clock("2026-08-24", "2026-08-24", "2026-08-24", "T2 開盤 09:32 推強勢族群龍頭 (盤中價)")
st.record_signal("strong_sector_leader", "2330", name="台積電", predicted_price=1025.0,
                  evaluate_after_days=1, extras={"market": "TW", "source": "開盤強勢族群龍頭"})
print("  記錄 1 檔 (不設 asof → 用推播日 8/24)")

set_clock("2026-08-24", "2026-08-24", "2026-08-24", "T3 美股盤前 20:32 推 BUY Top 5")
n3 = pr.record_picks("us_buy_picks",
    [{"symbol": "NVDA", "name": "Nvidia", "current": 102.0},
     {"symbol": "AMD", "name": "AMD", "current": 198.0},
     {"symbol": "DEAD", "name": "查無此股", "current": 50.0}],
    market="US", source="美股盤前 BUY Top 5", price_is_last_close=True)
print("  記錄 %d 檔, asof=%s" % (n3, pr.last_trading_day("US")))

set_clock("2026-08-25", "2026-08-25", None, "T4 隔天晨報 08:00 驗收")
print("  evaluate_pending →", st.evaluate_pending(), "筆")
for r in st.load_records():
    print("   %-6s %-22s pred=%-8s actual=%-8s @%-11s pct=%-7s hit=%s"
          % (r["stock_id"], r["signal_type"], r["predicted_price"], r["actual_price"],
             r["actual_date"], r["actual_pct"], r["hit"]))

show("晨報實際會長這樣")
print(pr.build_review_block(refresh=False))

show("斷言")
recs = {(r["stock_id"], r["signal_type"]): r for r in st.load_records()}
r = recs[("2330", "morning_tw_top5")]
chk(r["extras"].get("asof") == "2026-08-21", "盤前推播 asof 正確標成前一交易日 8/21 (不是推播日 8/24)")
chk(r["actual_date"] == "2026-08-24" and r["hit"] is True and abs(r["actual_pct"] - 1.98) < 0.02,
    "2330 用 8/24 收盤驗證 → +1.98% 判定有漲")
chk(recs[("2454", "morning_tw_top5")]["hit"] is False, "2454 -1.98% 判定沒漲")
r = recs[("6488", "morning_tw_top5")]
chk(r["actual_price"] == 316.0 and r["hit"] is True, "6488 自動 fallback .TWO 抓到並判定有漲")
chk(recs[("2330", "strong_sector_leader")]["hit"] is None,
    "盤中訊號 (asof=8/24, 要看 8/25 收盤) 保持 pending — 8/25 未收盤不會被誤判成沒漲")
r = recs[("NVDA", "us_buy_picks")]
chk(r["extras"].get("asof") == "2026-08-21" and r["actual_date"] == "2026-08-24" and r["hit"] is True,
    "美股 NVDA 有被驗證 (+5.88%) — 舊版美股永遠停在 pending")
chk(recs[("AMD", "us_buy_picks")]["hit"] is False, "美股 AMD -4.04% 判定沒漲")
chk(recs[("DEAD", "us_buy_picks")]["hit"] is None, "抓不到行情的標的保持 pending, 不會被當成沒漲")

show("週末不再被誤判 (舊版最嚴重的 bug)")
r = recs[("2330", "morning_tw_top5")]
chk(r["actual_date"] == "2026-08-24",
    "asof=8/21(週五) 的紀錄結算在 8/24(週一), 自動跳過週末; 舊版會拿 8/21 自己比 → pct=0 → 一律記成沒漲")

show("命中率")
d = pr.review_data(refresh=False)
print("  TW:", {k: v for k, v in (d["TW"] or {}).items() if k != "items"})
print("  US:", {k: v for k, v in (d["US"] or {}).items() if k != "items"})
chk(d["TW"]["pct"] == round(2 / 3 * 100, 1), "台股命中率 2/3 = 66.7%")
chk(d["US"]["pct"] == 50.0, "美股命中率 1/2 = 50%")
print("  ALL 30d:", st.accuracy_summary(None, 30))
print("  TW  30d:", st.accuracy_summary(None, 30, market="TW"))
print("  US  30d:", st.accuracy_summary(None, 30, market="US"))

show("放棄機制 (20 天抓不到就不再重試)")
set_clock("2026-09-20", "2026-09-20", None)
st.evaluate_pending()
r = {(x["stock_id"], x["signal_type"]): x for x in st.load_records()}[("DEAD", "us_buy_picks")]
chk(bool(r["extras"].get("_no_data")), "20 天後標記 _no_data, 之後不再重複抓行情")

print("\n" + ("=" * 72))
print("結果: %d 失敗" % len(FAILS))
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
