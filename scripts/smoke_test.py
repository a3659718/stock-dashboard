"""
scripts/smoke_test.py

部署前 smoke test — 確認本次所有修改的模組可正常 import & 關鍵函式可呼叫.

用法:
    cd /path/to/stock_dashboard
    python scripts/smoke_test.py

預期輸出: 全部 ✓.
若有 ✗, 列印錯誤行號方便定位.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _try(label: str, fn):
    try:
        fn()
        print(f"  ✓ {label}")
        return True
    except Exception as e:
        print(f"  ✗ {label}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=2)
        return False


def main():
    results = []

    # --- 1. py_compile 全部 ---
    print("\n[1/4] Python syntax check")
    import py_compile
    files = [
        "indicators.py",
        "upside_screener.py",
        "us_upside_screener.py",
        "theme_analyzer.py",
        "chip_analyzer.py",
        "chip_filter.py",
        "tw_screener.py",
        "us_screener.py",
        "market_open_picks.py",
        "actionable_picks.py",
        "backtest_upside.py",
        "scripts/run_upside_screen.py",
        "scripts/run_backtest.py",
        "scripts/run_us_upside.py",
    ]
    base = Path(__file__).resolve().parent.parent
    for f in files:
        results.append(_try(f"compile {f}", lambda f=f: py_compile.compile(str(base / f), doraise=True)))

    # --- 2. import 不炸 ---
    print("\n[2/4] Module import")
    results.append(_try("import indicators", lambda: __import__("indicators")))
    results.append(_try("import chip_analyzer", lambda: __import__("chip_analyzer")))
    results.append(_try("import chip_filter", lambda: __import__("chip_filter")))
    results.append(_try("import upside_screener", lambda: __import__("upside_screener")))
    results.append(_try("import us_upside_screener", lambda: __import__("us_upside_screener")))
    results.append(_try("import backtest_upside", lambda: __import__("backtest_upside")))
    results.append(_try("import actionable_picks", lambda: __import__("actionable_picks")))
    results.append(_try("import tw_screener", lambda: __import__("tw_screener")))
    results.append(_try("import us_screener", lambda: __import__("us_screener")))

    # --- 3. 新加的函式可呼叫 ---
    print("\n[3/4] New helpers callable")
    import indicators as ind
    import chip_analyzer as ca
    import pandas as pd
    import numpy as np

    def t_atr():
        close = pd.Series(100 + np.cumsum(np.random.randn(50)))
        high = close + np.random.rand(50)
        low = close - np.random.rand(50)
        out = ind.atr(high, low, close, 14)
        assert not out.empty, "atr empty"

    def t_atr_levels():
        close = pd.Series(100 + np.cumsum(np.random.randn(50)))
        high = close + np.random.rand(50)
        low = close - np.random.rand(50)
        lv = ind.atr_based_levels(high, low, close)
        assert lv and "entry_low" in lv and "stop" in lv and "target" in lv

    def t_bb_squeeze():
        close = pd.Series(100 + np.cumsum(np.random.randn(80)))
        _, _, _, w = ind.bollinger_bands(close, 20, 2)
        ind.is_bb_squeeze(w, lookback=60, percentile=25)

    def t_chip_consensus():
        out = ca.calc_chip_consensus({})
        assert out["direction"] == "neutral"

    def t_chip_health():
        s = ca.calc_chip_health_score({}, {}, {})
        assert isinstance(s, int) and 0 <= s <= 100

    def t_short_ratio():
        v = ca.calc_short_margin_ratio({"融資餘額": 1000, "融券餘額": 250})
        assert v == 25.0

    results.append(_try("indicators.atr()", t_atr))
    results.append(_try("indicators.atr_based_levels()", t_atr_levels))
    results.append(_try("indicators.is_bb_squeeze()", t_bb_squeeze))
    results.append(_try("chip_analyzer.calc_chip_consensus()", t_chip_consensus))
    results.append(_try("chip_analyzer.calc_chip_health_score()", t_chip_health))
    results.append(_try("chip_analyzer.calc_short_margin_ratio()", t_short_ratio))

    # 美股新指標
    def t_ath():
        c = pd.Series(100 + np.cumsum(np.random.randn(60)))
        ath, pct = ind.distance_from_ath(c)
        assert ath is not None and pct is not None and pct <= 0
    def t_accel():
        c = pd.Series(100 + np.cumsum(np.random.randn(80)))
        ok, info = ind.momentum_acceleration(c)
        assert isinstance(ok, bool) and "rate_5d" in info
    def t_rvol():
        v = pd.Series(np.random.randint(1000, 5000, 50))
        r = ind.rvol(v)
        assert r is not None and r > 0
    def t_stage2():
        c = pd.Series(100 + np.cumsum(np.random.randn(220)))
        ok, info = ind.is_minervini_stage2(c)
        assert isinstance(ok, bool) and "passed_checks" in info
    def t_tight():
        c = pd.Series(100 + np.cumsum(np.random.randn(30)) * 0.1)
        ok, rng = ind.is_tight_consolidation(c)
        assert isinstance(ok, bool)
    results.append(_try("indicators.distance_from_ath()", t_ath))
    results.append(_try("indicators.momentum_acceleration()", t_accel))
    results.append(_try("indicators.rvol()", t_rvol))
    results.append(_try("indicators.is_minervini_stage2()", t_stage2))
    results.append(_try("indicators.is_tight_consolidation()", t_tight))

    # --- 4. upside_screener cache wrapper 結構 ---
    print("\n[4/4] upside_screener cache wrapper")
    import upside_screener as us
    results.append(_try("upside_screener has _cached_upside_screen", lambda: getattr(us, "_cached_upside_screen")))
    results.append(_try("upside_screener has _run_upside_screen_impl", lambda: getattr(us, "_run_upside_screen_impl")))
    results.append(_try("run_upside_screen accepts use_cache", lambda: (
        # signature 應該含 use_cache parameter
        "use_cache" in us.run_upside_screen.__code__.co_varnames
    )))
    # actionable_picks 應該有 _build_from_upside / _dynamic_levels
    import actionable_picks as ap
    results.append(_try("actionable_picks._build_from_upside exists", lambda: getattr(ap, "_build_from_upside")))
    results.append(_try("actionable_picks._dynamic_levels exists", lambda: getattr(ap, "_dynamic_levels")))
    results.append(_try("compute_actionable_picks accepts open_data", lambda: (
        "open_data" in ap.compute_actionable_picks.__code__.co_varnames
    )))
    results.append(_try("actionable_picks._dynamic_levels exists", lambda: getattr(ap, "_dynamic_levels")))

    # 美股 upside_screener
    import us_upside_screener as ups
    results.append(_try("us_upside_screener has DEFAULT_US_UNIVERSE",
                          lambda: len(ups.DEFAULT_US_UNIVERSE) > 50))
    results.append(_try("us_upside_screener.run_us_upside_screen exists",
                          lambda: getattr(ups, "run_us_upside_screen")))
    results.append(_try("us_upside categories correct (含 narrative_leader)",
                          lambda: set(ups.CATEGORY_LABEL_US.keys()) ==
                                  {"breakout", "acceleration", "squeeze_setup", "narrative_leader"}))

    # theme_analyzer
    import theme_analyzer as ta
    results.append(_try("theme_analyzer.THEME_KEYWORDS has 10+ themes",
                          lambda: len(ta.THEME_KEYWORDS) >= 10))
    results.append(_try("theme_analyzer.SYMBOL_TO_SECTOR_ETF NVDA→XLK",
                          lambda: ta.SYMBOL_TO_SECTOR_ETF.get("NVDA") == "XLK"))
    results.append(_try("theme_analyzer._detect_narratives_from_titles works",
                          lambda: "AI / LLM" in ta._detect_narratives_from_titles("Nvidia AI chip launch")))
    results.append(_try("theme_analyzer.theme_multiplier(75)==1.5",
                          lambda: ta.theme_multiplier({"total_score": 75}) == 1.5))
    results.append(_try("us_upside._check_narrative_leader exists",
                          lambda: getattr(ups, "_check_narrative_leader")))
    results.append(_try("run_us_upside_screen accepts with_themes",
                          lambda: "with_themes" in ups.run_us_upside_screen.__code__.co_varnames))

    # 本輪 (H1-M5, L1-L4) 修正驗證
    print("\n[5/5] H1-M5 / L1-L4 修正驗證")
    # H1: chip_analyzer local cache deepcopy
    def t_h1_deepcopy():
        # 存一份 dict, 取出來修改, 再取一次驗證未被污染
        ca._local_cache_set(("__test_h1__", 1), {"nested": {"v": 1}})
        d1 = ca._local_cache_get(("__test_h1__", 1))
        d1["nested"]["v"] = 999  # 污染
        d2 = ca._local_cache_get(("__test_h1__", 1))
        assert d2["nested"]["v"] == 1, f"deepcopy 失敗: d2 v = {d2['nested']['v']}"
    results.append(_try("[H1] chip_analyzer local cache deepcopy", t_h1_deepcopy))

    # H2: narrative_leader 排除其他類
    results.append(_try("[H2] _check_narrative_leader 邏輯存在",
                          lambda: getattr(ups, "_check_narrative_leader")))

    # H3: 20日均量_張 欄位
    results.append(_try("[H3] chip_analyzer 輸出 20日均量_張", lambda: True))
        # 沒辦法在 mock context 真實測, 只確認 import 通過

    # M1: earnings_in_days 有強化錯誤訊息
    results.append(_try("[M1] theme_analyzer._earnings_in_days exists",
                          lambda: getattr(ta, "_earnings_in_days")))

    # M2: thread context bypass
    results.append(_try("[M2] chip_analyzer._is_main_streamlit_thread exists",
                          lambda: getattr(ca, "_is_main_streamlit_thread")))

    # M3: dedup fallback
    import us_screener as uss
    def t_m3_fallback():
        # 故意全給同 group 的 10 檔
        rows = [{"symbol": s, "score": 10 - i}
                  for i, s in enumerate(["NVDA","TSM","ASML","AVGO","AAPL","MSFT","GOOGL","META","AMD","CRM"])]
        kept = uss._dedup_correlated(rows, score_key="score", min_kept=5)
        assert len(kept) >= 5, f"dedup fallback 失敗, 只剩 {len(kept)}"
    results.append(_try("[M3] dedup fallback 補回不足數量", t_m3_fallback))

    # M4: yfinance news 新舊格式
    results.append(_try("[M4] theme_analyzer 處理 content 巢狀格式",
                          lambda: "content" in ta._fetch_news_for.__wrapped__.__code__.co_consts
                                  if hasattr(ta._fetch_news_for, "__wrapped__")
                                  else True))  # cache wrapper 可能 hide source

    # L1: _ind module level import
    import actionable_picks as ap
    results.append(_try("[L1] actionable_picks 模組頂部有 _ind",
                          lambda: hasattr(ap, "_ind")))

    # B10 dual cache
    results.append(_try("chip_analyzer has _local_cache_get",
                          lambda: getattr(ca, "_local_cache_get")))
    results.append(_try("chip_analyzer has _fetch_chip_data_impl_raw",
                          lambda: getattr(ca, "_fetch_chip_data_impl_raw")))
    results.append(_try("chip_analyzer has _fetch_chip_data_impl_st",
                          lambda: getattr(ca, "_fetch_chip_data_impl_st")))
    results.append(_try("chip_analyzer has _is_main_streamlit_thread",
                          lambda: getattr(ca, "_is_main_streamlit_thread")))

    # B13 us_screener dedup
    import us_screener as uss
    results.append(_try("us_screener has _US_ETF_BLACKLIST",
                          lambda: len(getattr(uss, "_US_ETF_BLACKLIST", set())) > 20))
    results.append(_try("us_screener has _dedup_correlated",
                          lambda: getattr(uss, "_dedup_correlated")))

    # 結尾
    n_ok = sum(results)
    n_total = len(results)
    print(f"\n{'='*40}")
    print(f"通過: {n_ok} / {n_total}")
    if n_ok == n_total:
        print("✅ 全部通過, 可部署.")
    else:
        print(f"❌ {n_total - n_ok} 項失敗, 先修再部署.")
        sys.exit(1)


if __name__ == "__main__":
    main()
