"""
us_upside_screener.py
專門找美股「即將爆發」的潛力股, 分三類:

  1. breakout       — 52w 高突破 / Stage 2 + tight base
  2. acceleration   — 動能加速 (5d > 10d > 20d 速率遞增) + RVOL ≥ 2 + 距 ATH < 15%
  3. squeeze_setup  — tight consolidation + 量縮 + 接近關鍵壓力 (準備噴出)

設計重點:
  - 純 yfinance, 不需要額外 API key
  - 用 indicators.py 的美股專用指標 (is_52w_high_breakout, momentum_acceleration,
    is_tight_consolidation, distance_from_ath, is_minervini_stage2)
  - 三類各自有明確的「進場 / 目標 / 停損」ATR-based levels
  - Universe 可擴展 (預設 ~150 檔, 涵蓋 mega-cap + mid-cap + IPO)

對外接口:
    run_us_upside_screen(top_n_per_category=5, universe=None) -> dict

注意: 比台股版慢 (yfinance batch 限速), 建議用 cache wrapper.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import data_sources as ds
import indicators as ind
import theme_analyzer as theme

# Streamlit cache 與 upside_screener 一致
try:
    import streamlit as st  # type: ignore
    _ST_OK = True
except Exception:
    _ST_OK = False
    class _NoOp:
        def cache_data(self, *args, **kwargs):
            def deco(f):
                return f
            if len(args) == 1 and callable(args[0]):
                return args[0]
            return deco
    st = _NoOp()  # type: ignore


# ---------------------------------------------------------------------------
# Universe (~150 檔)
# ---------------------------------------------------------------------------
DEFAULT_US_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "ORCL",
    "CRM", "ADBE", "AMD", "QCOM", "TXN", "INTC", "MU", "ASML",
    # AI / cloud / cyber leaders
    "PLTR", "SNOW", "CRWD", "PANW", "NET", "DDOG", "MDB", "SMCI", "ARM", "MRVL",
    "ZS", "OKTA", "S", "FTNT", "ANET", "ALAB",
    # Semiconductor mid-caps
    "ON", "AMAT", "LRCX", "KLAC", "MCHP", "SWKS", "QRVO", "AMBA", "RMBS",
    # Megacap consumer / financial
    "BRK-B", "JPM", "BAC", "V", "MA", "WMT", "COST", "PG", "JNJ", "UNH", "HD",
    "NFLX", "DIS", "MCD", "SBUX", "NKE", "ABNB", "UBER", "SHOP",
    # AI / quantum / new gen
    "RDDT", "CRWV", "ASTS", "RBLX", "IONQ", "RGTI", "QBTS", "SOUN", "BBAI",
    "AI", "GRAB", "BROS", "RKLB",
    # Crypto-related (有獨立爆發週期)
    "COIN", "MSTR", "MARA", "RIOT", "HUT",
    # Nuclear / energy
    "OKLO", "SMR", "CEG", "VST", "NEE", "EOG", "CVX", "XOM",
    # EV / transport
    "TSLA", "RIVN", "LCID", "F", "GM",
    # IPO / 2023-2024 high-momentum
    "CART", "KLC", "BIRK", "DUOL", "TOST", "DKNG",
    # Pharma / biotech leaders
    "LLY", "NVO", "REGN", "VRTX",
    # Fintech / consumer fintech
    "HOOD", "SOFI", "AFRM", "PYPL", "SQ",
    # Industrial / defense
    "GE", "BA", "CAT", "DE", "RTX", "LMT", "NOC",
    # Misc growth
    "TEM", "RGTI", "OUST", "CPRT", "MELI", "PINS", "U", "TWLO",
]


# ---------------------------------------------------------------------------
# 抓單檔 yfinance daily
# ---------------------------------------------------------------------------
def _fetch_yf_one(symbol: str, period: str = "1y") -> Optional[pd.DataFrame]:
    """抓單檔 yfinance 日線. 失敗回 None."""
    try:
        df = ds.fetch_yf_history(symbol, period=period, interval="1d")
        if df is None or df.empty or len(df) < 60:
            return None
        return df
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 核心 features 計算
# ---------------------------------------------------------------------------
def _compute_us_features(symbol: str, df: pd.DataFrame,
                          spy_df: Optional[pd.DataFrame] = None) -> Optional[Dict]:
    """從 yfinance daily 算所有美股 features."""
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

        # 動能
        today_pct = (cur / prev - 1) * 100 if prev > 0 else 0
        five_pct = (cur / float(c.iloc[-6]) - 1) * 100 if len(c) >= 6 else None
        twenty_pct = (cur / float(c.iloc[-21]) - 1) * 100 if len(c) >= 21 else None
        sixty_pct = (cur / float(c.iloc[-61]) - 1) * 100 if len(c) >= 61 else None

        # 52w / ATH
        hi52, lo52, pct_hi52, pct_lo52 = ind.distance_from_52w(c, window=252)
        ath, pct_from_ath = ind.distance_from_ath(c)

        # Breakout / Stage 2
        is_52w_brk, brk_info = ind.is_52w_high_breakout(c, window=252, breakout_tolerance=1.0)
        is_stage2, stage2_info = ind.is_minervini_stage2(c)

        # Acceleration
        is_accel, accel_info = ind.momentum_acceleration(c)

        # Tight consolidation / base
        is_tight, tight_range = ind.is_tight_consolidation(c, lookback=20, max_range_pct=8.0)
        is_very_tight, _ = ind.is_tight_consolidation(c, lookback=10, max_range_pct=5.0)
        base_depth = ind.base_depth_pct(c, base_window=30)

        # 量能
        rvol_today = ind.rvol(v, lookback=30)
        vol_dry_5d, _ = ind.volume_dryup(v, recent=5, base=30, ratio_threshold=0.8)
        is_vpt_up = ind.vpt_uptrend(c, v, lookback=20)

        # RSI / BB
        rsi_s = ind.rsi(c, 14)
        rsi_now = float(rsi_s.iloc[-1]) if not rsi_s.empty else None
        _, _, _, bb_w = ind.bollinger_bands(c, 20, 2.0)
        bb_squeeze = ind.is_bb_squeeze(bb_w, lookback=60, percentile=20)

        # RS vs SPY
        rs_vs_spy = None
        if spy_df is not None and not spy_df.empty and len(spy_df) >= 22 and len(c) >= 22:
            spy_c = spy_df["Close"].astype(float)
            try:
                spy_20 = (float(spy_c.iloc[-1]) / float(spy_c.iloc[-21]) - 1) * 100
                stock_20 = (cur / float(c.iloc[-21]) - 1) * 100
                rs_vs_spy = round(stock_20 - spy_20, 2)
            except (IndexError, ZeroDivisionError):
                pass

        # ATR levels
        levels = ind.atr_based_levels(h, l, c, stop_atr_mult=1.5, target_atr_mult=3.0) or {}

        return {
            "symbol": symbol, "current": round(cur, 2),
            "today_pct": round(today_pct, 2),
            "five_pct": round(five_pct, 2) if five_pct is not None else None,
            "twenty_pct": round(twenty_pct, 2) if twenty_pct is not None else None,
            "sixty_pct": round(sixty_pct, 2) if sixty_pct is not None else None,
            "rvol": rvol_today,
            "rsi": round(rsi_now, 1) if rsi_now is not None else None,
            "rs_vs_spy": rs_vs_spy,
            # 52w / ATH
            "hi52": round(hi52, 2) if hi52 else None,
            "lo52": round(lo52, 2) if lo52 else None,
            "pct_from_52w_high": round(pct_hi52, 2) if pct_hi52 is not None else None,
            "pct_from_52w_low": round(pct_lo52, 2) if pct_lo52 is not None else None,
            "ath": round(ath, 2) if ath else None,
            "pct_from_ath": round(pct_from_ath, 2) if pct_from_ath is not None else None,
            # Breakout
            "is_52w_breakout": is_52w_brk,
            "is_fresh_breakout": brk_info.get("is_fresh_breakout", False),
            "is_stage2": is_stage2,
            "stage2_passed": stage2_info.get("passed_checks", 0),
            # Acceleration
            "is_accel": is_accel,
            "accel_rates": accel_info,
            # Consolidation
            "is_tight": is_tight, "tight_range": tight_range,
            "is_very_tight": is_very_tight,
            "base_depth": base_depth,
            # Volume
            "vol_dry_5d": vol_dry_5d,
            "is_vpt_up": is_vpt_up,
            # BB
            "bb_squeeze": bb_squeeze,
            # Levels
            "levels": levels,
        }
    except Exception as e:
        print(f"[us_upside] features {symbol} failed: {e}", flush=True)
        return None


# ---------------------------------------------------------------------------
# 三類判定 + 評分
# ---------------------------------------------------------------------------
def _check_breakout(f: Dict, theme_data: Optional[Dict] = None) -> Optional[Dict]:
    """類別 1: 52w high breakout / Minervini Stage 2 + tight base."""
    # 必要條件: 在 52w 高附近 + 有 stage 2 趨勢
    if not f.get("is_52w_breakout") and not f.get("is_stage2"):
        return None
    if f.get("pct_from_52w_high") is None or f["pct_from_52w_high"] < -10:
        return None  # 距 52w 高超過 10% 就不是 breakout setup
    if (f.get("rvol") or 0) < 1.3:
        return None  # 至少要 1.3x RVOL 確認

    score = 50
    reasons = []
    warnings = []

    if f.get("is_fresh_breakout"):
        reasons.append("剛突破 52 週新高")
        score += 25
    elif f.get("is_52w_breakout"):
        reasons.append("接近/突破 52 週高")
        score += 15
    if f.get("is_stage2"):
        reasons.append(f"Minervini Stage 2 ({f['stage2_passed']}/7 通過)")
        score += 15
    if f.get("is_tight"):
        reasons.append(f"近 20 日 tight base ({f['tight_range']}%)")
        score += 10
    if f.get("rvol") and f["rvol"] >= 2:
        reasons.append(f"RVOL {f['rvol']}x")
        score += 10
    if f.get("is_vpt_up"):
        reasons.append("量價同向 (VPT 上升)")
        score += 5
    if (f.get("rs_vs_spy") or 0) > 0:
        reasons.append(f"RS vs SPY +{f['rs_vs_spy']}%")
        score += 5

    # 警示
    if (f.get("rsi") or 50) > 80:
        warnings.append(f"RSI {f['rsi']} 過熱")
        score -= 10
    if f.get("base_depth") and f["base_depth"] > 25:
        warnings.append(f"base 深度 {f['base_depth']}% 偏深")

    # L2 修正: 區分「距 52w 高還有空間」與「已突破 52w 高」兩種情境
    pct_hi = f.get("pct_from_52w_high")
    if pct_hi is None:
        upside = 12.0  # default
    elif pct_hi >= 0:
        # 已突破 52w 高 → 新高無前波壓力, 給保守 15% 預估
        upside = 15.0
    else:
        # 距 52w 高還有 X% → 攻回前高 + 多漲一段
        upside = abs(pct_hi) * 1.2 + 8
    return _build_pick(f, "breakout", score, reasons, warnings, upside, theme_data)


def _check_acceleration(f: Dict, theme_data: Optional[Dict] = None) -> Optional[Dict]:
    """類別 2: 動能加速 — 5d > 10d > 20d 速率遞增, 距 ATH 還有空間."""
    if not f.get("is_accel"):
        return None
    if (f.get("rvol") or 0) < 1.5:
        return None
    if f.get("pct_from_ath") is None or f["pct_from_ath"] > -3:
        return None  # 距 ATH > -3% 已經是 breakout 階段, 不算 "acceleration"
    if f["pct_from_ath"] < -40:
        return None  # 距 ATH > 40% 算反轉股, 不是 acceleration
    if (f.get("rsi") or 50) > 80:
        return None  # 過熱

    score = 55
    reasons = []
    warnings = []
    reasons.append("動能加速 (5d > 10d > 20d > 60d)")
    rates = f.get("accel_rates") or {}
    if rates.get("rate_5d"):
        reasons.append(f"近 5d 每日平均 +{rates['rate_5d']:.2f}%")

    if f.get("rvol") and f["rvol"] >= 2:
        reasons.append(f"RVOL {f['rvol']}x")
        score += 10
    if (f.get("rs_vs_spy") or 0) > 5:
        reasons.append(f"RS vs SPY +{f['rs_vs_spy']}% (強勢)")
        score += 10
    if (f.get("twenty_pct") or 0) > 10:
        reasons.append(f"20d +{f['twenty_pct']:.1f}%")
        score += 5
    if f.get("is_vpt_up"):
        reasons.append("量價同向")
        score += 5
    if (f.get("pct_from_ath") or -100) >= -15:
        reasons.append(f"距 ATH 僅 {-f['pct_from_ath']:.0f}% (有突破潛力)")
        score += 10

    if (f.get("rsi") or 50) >= 75:
        warnings.append(f"RSI {f['rsi']} 偏熱")
    if not f.get("is_stage2"):
        warnings.append("尚未通過 Stage 2 確認")

    upside = abs(f.get("pct_from_ath", -10))
    return _build_pick(f, "acceleration", score, reasons, warnings, upside, theme_data)


def _check_squeeze_setup(f: Dict, theme_data: Optional[Dict] = None) -> Optional[Dict]:
    """類別 3: 量縮整理 + BB squeeze + 接近壓力 (即將噴出)."""
    if not (f.get("is_tight") or f.get("bb_squeeze")):
        return None
    if not f.get("vol_dry_5d"):
        return None
    if f.get("pct_from_52w_high") is None or f["pct_from_52w_high"] < -25:
        return None  # 距 52w 高超過 25% 不是 squeeze, 是下跌中
    # 不能已經爆量噴出
    if (f.get("rvol") or 0) > 1.5:
        return None
    if (f.get("today_pct") or 0) > 5:
        return None

    score = 50
    reasons = []
    warnings = []
    if f.get("is_very_tight"):
        reasons.append(f"極窄整理 (近 10 日 ≤ 5%)")
        score += 20
    elif f.get("is_tight"):
        reasons.append(f"tight base (近 20 日 {f['tight_range']}%)")
        score += 15
    if f.get("bb_squeeze"):
        reasons.append("BB 寬度近 60 日 percentile 20% 以下 (波動率壓縮)")
        score += 15
    reasons.append("量縮整理 (籌碼沉澱)")
    if f.get("base_depth") and 5 <= f["base_depth"] <= 15:
        reasons.append(f"健康 base 深度 {f['base_depth']}%")
        score += 10
    if f.get("is_stage2"):
        reasons.append(f"Stage 2 confirmed")
        score += 10
    if (f.get("rs_vs_spy") or 0) > 0:
        reasons.append(f"RS vs SPY +{f['rs_vs_spy']}%")
        score += 5

    if f.get("base_depth") and f["base_depth"] > 25:
        warnings.append(f"base 過深 {f['base_depth']}%, 不是健康整理")
        score -= 15

    upside = 12.0 + (abs(f.get("pct_from_52w_high", -10)) * 0.5)
    return _build_pick(f, "squeeze_setup", score, reasons, warnings, upside, theme_data)


def _build_pick(f: Dict, category: str, score: int,
                 reasons: List[str], warnings: List[str], upside_pct: float,
                 theme_data: Optional[Dict] = None) -> Dict:
    """組裝 pick. 若有 theme_data, score 乘 theme_multiplier 並把題材標籤加進 reasons."""
    base_score = max(0, min(100, int(score)))

    # 題材調整
    theme_mult = theme.theme_multiplier(theme_data) if theme_data else 1.0
    final_score = max(0, min(100, int(base_score * theme_mult)))

    # 把題材資訊加進 reasons / metrics
    if theme_data:
        tags = theme_data.get("narrative_tags", [])
        strength = theme_data.get("theme_strength", "none")
        if tags:
            reasons.insert(0, f"題材[{strength}]: {', '.join(tags[:3])}")
        if theme_data.get("sector_rotation_rank"):
            r = theme_data["sector_rotation_rank"]
            if r <= 3:
                reasons.append(f"板塊輪動 rank {r}/11 (強勢)")
        earn_d = theme_data.get("earnings_in_days")
        if earn_d is not None and 0 <= earn_d <= 14:
            warnings.append(f"財報 {earn_d} 天內公佈 (binary risk)")

    metrics = {
        "今日%": f.get("today_pct"), "5日%": f.get("five_pct"),
        "20日%": f.get("twenty_pct"), "60日%": f.get("sixty_pct"),
        "rvol": f.get("rvol"), "rsi": f.get("rsi"),
        "rs_vs_spy": f.get("rs_vs_spy"),
        "pct_from_52w_high": f.get("pct_from_52w_high"),
        "pct_from_ath": f.get("pct_from_ath"),
        "is_stage2": f.get("is_stage2"),
        "is_tight": f.get("is_tight"),
        "bb_squeeze": f.get("bb_squeeze"),
        # 題材欄位
        "theme_score": theme_data.get("total_score") if theme_data else None,
        "theme_strength": theme_data.get("theme_strength") if theme_data else None,
        "narrative_tags": theme_data.get("narrative_tags", []) if theme_data else [],
        "earnings_in_days": theme_data.get("earnings_in_days") if theme_data else None,
    }
    return {
        "symbol": f["symbol"], "category": category,
        "current": f["current"],
        "score": final_score, "base_score": base_score,
        "theme_multiplier": round(theme_mult, 2),
        "upside_pct": round(float(upside_pct), 1),
        "reasons": reasons, "warnings": warnings,
        "levels": f.get("levels") or {},
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# 第 4 類: narrative_leader — 純題材 leader, 技術 setup 還沒到 stage 2
# ---------------------------------------------------------------------------
def _check_narrative_leader(f: Dict, theme_data: Optional[Dict]) -> Optional[Dict]:
    """類別 4: 強題材 + 已啟動但還沒大噴 (作為其他三類抓不到的補集).
    這類最容易被純技術 screener 漏掉, 因為他們的指標還沒成熟.
    """
    if not theme_data:
        return None
    t = theme_data.get("total_score", 0)
    if t < 50:
        return None  # 題材不夠強, 跳過
    # 必須有一些 technical confirmation 才能加入 (避免純炒題材的妖股)
    if (f.get("rs_vs_spy") or 0) < -3:
        return None  # RS 太弱 (即使有題材也跟不上大盤)
    if f.get("pct_from_52w_high") is None or f["pct_from_52w_high"] < -30:
        return None  # 距 52w 高超過 30% 不算 leader
    if (f.get("twenty_pct") or 0) < 0:
        return None  # 20 日仍跌, 不是 leader

    score = 45
    reasons = []
    warnings = []

    reasons.append(f"題材熱度 {t}/100 ({theme_data.get('theme_strength')})")
    if (f.get("rs_vs_spy") or 0) > 5:
        reasons.append(f"RS vs SPY +{f['rs_vs_spy']}%")
        score += 10
    if (f.get("twenty_pct") or 0) > 5:
        reasons.append(f"20d +{f['twenty_pct']}%")
        score += 5
    if (f.get("rvol") or 0) >= 1.5:
        reasons.append(f"RVOL {f['rvol']}x")
        score += 10
    if f.get("is_vpt_up"):
        reasons.append("量價同向")
        score += 5
    if theme_data.get("sector_rotation_rank") and theme_data["sector_rotation_rank"] <= 3:
        reasons.append(f"板塊強勢 rank {theme_data['sector_rotation_rank']}")
        score += 5
    if theme_data.get("news_count", 0) >= 4:
        reasons.append(f"近期新聞密集 ({theme_data['news_count']} 則)")
        score += 5

    if (f.get("rsi") or 50) > 75:
        warnings.append(f"RSI {f['rsi']} 偏熱")
    if not f.get("is_stage2"):
        warnings.append("技術尚未到 Stage 2")

    # 上漲空間 = 距 52w 高的空間
    upside = abs(f.get("pct_from_52w_high", -15))
    return _build_pick(f, "narrative_leader", score, reasons, warnings, upside, theme_data)


# ---------------------------------------------------------------------------
# 對外接口
# ---------------------------------------------------------------------------
_US_UPSIDE_CACHE_TTL = 30 * 60  # 美股 1 天才更新, 30 分鐘 cache 足夠


@st.cache_data(ttl=_US_UPSIDE_CACHE_TTL, show_spinner=False)
def _cached_us_upside_screen(universe_tuple: tuple, max_workers: int,
                              with_themes: bool) -> Dict:
    return _run_impl(list(universe_tuple), max_workers=max_workers, with_themes=with_themes)


def run_us_upside_screen(top_n_per_category: int = 5,
                          universe: Optional[List[str]] = None,
                          max_workers: int = 5, use_cache: bool = True,
                          with_themes: bool = True) -> Dict:
    """主接口. 預設用 DEFAULT_US_UNIVERSE.

    use_cache=True 走 @st.cache_data 包裝.
    with_themes=True 額外抓題材 / 新聞 / 板塊 / 財報, 並把分數乘 theme_multiplier.
                     False = 純技術面 (與舊版相容, 速度快很多).
    """
    syms = universe if universe else DEFAULT_US_UNIVERSE
    # 去重保持順序
    syms = list(dict.fromkeys(syms))
    if use_cache:
        full = _cached_us_upside_screen(tuple(syms), max_workers, with_themes)
    else:
        full = _run_impl(syms, max_workers, with_themes=with_themes)
    # top_n trim
    for k in ("breakout", "acceleration", "squeeze_setup", "narrative_leader"):
        full[k] = full.get(k, [])[:top_n_per_category]
    return full


def _run_impl(symbols: List[str], max_workers: int = 5,
                with_themes: bool = True) -> Dict:
    """實際執行 — 平行抓 yfinance, 計算 features, 跑四類判定.
    with_themes=True 時還會抓題材 / 新聞 / 板塊 / 財報日 評分.
    """
    print(f"[us_upside] scanning {len(symbols)} symbols…", flush=True)
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

    print(f"[us_upside] {len(features)} 檔特徵計算完成", flush=True)

    # 抓題材分數 (對所有有 features 的, 一次平行抓完)
    theme_map: Dict[str, Dict] = {}
    if with_themes and features:
        print(f"[us_upside] 抓題材 / 新聞 / 板塊 / 財報 ({len(features)} 檔)…", flush=True)
        try:
            symbols_for_theme = [f["symbol"] for f in features]
            theme_map = theme.batch_theme_scores(symbols_for_theme, max_workers=max_workers)
        except Exception as e:
            print(f"[us_upside] theme analysis failed: {e}", flush=True)
            theme_map = {}

    print(f"[us_upside] 跑分類…", flush=True)
    breakout = []
    acceleration = []
    squeeze = []
    narrative = []
    # H2 修正: narrative_leader 必須是「前三類抓不到」的補集.
    # 先跑前三類, 收集 hit symbols, narrative 階段過濾掉.
    classified_syms: set = set()
    for f in features:
        sym = f["symbol"]
        td = theme_map.get(sym)
        hit_count = 0
        if (p := _check_breakout(f, td)):
            breakout.append(p)
            hit_count += 1
        if (p := _check_acceleration(f, td)):
            acceleration.append(p)
            hit_count += 1
        if (p := _check_squeeze_setup(f, td)):
            squeeze.append(p)
            hit_count += 1
        if hit_count > 0:
            classified_syms.add(sym)

    # narrative_leader: 只收前三類沒抓到的
    if with_themes:
        for f in features:
            sym = f["symbol"]
            if sym in classified_syms:
                continue  # H2: 已被分到其他類, 不重複入榜
            td = theme_map.get(sym)
            if td and (p := _check_narrative_leader(f, td)):
                narrative.append(p)

    breakout.sort(key=lambda x: x["score"], reverse=True)
    acceleration.sort(key=lambda x: x["score"], reverse=True)
    squeeze.sort(key=lambda x: x["score"], reverse=True)
    narrative.sort(key=lambda x: x["score"], reverse=True)

    # 合併去重
    by_sym: Dict[str, Dict] = {}
    for p in breakout + acceleration + squeeze + narrative:
        sym = p["symbol"]
        if sym not in by_sym or p["score"] > by_sym[sym]["score"]:
            by_sym[sym] = p
    all_picks = sorted(by_sym.values(), key=lambda x: x["score"], reverse=True)

    import datetime as dt
    return {
        "breakout": breakout,
        "acceleration": acceleration,
        "squeeze_setup": squeeze,
        "narrative_leader": narrative,
        "all": all_picks[:50],
        "meta": {
            "scanned": len(features),
            "universe_size": len(symbols),
            "data_date": dt.date.today().strftime("%Y-%m-%d"),
            "breakout_count": len(breakout),
            "acceleration_count": len(acceleration),
            "squeeze_count": len(squeeze),
            "narrative_count": len(narrative),
            "themes_loaded": len(theme_map),
        }
    }


# ---------------------------------------------------------------------------
# 格式化輸出
# ---------------------------------------------------------------------------
CATEGORY_LABEL_US = {
    "breakout": "52w 突破",
    "acceleration": "動能加速",
    "squeeze_setup": "壓縮待噴",
    "narrative_leader": "題材領跑",  # 新增第 4 類 (純題材但有技術 confirmation)
}


def fmt_summary_md(result: Dict, per_category: int = 5) -> str:
    """Markdown 摘要."""
    lines = ["# 🚀 美股潛在爆發股清單"]
    meta = result.get("meta", {})
    lines.append(f"_掃描 {meta.get('scanned', '?')}/{meta.get('universe_size', '?')} 檔 · 資料日 {meta.get('data_date')}_\n")

    for key in ("breakout", "acceleration", "squeeze_setup"):
        label = CATEGORY_LABEL_US.get(key, key)
        picks = (result.get(key) or [])[:per_category]
        lines.append(f"\n## {label} (共 {len(result.get(key) or [])} 檔)")
        if not picks:
            lines.append("_(無符合標的)_")
            continue
        for i, p in enumerate(picks, 1):
            lv = p.get("levels") or {}
            m = p.get("metrics") or {}
            lines.append(f"\n**{i}. {p['symbol']}** · 分數 {p['score']} · 空間 ~{p['upside_pct']}%")
            lines.append(f"- 現價 ${p['current']} · 進場 ${lv.get('entry_low')}~${lv.get('entry_high')} · 目標 ${lv.get('target')} · 停損 ${lv.get('stop')} · R:R {lv.get('rr')}")
            lines.append(f"- RSI {m.get('rsi')} · RVOL {m.get('rvol')}x · 距 ATH {m.get('pct_from_ath')}% · RS vs SPY {m.get('rs_vs_spy')}")
            for r in p.get("reasons", [])[:4]:
                lines.append(f"  - ✓ {r}")
            for w in p.get("warnings", [])[:2]:
                lines.append(f"  - ⚠ {w}")
    lines.append("\n_※ 本清單為演算法產出, 不構成投資建議_")
    return "\n".join(lines)
