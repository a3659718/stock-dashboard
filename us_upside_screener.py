"""
us_upside_screener.py
Find US stocks with explosive potential. 5 categories:

  1. breakout         — 52w high break / Minervini Stage 2 + tight base
  2. acceleration     — 5d > 10d > 20d > 60d rate increasing + RVOL >= 1.5
  3. squeeze_setup    — tight consolidation + volume dryup + BB squeeze
  4. revival_setup    — distance from ATH < -40% but reviving (RKLB-like)
  5. narrative_leader — strong theme score + tech confirmation (補集)

Uses only yfinance for data. Integrates theme_analyzer for narrative scoring.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import data_sources as ds
import indicators as ind
import theme_analyzer as theme
# 長線目標價估算 (gemini 為選用 — 沒設 key 自動 skip)
try:
    import gemini_target_estimator as _gte
    _GTE_OK = True
except Exception:
    _GTE_OK = False

try:
    import streamlit as st  # type: ignore
except Exception:
    class _NoOp:
        def cache_data(self, *args, **kwargs):
            def deco(f):
                return f
            if len(args) == 1 and callable(args[0]):
                return args[0]
            return deco
    st = _NoOp()  # type: ignore


# --- Universe (~150 stocks: mega-cap + mid-cap + IPO) ---
DEFAULT_US_UNIVERSE = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "ORCL",
    "CRM", "ADBE", "AMD", "QCOM", "TXN", "INTC", "MU", "ASML",
    "PLTR", "SNOW", "CRWD", "PANW", "NET", "DDOG", "MDB", "SMCI", "ARM", "MRVL",
    "ZS", "OKTA", "S", "FTNT", "ANET", "ALAB",
    "ON", "AMAT", "LRCX", "KLAC", "MCHP", "SWKS", "QRVO", "AMBA", "RMBS",
    "BRK-B", "JPM", "BAC", "V", "MA", "WMT", "COST", "PG", "JNJ", "UNH", "HD",
    "NFLX", "DIS", "MCD", "SBUX", "NKE", "ABNB", "UBER", "SHOP",
    "RDDT", "CRWV", "ASTS", "RBLX", "IONQ", "RGTI", "QBTS", "SOUN", "BBAI",
    "AI", "GRAB", "BROS", "RKLB",
        "OKLO", "SMR", "CEG", "VST", "NEE", "EOG", "CVX", "XOM",
    "RIVN", "LCID", "F", "GM",
    "CART", "KLC", "BIRK", "DUOL", "TOST", "DKNG",
    "LLY", "NVO", "REGN", "VRTX",
    "HOOD", "SOFI", "AFRM", "PYPL", "SQ",
    "GE", "BA", "CAT", "DE", "RTX", "LMT", "NOC",
    "TEM", "OUST", "CPRT", "MELI", "PINS", "U", "TWLO",
]

CATEGORY_LABEL_US = {
    "breakout": "52w 突破",
    "acceleration": "動能加速",
    "squeeze_setup": "壓縮待噴",
    "revival_setup": "谷底重生",
    "narrative_leader": "題材領跑",
}


def _fetch_yf_one(symbol: str, period: str = "1y") -> Optional[pd.DataFrame]:
    try:
        df = ds.fetch_yf_history(symbol, period=period, interval="1d")
        if df is None or df.empty or len(df) < 60:
            return None
        return df
    except Exception:
        return None


def _compute_us_features(symbol: str, df: pd.DataFrame,
                          spy_df: Optional[pd.DataFrame] = None) -> Optional[Dict]:
    if df is None or df.empty or len(df) < 60:
        return None
    try:
        c = df["Close"].astype(float).reset_index(drop=True)
        h = df["High"].astype(float).reset_index(drop=True)
        l = df["Low"].astype(float).reset_index(drop=True)
        v = df["Volume"].astype(float).reset_index(drop=True)
        cur = float(c.iloc[-1])
        if cur <= 0:
            return None
        prev = float(c.iloc[-2])
        today_pct = (cur / prev - 1) * 100 if prev > 0 else 0
        five_pct = (cur / float(c.iloc[-6]) - 1) * 100 if len(c) >= 6 else None
        twenty_pct = (cur / float(c.iloc[-21]) - 1) * 100 if len(c) >= 21 else None
        sixty_pct = (cur / float(c.iloc[-61]) - 1) * 100 if len(c) >= 61 else None
        hi52, lo52, pct_hi52, pct_lo52 = ind.distance_from_52w(c, window=252)
        ath, pct_from_ath = ind.distance_from_ath(c)
        is_52w_brk, brk_info = ind.is_52w_high_breakout(c, window=252, breakout_tolerance=1.0)
        is_stage2, stage2_info = ind.is_minervini_stage2(c)
        is_accel, accel_info = ind.momentum_acceleration(c)
        is_tight, tight_range = ind.is_tight_consolidation(c, lookback=20, max_range_pct=8.0)
        is_very_tight, _ = ind.is_tight_consolidation(c, lookback=10, max_range_pct=5.0)
        base_depth = ind.base_depth_pct(c, base_window=30)
        rvol_today = ind.rvol(v, lookback=30)
        vol_dry_5d, _ = ind.volume_dryup(v, recent=5, base=30, ratio_threshold=0.8)
        is_vpt_up = ind.vpt_uptrend(c, v, lookback=20)
        rsi_s = ind.rsi(c, 14)
        rsi_now = float(rsi_s.iloc[-1]) if not rsi_s.empty else None
        _, _, _, bb_w = ind.bollinger_bands(c, 20, 2.0)
        bb_squeeze = ind.is_bb_squeeze(bb_w, lookback=60, percentile=20)
        rs_vs_spy = None
        if spy_df is not None and not spy_df.empty and len(spy_df) >= 22 and len(c) >= 22:
            spy_c = spy_df["Close"].astype(float)
            try:
                spy_20 = (float(spy_c.iloc[-1]) / float(spy_c.iloc[-21]) - 1) * 100
                stock_20 = (cur / float(c.iloc[-21]) - 1) * 100
                rs_vs_spy = round(stock_20 - spy_20, 2)
            except (IndexError, ZeroDivisionError):
                pass
        levels = ind.atr_based_levels(h, l, c, stop_atr_mult=1.5, target_atr_mult=3.0) or {}
        # 新增中長線 target: fib extension + measured move
        fib_targets = ind.fibonacci_extension_targets(c, lookback=252, pivot_window=10) or {}
        mm_target = ind.measured_move_target(c, base_lookback=60, min_base_days=15) or {}
        ma60 = float(c.rolling(60).mean().iloc[-1]) if len(c) >= 60 else None
        ma200 = float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None
        ma_bull = ind.ma_alignment_bullish(c)
        return {
            "symbol": symbol, "current": round(cur, 2),
            "today_pct": round(today_pct, 2),
            "five_pct": round(five_pct, 2) if five_pct is not None else None,
            "twenty_pct": round(twenty_pct, 2) if twenty_pct is not None else None,
            "sixty_pct": round(sixty_pct, 2) if sixty_pct is not None else None,
            "rvol": rvol_today,
            "rsi": round(rsi_now, 1) if rsi_now is not None else None,
            "rs_vs_spy": rs_vs_spy,
            "hi52": round(hi52, 2) if hi52 else None,
            "lo52": round(lo52, 2) if lo52 else None,
            "pct_from_52w_high": round(pct_hi52, 2) if pct_hi52 is not None else None,
            "pct_from_52w_low": round(pct_lo52, 2) if pct_lo52 is not None else None,
            "ath": round(ath, 2) if ath else None,
            "pct_from_ath": round(pct_from_ath, 2) if pct_from_ath is not None else None,
            "is_52w_breakout": is_52w_brk,
            "is_fresh_breakout": brk_info.get("is_fresh_breakout", False),
            "is_stage2": is_stage2,
            "stage2_passed": stage2_info.get("passed_checks", 0),
            "is_accel": is_accel,
            "accel_rates": accel_info,
            "is_tight": is_tight, "tight_range": tight_range,
            "is_very_tight": is_very_tight,
            "base_depth": base_depth,
            "vol_dry_5d": vol_dry_5d,
            "is_vpt_up": is_vpt_up,
            "bb_squeeze": bb_squeeze,
            "ma60": ma60, "ma200": ma200,
            "ma_bullish_alignment": ma_bull,
            "levels": levels,
            "fib_targets": fib_targets,
            "measured_move": mm_target,
        }
    except Exception as e:
        print("[us_upside] features " + symbol + " failed: " + str(e), flush=True)
        return None


def _check_breakout(f: Dict, theme_data: Optional[Dict] = None) -> Optional[Dict]:
    if not f.get("is_52w_breakout") and not f.get("is_stage2"):
        return None
    if f.get("pct_from_52w_high") is None or f["pct_from_52w_high"] < -10:
        return None
    if (f.get("rvol") or 0) < 1.3:
        return None
    score = 50
    reasons = []; warnings = []
    if f.get("is_fresh_breakout"):
        reasons.append("剛突破 52 週新高"); score += 25
    elif f.get("is_52w_breakout"):
        reasons.append("接近/突破 52 週高"); score += 15
    if f.get("is_stage2"):
        reasons.append("Minervini Stage 2 (" + str(f["stage2_passed"]) + "/7 通過)"); score += 15
    if f.get("is_tight"):
        reasons.append("近 20 日 tight base (" + str(f["tight_range"]) + "%)"); score += 10
    if f.get("rvol") and f["rvol"] >= 2:
        reasons.append("RVOL " + str(f["rvol"]) + "x"); score += 10
    if f.get("is_vpt_up"):
        reasons.append("量價同向 (VPT 上升)"); score += 5
    if (f.get("rs_vs_spy") or 0) > 0:
        reasons.append("RS vs SPY +" + str(f["rs_vs_spy"]) + "%"); score += 5
    if (f.get("rsi") or 50) > 80:
        warnings.append("RSI " + str(f["rsi"]) + " 過熱"); score -= 10
    pct_hi = f.get("pct_from_52w_high")
    if pct_hi is None:
        upside = 12.0
    elif pct_hi >= 0:
        upside = 15.0
    else:
        upside = abs(pct_hi) * 1.2 + 8
    return _build_pick(f, "breakout", score, reasons, warnings, upside, theme_data)


def _check_acceleration(f: Dict, theme_data: Optional[Dict] = None) -> Optional[Dict]:
    """放寬 ATH 上限到 -70%, 距 ATH < -40% 仍可入列但分數打折."""
    if not f.get("is_accel"):
        return None
    if (f.get("rvol") or 0) < 1.5:
        return None
    if f.get("pct_from_ath") is None or f["pct_from_ath"] > -3:
        return None
    if f["pct_from_ath"] < -70:
        return None  # 太極端, 留給 revival_setup
    if (f.get("rsi") or 50) > 80:
        return None
    is_deep_below_ath = f["pct_from_ath"] < -40
    score = 55
    reasons = []; warnings = []
    reasons.append("動能加速 (5d > 10d > 20d > 60d)")
    rates = f.get("accel_rates") or {}
    if rates.get("rate_5d"):
        reasons.append("近 5d 每日平均 +" + "{:.2f}".format(rates["rate_5d"]) + "%")
    if f.get("rvol") and f["rvol"] >= 2:
        reasons.append("RVOL " + str(f["rvol"]) + "x"); score += 10
    if (f.get("rs_vs_spy") or 0) > 5:
        reasons.append("RS vs SPY +" + str(f["rs_vs_spy"]) + "%"); score += 10
    if (f.get("twenty_pct") or 0) > 10:
        reasons.append("20d +" + str(f["twenty_pct"]) + "%"); score += 5
    if f.get("is_vpt_up"):
        reasons.append("量價同向"); score += 5
    if (f.get("pct_from_ath") or -100) >= -15:
        reasons.append("距 ATH 僅 " + str(-f["pct_from_ath"]) + "% (有突破潛力)"); score += 10
    if (f.get("rsi") or 50) >= 75:
        warnings.append("RSI " + str(f["rsi"]) + " 偏熱")
    if not f.get("is_stage2"):
        warnings.append("尚未通過 Stage 2 確認")
    if is_deep_below_ath:
        warnings.append("距 ATH " + "{:.0f}".format(f["pct_from_ath"]) + "% (深跌重生, 風險高)")
        score -= 10
    upside = abs(f.get("pct_from_ath", -10))
    return _build_pick(f, "acceleration", score, reasons, warnings, upside, theme_data)


def _check_squeeze_setup(f: Dict, theme_data: Optional[Dict] = None) -> Optional[Dict]:
    if not (f.get("is_tight") or f.get("bb_squeeze")):
        return None
    if not f.get("vol_dry_5d"):
        return None
    if f.get("pct_from_52w_high") is None or f["pct_from_52w_high"] < -25:
        return None
    if (f.get("rvol") or 0) > 1.5:
        return None
    if (f.get("today_pct") or 0) > 5:
        return None
    score = 50
    reasons = []; warnings = []
    if f.get("is_very_tight"):
        reasons.append("極窄整理 (近 10 日 ≤ 5%)"); score += 20
    elif f.get("is_tight"):
        reasons.append("tight base (近 20 日 " + str(f["tight_range"]) + "%)"); score += 15
    if f.get("bb_squeeze"):
        reasons.append("BB 寬度近 60 日 percentile 20% 以下"); score += 15
    reasons.append("量縮整理 (籌碼沉澱)")
    if f.get("base_depth") and 5 <= f["base_depth"] <= 15:
        reasons.append("健康 base 深度 " + str(f["base_depth"]) + "%"); score += 10
    if f.get("is_stage2"):
        reasons.append("Stage 2 confirmed"); score += 10
    if (f.get("rs_vs_spy") or 0) > 0:
        reasons.append("RS vs SPY +" + str(f["rs_vs_spy"]) + "%"); score += 5
    if f.get("base_depth") and f["base_depth"] > 25:
        warnings.append("base 過深 " + str(f["base_depth"]) + "%"); score -= 15
    upside = 12.0 + (abs(f.get("pct_from_52w_high", -10)) * 0.5)
    return _build_pick(f, "squeeze_setup", score, reasons, warnings, upside, theme_data)


def _check_revival_setup(f: Dict, theme_data: Optional[Dict] = None) -> Optional[Dict]:
    """谷底重生 — RKLB / PLTR / SOFI 型. 距 ATH 深跌但正在重啟趨勢."""
    pct_ath = f.get("pct_from_ath")
    pct_52w_lo = f.get("pct_from_52w_low")
    if pct_ath is None or pct_ath > -40:
        return None
    if pct_ath < -90:
        return None
    if pct_52w_lo is None or pct_52w_lo < 40:
        return None
    if (f.get("twenty_pct") or 0) < 0:
        return None
    if (f.get("rvol") or 0) < 1.2:
        return None
    cur = f.get("current")
    ma60 = f.get("ma60")
    if cur and ma60 and cur <= ma60 * 0.95:
        return None
    score = 50
    reasons = []; warnings = []
    reasons.append("距 ATH " + "{:.0f}".format(pct_ath) + "% / 距 52w 低 +" + "{:.0f}".format(pct_52w_lo) + "% (谷底重生)")
    if (f.get("twenty_pct") or 0) > 10:
        reasons.append("20d +" + "{:.1f}".format(f["twenty_pct"]) + "% (動能強)"); score += 10
    if (f.get("rvol") or 0) >= 2:
        reasons.append("RVOL " + str(f["rvol"]) + "x (大量資金進場)"); score += 15
    if f.get("is_accel"):
        reasons.append("動能正在加速 (5d > 10d > 20d > 60d)"); score += 15
    if f.get("ma_bullish_alignment"):
        reasons.append("MA 多頭排列確認"); score += 10
    if (f.get("rs_vs_spy") or 0) > 5:
        reasons.append("RS vs SPY +" + str(f["rs_vs_spy"]) + "%"); score += 5
    if f.get("is_vpt_up"):
        reasons.append("量價同向"); score += 5
    if (f.get("rsi") or 50) > 75:
        warnings.append("RSI " + str(f["rsi"]) + " 偏熱, 短線可能拉回")
    if pct_ath < -70:
        warnings.append("距 ATH " + "{:.0f}".format(pct_ath) + "% 較深, 需確認基本面已轉")
    upside = abs(pct_ath) / 3
    return _build_pick(f, "revival_setup", score, reasons, warnings, upside, theme_data)


def _check_narrative_leader(f: Dict, theme_data: Optional[Dict]) -> Optional[Dict]:
    """純題材但已有基本動能 — 前 4 類抓不到的補集."""
    if not theme_data:
        return None
    t = theme_data.get("total_score", 0)
    if t < 50:
        return None
    if (f.get("rs_vs_spy") or 0) < -3:
        return None
    if f.get("pct_from_52w_high") is None or f["pct_from_52w_high"] < -30:
        return None
    if (f.get("twenty_pct") or 0) < 0:
        return None
    score = 45
    reasons = []; warnings = []
    reasons.append("題材熱度 " + str(t) + "/100 (" + theme_data.get("theme_strength", "?") + ")")
    if (f.get("rs_vs_spy") or 0) > 5:
        reasons.append("RS vs SPY +" + str(f["rs_vs_spy"]) + "%"); score += 10
    if (f.get("twenty_pct") or 0) > 5:
        reasons.append("20d +" + str(f["twenty_pct"]) + "%"); score += 5
    if (f.get("rvol") or 0) >= 1.5:
        reasons.append("RVOL " + str(f["rvol"]) + "x"); score += 10
    if f.get("is_vpt_up"):
        reasons.append("量價同向"); score += 5
    if theme_data.get("sector_rotation_rank") and theme_data["sector_rotation_rank"] <= 3:
        reasons.append("板塊強勢 rank " + str(theme_data["sector_rotation_rank"])); score += 5
    if theme_data.get("news_count", 0) >= 4:
        reasons.append("近期新聞密集 (" + str(theme_data["news_count"]) + " 則)"); score += 5
    if (f.get("rsi") or 50) > 75:
        warnings.append("RSI " + str(f["rsi"]) + " 偏熱")
    if not f.get("is_stage2"):
        warnings.append("技術尚未到 Stage 2")
    upside = abs(f.get("pct_from_52w_high", -15))
    return _build_pick(f, "narrative_leader", score, reasons, warnings, upside, theme_data)


def _build_pick(f: Dict, category: str, score: int,
                 reasons: List[str], warnings: List[str], upside_pct: float,
                 theme_data: Optional[Dict] = None) -> Dict:
    base_score = max(0, min(100, int(score)))
    theme_mult = theme.theme_multiplier(theme_data) if theme_data else 1.0
    final_score = max(0, min(100, int(base_score * theme_mult)))
    if theme_data:
        tags = theme_data.get("narrative_tags", [])
        strength = theme_data.get("theme_strength", "none")
        if tags:
            reasons.insert(0, "題材[" + strength + "]: " + ", ".join(tags[:3]))
        if theme_data.get("sector_rotation_rank"):
            r = theme_data["sector_rotation_rank"]
            if r <= 3:
                reasons.append("板塊輪動 rank " + str(r) + "/11 (強勢)")
        earn_d = theme_data.get("earnings_in_days")
        if earn_d is not None and 0 <= earn_d <= 14:
            warnings.append("財報 " + str(earn_d) + " 天內公佈 (binary risk)")
    metrics = {
        "today_pct": f.get("today_pct"), "five_pct": f.get("five_pct"),
        "twenty_pct": f.get("twenty_pct"), "sixty_pct": f.get("sixty_pct"),
        "rvol": f.get("rvol"), "rsi": f.get("rsi"),
        "rs_vs_spy": f.get("rs_vs_spy"),
        "pct_from_52w_high": f.get("pct_from_52w_high"),
        "pct_from_ath": f.get("pct_from_ath"),
        "is_stage2": f.get("is_stage2"),
        "is_tight": f.get("is_tight"),
        "bb_squeeze": f.get("bb_squeeze"),
        "theme_score": theme_data.get("total_score") if theme_data else None,
        "theme_strength": theme_data.get("theme_strength") if theme_data else None,
        "narrative_tags": theme_data.get("narrative_tags", []) if theme_data else [],
        "earnings_in_days": theme_data.get("earnings_in_days") if theme_data else None,
    }
    # 合併 short / mid / long targets 到 levels
    merged_levels = dict(f.get("levels") or {})
    fib = f.get("fib_targets") or {}
    mm = f.get("measured_move") or {}
    if fib:
        merged_levels["target_fib_127"] = fib.get("fib_127")
        merged_levels["target_fib_162"] = fib.get("fib_162")
        merged_levels["target_fib_262"] = fib.get("fib_262")
        merged_levels["fib_swing"] = f"{fib.get('swing_low')}-{fib.get('swing_high')}"
    if mm:
        merged_levels["target_measured_move"] = mm.get("target")
        merged_levels["target_mm_conservative"] = mm.get("target_conservative")
        merged_levels["mm_base"] = f"{mm.get('base_low')}-{mm.get('base_high')} ({mm.get('base_height_pct')}%)"
    # target_fundamental 由 _run_impl 在 Gemini 結果回來後寫入

    return {
        "symbol": f["symbol"], "category": category,
        "current": f["current"],
        "score": final_score, "base_score": base_score,
        "theme_multiplier": round(theme_mult, 2),
        "upside_pct": round(float(upside_pct), 1),
        "reasons": reasons, "warnings": warnings,
        "levels": merged_levels,
        "metrics": metrics,
    }


_US_UPSIDE_CACHE_TTL = 30 * 60


@st.cache_data(ttl=_US_UPSIDE_CACHE_TTL, show_spinner=False)
def _cached_us_upside_screen(universe_tuple: tuple, max_workers: int,
                              with_themes: bool) -> Dict:
    return _run_impl(list(universe_tuple), max_workers=max_workers, with_themes=with_themes)


def run_us_upside_screen(top_n_per_category: int = 5,
                          universe: Optional[List[str]] = None,
                          max_workers: int = 5, use_cache: bool = True,
                          with_themes: bool = True,
                          with_entry_label: bool = True) -> Dict:
    syms = universe if universe else DEFAULT_US_UNIVERSE
    syms = list(dict.fromkeys(syms))
    if use_cache:
        full = _cached_us_upside_screen(tuple(syms), max_workers, with_themes)
    else:
        full = _run_impl(syms, max_workers, with_themes=with_themes)
    for k in ("breakout", "acceleration", "squeeze_setup", "revival_setup", "narrative_leader"):
        full[k] = full.get(k, [])[:top_n_per_category]

    # B: 對所有 picks 批次跑 quick entry 評估, 加 entry_label/emoji/score/action
    if with_entry_label:
        try:
            import entry_label_helper as _el
            all_syms = set()
            for k in ("breakout", "acceleration", "squeeze_setup", "revival_setup", "narrative_leader"):
                for p in full.get(k, []):
                    s = p.get("symbol")
                    if s:
                        all_syms.add(s)
            if all_syms:
                pairs = [(s, "US") for s in all_syms]
                eval_map = _el.batch_evaluate(pairs, max_workers=max_workers)
                for k in ("breakout", "acceleration", "squeeze_setup", "revival_setup", "narrative_leader"):
                    for p in full.get(k, []):
                        s = p.get("symbol")
                        ev = eval_map.get(s) or {}
                        p["entry_label"] = ev.get("entry_label", "—")
                        p["entry_emoji"] = ev.get("entry_emoji", "")
                        p["entry_score"] = ev.get("entry_score")
                        p["entry_action"] = ev.get("entry_action", "—")
        except Exception as _e:
            print(f"[us_upside] entry_label 計算失敗 (non-fatal): {_e}", flush=True)
    return full


def _run_impl(symbols: List[str], max_workers: int = 5,
                with_themes: bool = True) -> Dict:
    print("[us_upside] scanning " + str(len(symbols)) + " symbols...", flush=True)
    spy_df = _fetch_yf_one("SPY", period="3mo")
    features = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_yf_one, s, "1y"): s for s in symbols}
        for fut in as_completed(futures):
            s = futures[fut]
            df = fut.result()
            if df is None:
                continue
            f = _compute_us_features(s, df, spy_df=spy_df)
            if f:
                features.append(f)

    theme_map: Dict[str, Dict] = {}
    if with_themes and features:
        try:
            symbols_for_theme = [f["symbol"] for f in features]
            theme_map = theme.batch_theme_scores(symbols_for_theme, max_workers=max_workers)
        except Exception as e:
            print("[us_upside] theme analysis failed: " + str(e), flush=True)

    breakout, acceleration, squeeze, revival, narrative = [], [], [], [], []
    classified_syms: set = set()
    for f in features:
        sym = f["symbol"]
        td = theme_map.get(sym)
        hit_count = 0
        if (p := _check_breakout(f, td)):
            breakout.append(p); hit_count += 1
        if (p := _check_acceleration(f, td)):
            acceleration.append(p); hit_count += 1
        if (p := _check_squeeze_setup(f, td)):
            squeeze.append(p); hit_count += 1
        if (p := _check_revival_setup(f, td)):
            revival.append(p); hit_count += 1
        if hit_count > 0:
            classified_syms.add(sym)

    if with_themes:
        for f in features:
            sym = f["symbol"]
            if sym in classified_syms:
                continue
            td = theme_map.get(sym)
            if td and (p := _check_narrative_leader(f, td)):
                narrative.append(p)

    for lst in (breakout, acceleration, squeeze, revival, narrative):
        lst.sort(key=lambda x: x["score"], reverse=True)

    by_sym: Dict[str, Dict] = {}
    for p in breakout + acceleration + squeeze + revival + narrative:
        sym = p["symbol"]
        if sym not in by_sym or p["score"] > by_sym[sym]["score"]:
            by_sym[sym] = p
    all_picks = sorted(by_sym.values(), key=lambda x: x["score"], reverse=True)

    # Gemini-based fundamental long-term target — 只對 top N 高分 picks 呼叫 (省 quota)
    # 每天每 symbol 只打 1 次 (內部 24h cache)
    gemini_targets_loaded = 0
    if with_themes and _GTE_OK and all_picks:
        try:
            top_for_gemini = [p for p in all_picks[:8] if p.get("score", 0) >= 70]
            if top_for_gemini:
                feat_map = {}
                t_map = {}
                for p in top_for_gemini:
                    sym = p["symbol"]
                    m = p.get("metrics") or {}
                    feat_map[sym] = {"current": p.get("current"),
                                       "pct_from_52w_high": m.get("pct_from_52w_high"),
                                       "pct_from_ath": m.get("pct_from_ath"),
                                       "twenty_pct": m.get("twenty_pct"),
                                       "sixty_pct": m.get("sixty_pct"),
                                       "rsi": m.get("rsi")}
                    if m.get("theme_score") is not None:
                        t_map[sym] = {"total_score": m["theme_score"],
                                       "theme_strength": m.get("theme_strength"),
                                       "narrative_tags": m.get("narrative_tags", [])}
                g_results = _gte.estimate_batch(
                    [p["symbol"] for p in top_for_gemini],
                    features_map=feat_map, theme_map=t_map, max_calls=8,
                )
                for p in all_picks:
                    g = g_results.get(p["symbol"])
                    if g:
                        gemini_targets_loaded += 1
                        lv = p.setdefault("levels", {})
                        lv["target_fundamental_3m"] = g.get("target_3m")
                        lv["target_fundamental_6m"] = g.get("target_6m")
                        lv["target_fundamental_bull"] = g.get("bull_target")
                        lv["target_fundamental_bear"] = g.get("bear_target")
                        lv["fundamental_confidence"] = g.get("confidence")
                        lv["fundamental_reasoning"] = g.get("reasoning")
        except Exception as e:
            print("[us_upside] gemini target failed: " + str(e), flush=True)

    import datetime as dt
    return {
        "breakout": breakout,
        "acceleration": acceleration,
        "squeeze_setup": squeeze,
        "revival_setup": revival,
        "narrative_leader": narrative,
        "all": all_picks[:50],
        "meta": {
            "scanned": len(features),
            "universe_size": len(symbols),
            "data_date": dt.date.today().strftime("%Y-%m-%d"),
            "breakout_count": len(breakout),
            "acceleration_count": len(acceleration),
            "squeeze_count": len(squeeze),
            "revival_count": len(revival),
            "narrative_count": len(narrative),
            "themes_loaded": len(theme_map),
            "gemini_targets_loaded": gemini_targets_loaded,
        }
    }


def fmt_summary_md(result: Dict, per_category: int = 5) -> str:
    lines = ["# 🚀 美股潛在爆發股清單"]
    meta = result.get("meta", {})
    lines.append("_掃描 " + str(meta.get("scanned", "?")) + "/" + str(meta.get("universe_size", "?")) + " 檔 · 資料日 " + str(meta.get("data_date")) + "_\n")
    for key in ("breakout", "acceleration", "squeeze_setup", "revival_setup", "narrative_leader"):
        label = CATEGORY_LABEL_US.get(key, key)
        picks = (result.get(key) or [])[:per_category]
        lines.append("\n## " + label + " (共 " + str(len(result.get(key) or [])) + " 檔)")
        if not picks:
            lines.append("_(無符合標的)_")
            continue
        for i, p in enumerate(picks, 1):
            lv = p.get("levels") or {}
            lines.append("\n**" + str(i) + ". " + p["symbol"] + "** · 分數 " + str(p.get("score", "—")))
            lines.append("- 現價: " + str(p.get("price", "—")))
            if lv:
                lines.append("- 目標: " + str(lv))
        lines.append("")
    return "\n".join(lines)
