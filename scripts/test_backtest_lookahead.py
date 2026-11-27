# -*- coding: utf-8 -*-
"""scripts/test_backtest_lookahead.py — 回測 look-ahead 離線回歸測試 (不打網路, 不需 secrets)

用法: python scripts/test_backtest_lookahead.py   (exit 0 = 全過)

audit #1: 回測原本用「訊號日 T 的收盤」進場, 但訊號用到 T 日收盤後才公布的籌碼 (法人約 15:00-16:30)
→ 勝率被高估。修正後一律「隔日開盤進場」(T+1 open, 開盤缺值才退回 T+1 收盤, 沒有 T+1 則跳過)。

驗證:
  1. 進場價 = T+1 開盤 (backtest.py 的 _forward_returns 與 backtest_upside.backtest 兩條路徑)
  2. 訊號出在最後一根 K 棒 (沒有 T+1) → 跳過, 不計入
  3. T+1 跳空大漲 → 量到的報酬比舊版「T 收盤進場」低
  4. 開盤價缺失 / 0 → 退回 T+1 收盤
  5. 特徵計算只看 T (含) 以前: 改動 T 之後的 K 棒, 特徵不變
  6. 持有天數從 T+1 起算; 大盤 benchmark 也用 T+1 開盤
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from radar.analysis import backtest_upside as bu
from radar.analysis import backtest as bk

FAILS = []


def chk(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


def show(title):
    print("\n" + "=" * 72 + "\n" + title)


def make_df(n=80, start="2026-01-01", gap_at=None, gap_open=None, seed=0):
    """合成日線: 緩漲 + 小雜訊; gap_at 指定那根 K 棒的開盤改成 gap_open (跳空)。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n)
    close = 100 + np.cumsum(rng.normal(0.1, 0.5, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.1, n)
    df = pd.DataFrame({
        "date": dates,
        "open": open_,
        "close": close,
        "volume": rng.integers(1000, 2000, n).astype(float),
    })
    if gap_at is not None:
        df.loc[gap_at, "open"] = gap_open
    df["high"] = np.maximum(df["open"], df["close"]) + 0.5
    df["low"] = np.minimum(df["open"], df["close"]) - 0.5
    return df[["date", "open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
show("1. _next_open_entry: T+1 開盤 / 開盤缺值退回收盤 / 沒有 T+1")
df = make_df(10)
after = df.iloc[4:].reset_index(drop=True)
e = bu._next_open_entry(after)
chk(e is not None and e[0] == df.loc[4, "date"] and abs(e[1] - df.loc[4, "open"]) < 1e-9 and e[2] == "open",
    "進場 = 下一根 K 棒的開盤")
after0 = after.copy()
after0.loc[0, "open"] = 0.0
e0 = bu._next_open_entry(after0)
chk(e0 is not None and abs(e0[1] - after0.loc[0, "close"]) < 1e-9 and e0[2] == "close",
    "開盤 = 0 → 退回 T+1 收盤")
after_nan = after.copy()
after_nan.loc[0, "open"] = np.nan
chk(bu._next_open_entry(after_nan)[2] == "close", "開盤 = NaN → 退回 T+1 收盤")
chk(bu._next_open_entry(df.iloc[0:0]) is None, "沒有 T+1 K 棒 → None")

# ---------------------------------------------------------------------------
show("2. backtest.py _forward_returns: 進場 = T+1 開盤, 持有天數 T+1 起算")
T = 30
df = make_df(60)
df.loc[T + 1, "open"] = float(df.loc[T, "close"]) + 0.3  # 小跳空
df["high"] = np.maximum(df["high"], df["open"])
as_of = df.loc[T, "date"]
fwd = bk._forward_returns(df, as_of)
entry = float(df.loc[T + 1, "open"])
chk(abs(fwd["entry_price"] - round(entry, 2)) < 1e-9, f"entry_price = T+1 開盤 ({entry:.2f})")
chk(fwd["entry_date"] == df.loc[T + 1, "date"].date(), "entry_date = T+1")
chk(fwd["entry_basis"] == "open", "entry_basis = open")
exp_r5 = (float(df.loc[T + 5, "close"]) / entry - 1) * 100
chk(abs(fwd["r5d"] - exp_r5) < 1e-9, "r5d = T+5 收盤 / T+1 開盤 - 1 (持有天數從 T+1 起算)")
exp_hi = (float(df.loc[T + 1:T + 20, "high"].max()) / entry - 1) * 100
chk(abs(fwd["max_high_20d"] - exp_hi) < 1e-9, "max_high_20d 以進場價為基準, 範圍 T+1..T+20")

show("3. backtest.py: 訊號在最後一根 K 棒 → 跳過")
last = df["date"].iloc[-1]
chk(bk._forward_returns(df, last) == {}, "as_of = 最後一根 → 回傳 {} (呼叫端 continue)")

show("4. 跳空大漲: 隔日開盤進場的報酬 < 舊版 T 收盤進場")
df_gap = make_df(60)
t_close = float(df_gap.loc[T, "close"])
df_gap.loc[T + 1, "open"] = t_close * 1.07   # 隔天跳空 +7%
df_gap["high"] = np.maximum(df_gap["high"], df_gap["open"])
fwd_gap = bk._forward_returns(df_gap, df_gap.loc[T, "date"])
old_r5 = (float(df_gap.loc[T + 5, "close"]) / t_close - 1) * 100   # 舊版算法
chk(fwd_gap["r5d"] < old_r5, f"新 r5d {fwd_gap['r5d']:.2f}% < 舊 r5d {old_r5:.2f}%")
chk(abs((old_r5 - fwd_gap["r5d"]) - (old_r5 + 100) * (1 - 1 / 1.07)) < 1e-6,
    "差距正好等於跳空幅度造成的基準差")

# ---------------------------------------------------------------------------
show("5. backtest_upside.backtest 端到端 (強制每天都命中 early_stage)")
_orig = (bu._check_early, bu._check_momentum, bu._check_reversal)
bu._check_early = lambda f, chip_required: 60
bu._check_momentum = lambda f, chip_required: None
bu._check_reversal = lambda f, chip_required: None
try:
    N = 60
    dfu = make_df(N, seed=1)
    G = 45  # 在 G 跳空
    dfu.loc[G, "open"] = float(dfu.loc[G - 1, "close"]) * 1.06
    dfu["high"] = np.maximum(dfu["high"], dfu["open"])
    bench = make_df(N, seed=2)
    # 故意打亂輸入順序 — backtest 應自行排序, 不能拿錯 T / T+1
    shuffled = dfu.sample(frac=1.0, random_state=0).reset_index(drop=True)
    res = bu.backtest(
        prices={"9999": shuffled}, names={"9999": "測試"},
        start_date=str(dfu.loc[35, "date"].date()), end_date=str(dfu["date"].iloc[-1].date()),
        hold_days=[1, 5], benchmark_df=bench, rescan_every=1,
    )
    picks = res["picks_df"]
    by_asof = {pd.Timestamp(r["as_of"]): r for _, r in picks.iterrows()}
    chk(dfu["date"].iloc[-1] not in by_asof, "最後一根 K 棒的訊號被跳過 (沒有 T+1 可進場)")
    chk(len(picks) == (N - 1) - 35, f"其餘每天都有 pick ({len(picks)} 筆)")
    ok_all = True
    for i in range(35, N - 1):
        r = by_asof.get(dfu.loc[i, "date"])
        if r is None or abs(r["entry_price"] - round(float(dfu.loc[i + 1, "open"]), 2)) > 1e-9 \
                or pd.Timestamp(r["entry_date"]) != dfu.loc[i + 1, "date"] \
                or abs(r["signal_close"] - round(float(dfu.loc[i, "close"]), 2)) > 1e-9:
            ok_all = False
            break
    chk(ok_all, "每筆 entry_price = T+1 開盤, entry_date = T+1, signal_close = T 收盤")
    rg = by_asof[dfu.loc[G - 1, "date"]]
    exp = (float(dfu.loc[G - 1 + 5, "close"]) / float(dfu.loc[G, "open"]) - 1) * 100
    old = (float(dfu.loc[G - 1 + 5, "close"]) / float(dfu.loc[G - 1, "close"]) - 1) * 100
    # ret_{hd}d 欄位由 backtest_upside 以 f-string 產生, 這裡也用同樣方式讀 (避免 orphan_keys 誤報)
    r5, r1 = rg[f"ret_{5}d"], rg[f"ret_{1}d"]
    chk(abs(r5 - exp) < 1e-9, "ret_5d = T+5 收盤 / T+1 開盤")
    chk(r5 < old, f"跳空日前一天的訊號: 新 ret_5d {r5:.2f}% < 舊 {old:.2f}%")
    exp1 = (float(dfu.loc[G, "close"]) / float(dfu.loc[G, "open"]) - 1) * 100
    chk(abs(r1 - exp1) < 1e-9, "ret_1d = T+1 收盤 / T+1 開盤 (第 1 天 = T+1)")
    chk(res["meta"].get("entry_rule") == "隔日開盤進場", "meta.entry_rule = 隔日開盤進場")
    # benchmark: 也用 T+1 開盤
    bd = bench.set_index("date")
    scans = [d for d in dfu["date"] if dfu.loc[35, "date"] <= d <= dfu["date"].iloc[-1]]
    b5 = []
    for d in scans:
        after = bench[bench["date"] > d].reset_index(drop=True)
        if len(after) >= 5:
            b5.append((float(after.loc[4, "close"]) / float(after.loc[0, "open"]) - 1) * 100)
    chk(res["benchmark_returns"][5] == round(float(np.mean(b5)), 2), "benchmark 5d 報酬也以 T+1 開盤為基準")
finally:
    bu._check_early, bu._check_momentum, bu._check_reversal = _orig

# ---------------------------------------------------------------------------
show("6. 特徵計算不偷看 T 之後的 K 棒")
dff = make_df(80, seed=3)
T2 = 60
as_of2 = dff.loc[T2, "date"]
f1 = bu._compute_features_at("x", "x", dff, as_of2)
dff2 = dff.copy()
dff2.loc[T2 + 1:, ["open", "high", "low", "close"]] *= 3.0   # 未來暴漲 3 倍
dff2.loc[T2 + 1:, "volume"] *= 50
f2 = bu._compute_features_at("x", "x", dff2, as_of2)
keys = ["current", "ma20", "ma60", "today_pct", "five_pct", "twenty_pct", "pct_from_52w_high",
        "pct_from_52w_low", "rsi", "vol_ratio_today", "bb_squeeze", "ma_bullish_alignment",
        "rsi_bottom_divergence"]
same = all((f1[k] == f2[k]) or (isinstance(f1[k], float) and np.isnan(f1[k]) and np.isnan(f2[k])) for k in keys)
chk(same and f1["levels"] == f2["levels"], "改動 T 之後的資料, T 日特徵 / ATR 目標停損完全不變")

print("\n" + ("=" * 72))
print("結果: %d 失敗" % len(FAILS))
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
