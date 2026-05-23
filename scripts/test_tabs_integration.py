"""
scripts/test_tabs_integration.py

Dashboard 各 tab 的整合測試 — 不啟動 streamlit, 直接呼叫每個 tab 用到的
模組 / 函式, 驗證 import 路徑 + 函式簽名 + 預期欄位都還在.

對 dashboard 部署前一個關鍵 sanity check (smoke_test 是檢查語法, 這個是檢查
function call 串起來會不會崩).

用法:
    cd /path/to/stock_dashboard
    python scripts/test_tabs_integration.py

預期: 全部 ✓. 若有 ✗ 表示某個 tab 在 dashboard 跑時會炸.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _try(label, fn):
    try:
        fn()
        print(f"  ✓ {label}")
        return True
    except Exception as e:
        print(f"  ✗ {label}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
        return False


def main():
    results = []

    # === tab_actionable: actionable_picks.compute_actionable_picks ===
    print("\n[tab 今日可行動]")
    def t_actionable_sig():
        import actionable_picks as ap
        import inspect
        sig = inspect.signature(ap.compute_actionable_picks)
        params = list(sig.parameters.keys())
        # 應該至少包含 top_n, market, respect_regime, open_data (B9 新增)
        for p in ("top_n", "market", "respect_regime", "open_data"):
            assert p in params, f"compute_actionable_picks missing param: {p}"

    def t_actionable_fmt():
        import actionable_picks as ap
        # 餵空 list 看格式化函式不會崩
        out = ap.fmt_actionable_picks_tg([])
        assert isinstance(out, str)

    def t_actionable_dynamic_levels():
        import actionable_picks as ap
        # _dynamic_levels 在無 yfinance 時應走 fallback
        lv = ap._dynamic_levels("2330", current=1050.0)
        assert lv and lv.get("stop") and lv.get("target") and lv.get("rr")

    results.append(_try("compute_actionable_picks 簽名含 open_data", t_actionable_sig))
    results.append(_try("fmt_actionable_picks_tg([]) 不崩", t_actionable_fmt))
    results.append(_try("_dynamic_levels fallback 可用", t_actionable_dynamic_levels))

    # === tab_tw: tw_screener.run_all_screens 簽名 + CONDITION_LABELS ===
    print("\n[tab 台股篩選]")
    def t_tw_labels():
        import tw_screener
        labels = tw_screener.CONDITION_LABELS
        # app.py line 1418 expects these conditions
        required = ("break_ma", "volume_burst", "short_increase", "invtrust_first_buy",
                    "invtrust_consecutive", "invtrust_5d_acc", "capital_ratio",
                    "above_ma_uptrend", "kd_golden_cross", "macd_turn_positive")
        for k in required:
            assert k in labels, f"CONDITION_LABELS missing: {k}"
        # B7 fix: label 應該已更新到 1.5%
        assert "1.5%" in labels["capital_ratio"] or "20日" in labels["capital_ratio"], \
            f"capital_ratio label 未更新: {labels['capital_ratio']}"

    def t_tw_params():
        import tw_screener
        p = tw_screener.TWParams()
        # B7 fix: 預設值已調整
        assert p.capital_ratio_pct == 1.5
        assert hasattr(p, "capital_ratio_window")

    def t_tw_run_signature():
        import tw_screener
        import inspect
        sig = inspect.signature(tw_screener.run_all_screens)
        for p in ("market", "params", "enabled", "progress_cb"):
            assert p in sig.parameters

    results.append(_try("tw_screener.CONDITION_LABELS 10 條件齊全 + 1.5% label", t_tw_labels))
    results.append(_try("tw_screener.TWParams 預設值正確", t_tw_params))
    results.append(_try("tw_screener.run_all_screens 簽名兼容", t_tw_run_signature))

    # === tab_stock: stock_analyzer.institutional_summary / margin_summary ===
    print("\n[tab 個股深度分析]")
    def t_stock_helpers():
        import stock_analyzer, pandas as pd
        # 餵空 DataFrame 應該不崩 (回空 summary)
        assert stock_analyzer.institutional_summary(pd.DataFrame()).empty
        assert isinstance(stock_analyzer.margin_summary(pd.DataFrame()), dict)
    results.append(_try("stock_analyzer institutional/margin 空 DF 不崩", t_stock_helpers))

    # === tab_us: us_screener.run_us_recommendation ===
    print("\n[tab 美股 Top 5]")
    def t_us_sig():
        import us_screener
        import inspect
        sig = inspect.signature(us_screener.run_us_recommendation)
        assert "top_n" in sig.parameters
        # B13 新增 dedup_correlated
        assert "dedup_correlated" in sig.parameters

    def t_us_etf_blacklist():
        import us_screener
        assert "SPY" in us_screener._US_ETF_BLACKLIST
        assert "ARKK" in us_screener._US_ETF_BLACKLIST
        assert len(us_screener._US_ETF_BLACKLIST) >= 20

    def t_us_dedup():
        import us_screener
        rows = [{"symbol": s, "score": 10 - i} for i, s in enumerate(
            ["NVDA", "TSM", "ASML", "AVGO", "MSFT", "GOOGL", "META", "AAPL"])]
        kept = us_screener._dedup_correlated(rows, min_kept=5)
        assert len(kept) >= 5, f"dedup fallback 失效: {len(kept)}"

    results.append(_try("us_screener.run_us_recommendation 簽名含 dedup_correlated", t_us_sig))
    results.append(_try("us_screener ETF blacklist 完整", t_us_etf_blacklist))
    results.append(_try("us_screener dedup fallback 補足 min_kept", t_us_dedup))

    # === chip_analyzer 的 fetch_chip_data 回傳 dict 結構 (給 closing/holdings/chip_filter 用) ===
    print("\n[chip_analyzer 回傳 dict 結構驗證]")
    def t_chip_helpers():
        import chip_analyzer as ca
        # 餵空 dict, 確認 helper 不崩
        assert ca.calc_chip_consensus({}) is not None
        assert isinstance(ca.calc_chip_health_score({}, {}, {}), int)
        assert ca.calc_short_margin_ratio({"融資餘額": 1000, "融券餘額": 250}) == 25.0

    def t_chip_dual_cache():
        import chip_analyzer as ca
        # M2: 兩個 impl 路徑都存在
        assert hasattr(ca, "_fetch_chip_data_impl_raw")
        assert hasattr(ca, "_fetch_chip_data_impl_st")
        assert hasattr(ca, "_is_main_streamlit_thread")

    def t_chip_deepcopy():
        # H1: local cache deepcopy 確認
        import chip_analyzer as ca
        ca._local_cache_set(("__integration_test__", 1), {"x": {"v": 100}})
        d1 = ca._local_cache_get(("__integration_test__", 1))
        d1["x"]["v"] = 999
        d2 = ca._local_cache_get(("__integration_test__", 1))
        assert d2["x"]["v"] == 100, "deepcopy 失效"

    results.append(_try("chip_analyzer helper 函式可呼叫", t_chip_helpers))
    results.append(_try("chip_analyzer dual cache 結構正確", t_chip_dual_cache))
    results.append(_try("chip_analyzer local cache deepcopy", t_chip_deepcopy))

    # === 下游 consumer 對 None margin 的 robustness ===
    print("\n[None margin robustness — 給 closing/holdings 用]")
    def t_closing_none_margin():
        # closing_analyzer 第 122 行抓 margin_30d, 我已用 or 0 處理.
        # 模擬 chip dict 含 None margin
        import closing_analyzer as ca
        # 用 grep 確認 line 122 有 `or 0`
        src = Path(ca.__file__).read_text(encoding="utf-8")
        assert "融資30日變化%') or 0" in src, "closing_analyzer 第 122 行未加 or 0 fallback"

    def t_holdings_none_margin():
        import holdings_analyzer as ha
        src = Path(ha.__file__).read_text(encoding="utf-8")
        assert "(margin.get('融資30日變化%') or 0)" in src, "holdings_analyzer 未加 or 0 fallback"

    def t_chip_filter_none_margin():
        import chip_filter as cf
        src = Path(cf.__file__).read_text(encoding="utf-8")
        # L3 fix: 用 is not None 區分
        assert "margin_30d is not None" in src, "chip_filter 未用 is not None 判斷"

    results.append(_try("closing_analyzer 對 None margin 安全", t_closing_none_margin))
    results.append(_try("holdings_analyzer 對 None margin 安全", t_holdings_none_margin))
    results.append(_try("chip_filter 用 is not None 區分未知/0", t_chip_filter_none_margin))

    # === upside_screener / us_upside_screener 整合性 ===
    print("\n[upside / us_upside / theme_analyzer]")
    def t_upside_cache():
        import upside_screener as us
        import inspect
        sig = inspect.signature(us.run_upside_screen)
        assert "use_cache" in sig.parameters
        assert hasattr(us, "_cached_upside_screen")
        assert hasattr(us, "_run_upside_screen_impl")

    def t_us_upside_themes():
        import us_upside_screener as ups
        import inspect
        sig = inspect.signature(ups.run_us_upside_screen)
        assert "with_themes" in sig.parameters
        # H2: 4 類齊全
        assert set(ups.CATEGORY_LABEL_US.keys()) == {"breakout", "acceleration", "squeeze_setup", "narrative_leader"}

    def t_theme_analyzer():
        import theme_analyzer as ta
        # 12 大 narrative
        assert len(ta.THEME_KEYWORDS) >= 10
        # multiplier 邏輯
        assert ta.theme_multiplier({"total_score": 80}) == 1.5
        assert ta.theme_multiplier(None) == 1.0
        # 新版 yfinance news 格式偵測
        result = ta._detect_narratives_from_titles("NVDA AI chip announcement")
        assert "AI / LLM" in result

    results.append(_try("upside_screener cache wrapper 正確", t_upside_cache))
    results.append(_try("us_upside_screener with_themes + 4 類", t_us_upside_themes))
    results.append(_try("theme_analyzer 基本功能", t_theme_analyzer))

    # === 統計 ===
    n_ok = sum(results)
    n_total = len(results)
    print(f"\n{'='*50}")
    print(f"整合測試: {n_ok} / {n_total} 通過")
    if n_ok == n_total:
        print("✅ 所有 tab 的整合點都安全, 可部署.")
    else:
        print(f"❌ 有 {n_total - n_ok} 項失敗, 部署前必須修.")
        sys.exit(1)


if __name__ == "__main__":
    main()
