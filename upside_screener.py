"""
upside_screener.py
專門找「還有上漲空間」的潛力股, 分三類:

  1. 起漲初期 (early_stage)   — 還沒大漲, 籌碼轉好, 量縮後剛要放
  2. 動能繼續 (momentum)      — 已漲但動能未竭, 距 52w 高還有 15%+
  3. 反轉型 (reversal)        — 超賣反彈 + RSI 底背離 + 法人由賣轉買

設計重點:
  - 用 ATR 動態決定停損 / 目標 (修正 actionable_picks 固定 % 的問題)
  - 用 % from 52w high/low 過濾「太貴 / 太便宜」
  - 多訊號 cross-validation 提高勝率
  - 籌碼健康度為過濾 + 加分依據

對外接口:
    run_upside_screen(market='all', max_stocks=200) -> dict
        {
            'early_stage': List[Dict],   # 每筆: stock_id/name/score/reasons/levels
            'momentum':    List[Dict],
            'reversal':    List[Dict],
            'all':         List[Dict],   # 三類去重後合併
            'meta':        {'scanned': int, 'data_date': str}
        }

每筆 pick 結構:
    {
        'stock_id': '2330', 'name': '台積電', 'market': 'twse',
        'category': 'early_stage'|'momentum'|'reversal',
        'current': 1050.0,
        'score': 78,                  # 0-100 綜合分
        'upside_pct': 18.5,           # 預估上漲空間 (vs 52w high or target)
        'reasons': ['位於 52w 低 +12%', '投信 5d 連買', 'BB 壓縮 60d 新低'],
        'warnings': ['量能尚未明顯放大'],
        'levels': {
            'entry_low': 1040, 'entry_high': 1055,
            'target': 1150, 'stop': 1000, 'rr': 2.0, 'atr_pct': 1.8
        },
        'metrics': {
            '今日%': 1.2, '5日%': 3.5, '20日%': 4.1,
            'pct_from_52w_high': -18.5, 'pct_from_52w_low': 24.0,
            'rsi': 52.3, 'bb_width%': 4.2,
            'chip_health': 72, 'chip_consensus': 'bullish',
        }
    }
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import chip_analyzer
import data_sources as ds
import indicators as ind

# Streamlit cache (deploy 到網站時必須的, 避免每次互動都重打 ~300 個 FinMind API).
# 若在非 streamlit context (純 python script / 排程) 載入失敗就回 no-op 裝飾器.
try:
    import streamlit as st  # type: ignore
    _STREAMLIT_AVAILABLE = True
except Exception:
    _STREAMLIT_AVAILABLE = False
    class _NoOp:
        def cache_data(self, *args, **kwargs):
            def deco(f):
                return f
            # 支援 @st.cache_data 與 @st.cache_data(ttl=...) 兩種寫法
            if len(args) == 1 and callable(args[0]):
                return args[0]
            return deco
    st = _NoOp()  # type: ignore


# ---------------------------------------------------------------------------
# 設定 (可以後續抽到 config.py)
# ---------------------------------------------------------------------------
DEFAULT_MAX_STOCKS = 200

# 起漲初期參數
EARLY_PARAMS = {
    "max_pct_from_52w_low": 30.0,    # 離 52w 低點 ≤ 30% (未大漲)
    "min_pct_from_52w_low": 5.0,     # 但已脫離絕對底部 (避免接刀)
    "max_5d_pct": 8.0,                # 近 5 日漲幅 ≤ 8%
    "max_20d_pct": 15.0,              # 近 20 日漲幅 ≤ 15%
    "min_vol_expansion_ratio": 1.15,  # 5d vol / 20d vol ≥ 1.15
    "require_above_ma20": True,
    "require_bb_squeeze_or_chip_bullish": True,
    "min_chip_health": 55,
}

# 動能繼續參數
MOMENTUM_PARAMS = {
    "max_pct_from_52w_high": -8.0,   # 距 52w 高還有 ≥ 8% 空間
    "min_5d_pct": 3.0,                # 近 5 日漲幅 ≥ 3%
    "max_5d_pct": 15.0,               # 但不超過 15% (避免追頂)
    "min_today_pct": -1.0,            # 今天不能大跌
    "max_today_pct": 6.0,             # 今天也不能大噴 (避追高)
    "max_rsi": 75.0,                  # RSI ≤ 75 (未過熱)
    "min_rsi": 55.0,                  # RSI ≥ 55 (確認強勢)
    "require_ma_bullish_alignment": True,
    "min_chip_health": 60,
}

# 反轉型參數
REVERSAL_PARAMS = {
    "max_20d_pct": -8.0,              # 過去 20 日跌 ≥ 8% (已修正)
    "min_60d_pct": -40.0,             # 但不超過 -40% (避免基本面崩盤)
    "rsi_oversold_threshold": 35.0,
    "min_today_pct": 0.5,              # 今天必須收紅
    "min_vol_ratio_today": 1.3,        # 今天爆量 (止跌訊號)
    "require_rsi_divergence_or_bounce": True,
    "min_chip_consensus_score": 1,     # 至少 1 家法人轉買
}


# ---------------------------------------------------------------------------
# 抓取單檔的完整資料 (daily + chip)
# ---------------------------------------------------------------------------
def _fetch_one_full(stock_id: str, days_daily: int = 280) -> Dict:
    """抓單檔: daily (含足夠長度算 52w + ATR + RSI) + 籌碼 dict.
    days_daily 預設 280 = 約 1 年交易日 + 緩衝.
    M5: 在 return 中附上實際最新交易日 (last_date), 給 caller 算真正的 data_date.
    """
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days_daily)).strftime("%Y-%m-%d")
    daily = ds._finmind_get_one("TaiwanStockPrice", stock_id, start, end)
    chip = {}
    last_date = None
    if not daily.empty:
        if "max" in daily.columns and "high" not in daily.columns:
            daily = daily.rename(columns={"max": "high", "min": "low"})
        daily["date"] = pd.to_datetime(daily["date"])
        daily = daily.sort_values("date").reset_index(drop=True)
        try:
            last_date = daily["date"].max()
        except Exception:
            last_date = None
    try:
        chip = chip_analyzer.fetch_chip_data(stock_id, days=30) or {}
    except Exception:
        chip = {}
    return {"stock_id": stock_id, "daily": daily, "chip": chip, "last_date": last_date}


def _compute_features(stock_id: str, name: str, market: str,
                       daily: pd.DataFrame, chip: Dict) -> Optional[Dict]:
    """從 daily + chip 計算所有 features 給三類 screen 共用."""
    if daily is None or daily.empty or len(daily) < 30:
        return None
    try:
        c = daily["close"].astype(float)
        h = daily["high"].astype(float)
        l = daily["low"].astype(float)
        v = daily["Trading_Volume"].astype(float)
        cur = float(c.iloc[-1])
        prev = float(c.iloc[-2]) if len(c) >= 2 else cur

        # MA
        ma5 = c.rolling(5).mean().iloc[-1] if len(c) >= 5 else np.nan
        ma10 = c.rolling(10).mean().iloc[-1] if len(c) >= 10 else np.nan
        ma20 = c.rolling(20).mean().iloc[-1] if len(c) >= 20 else np.nan
        ma60 = c.rolling(60).mean().iloc[-1] if len(c) >= 60 else np.nan

        # 動能
        today_pct = (cur / prev - 1) * 100 if prev > 0 else 0
        five_pct = (cur / float(c.iloc[-6]) - 1) * 100 if len(c) >= 6 else None
        twenty_pct = (cur / float(c.iloc[-21]) - 1) * 100 if len(c) >= 21 else None
        sixty_pct = (cur / float(c.iloc[-61]) - 1) * 100 if len(c) >= 61 else None

        # 52w 位置
        hi52, lo52, pct_hi, pct_lo = ind.distance_from_52w(c, window=252)

        # 量能
        avg5_vol = float(v.iloc[-6:-1].mean()) if len(v) >= 6 else None
        # 修正: 原本 `cur and (...) if avg5_vol else None` 有布林短路怪味,
        # 直接判斷 avg5_vol 即可 (cur 已在前面確認 > 0).
        vol_ratio_today = (float(v.iloc[-1]) / avg5_vol) if (avg5_vol and avg5_vol > 0) else None
        vol_expand, vol_expand_ratio = ind.volume_expansion(v, recent=5, base=20, ratio_threshold=1.15)
        vol_dry, vol_dry_ratio = ind.volume_dryup(v, recent=5, base=20, ratio_threshold=0.7)

        # 指標
        rsi_s = ind.rsi(c, 14)
        rsi_now = float(rsi_s.iloc[-1]) if not rsi_s.empty else None
        atr_s = ind.atr(h, l, c, 14)
        atr_now = float(atr_s.iloc[-1]) if not atr_s.empty and not pd.isna(atr_s.iloc[-1]) else None
        bb_mid, bb_up, bb_lo, bb_w = ind.bollinger_bands(c, 20, 2.0)
        bb_w_now = float(bb_w.iloc[-1]) if not bb_w.empty and not pd.isna(bb_w.iloc[-1]) else None
        bb_squeeze = ind.is_bb_squeeze(bb_w, lookback=60, percentile=25)

        # 背離
        rsi_bot_div = ind.rsi_bottom_divergence(c, rsi_s, lookback=30, pivot_window=3)
        rsi_top_div = ind.rsi_top_divergence(c, rsi_s, lookback=30, pivot_window=3)

        # MA 多頭排列
        ma_bull = ind.ma_alignment_bullish(c, periods=(5, 10, 20, 60))

        # 籌碼
        derived = (chip or {}).get("derived") or {}
        chip_health = derived.get("籌碼健康度", 50)
        chip_consensus = derived.get("法人共識", {})
        short_margin_ratio = derived.get("短券資比%")

        # 法人連續買超天數 (投信)
        it = ((chip or {}).get("institutional") or {}).get("Investment_Trust") or {}
        it_consec = it.get("consecutive_days", 0)
        it_5d = it.get("5d_total", 0)
        fi = ((chip or {}).get("institutional") or {}).get("Foreign_Investor") or {}
        fi_consec = fi.get("consecutive_days", 0)
        fi_5d = fi.get("5d_total", 0)

        # ATR-based levels (修正 B8)
        levels = ind.atr_based_levels(h, l, c) or {}

        return {
            "stock_id": stock_id, "name": name, "market": market,
            "current": round(cur, 2),
            "ma5": _r(ma5), "ma10": _r(ma10), "ma20": _r(ma20), "ma60": _r(ma60),
            "today_pct": _r(today_pct, 2),
            "five_pct": _r(five_pct, 2),
            "twenty_pct": _r(twenty_pct, 2),
            "sixty_pct": _r(sixty_pct, 2),
            "hi52": _r(hi52, 2), "lo52": _r(lo52, 2),
            "pct_from_52w_high": _r(pct_hi, 2),
            "pct_from_52w_low": _r(pct_lo, 2),
            "vol_ratio_today": _r(vol_ratio_today, 2),
            "vol_expand": vol_expand, "vol_expand_ratio": vol_expand_ratio,
            "vol_dry": vol_dry, "vol_dry_ratio": vol_dry_ratio,
            "rsi": _r(rsi_now, 1),
            "atr": _r(atr_now, 3),
            "atr_pct": round(atr_now / cur * 100, 2) if atr_now and cur else None,
            "bb_width": _r(bb_w_now, 2),
            "bb_squeeze": bb_squeeze,
            "rsi_bottom_divergence": rsi_bot_div,
            "rsi_top_divergence": rsi_top_div,
            "ma_bullish_alignment": ma_bull,
            "chip_health": chip_health,
            "chip_consensus_direction": (chip_consensus or {}).get("direction", "neutral"),
            "chip_consensus_score": (chip_consensus or {}).get("score", 0),
            "short_margin_ratio": short_margin_ratio,
            "it_consecutive": it_consec, "it_5d_net": it_5d,
            "fi_consecutive": fi_consec, "fi_5d_net": fi_5d,
            "levels": levels,
        }
    except Exception as e:
        print(f"[upside_screener] features failed for {stock_id}: {e}", flush=True)
        return None


def _r(v, n: int = 2):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return None
    return round(float(v), n)


# ---------------------------------------------------------------------------
# 三類篩選 + 評分
# ---------------------------------------------------------------------------
def _check_early_stage(f: Dict) -> Optional[Dict]:
    """起漲初期: 還沒漲、籌碼轉好、量能開始累積."""
    p = EARLY_PARAMS
    pct_lo = f.get("pct_from_52w_low")
    if pct_lo is None or pct_lo > p["max_pct_from_52w_low"] or pct_lo < p["min_pct_from_52w_low"]:
        return None
    if (f.get("five_pct") or 0) > p["max_5d_pct"]:
        return None
    if (f.get("twenty_pct") or 0) > p["max_20d_pct"]:
        return None
    if p["require_above_ma20"]:
        if not (f.get("current") and f.get("ma20") and f["current"] > f["ma20"]):
            return None
    if (f.get("chip_health") or 0) < p["min_chip_health"]:
        return None

    reasons = []
    warnings = []
    score = 30  # 基準

    reasons.append(f"距 52w 低 +{pct_lo:.1f}% (未大漲)")
    if f.get("pct_from_52w_high") and f["pct_from_52w_high"] <= -20:
        reasons.append(f"距 52w 高還有 {-f['pct_from_52w_high']:.0f}% 空間")
        score += 15

    if f.get("bb_squeeze"):
        reasons.append("BB 寬度近 60 日新低 (波動率壓縮)")
        score += 15
    if f.get("vol_dry") and f.get("vol_dry_ratio") and f["vol_dry_ratio"] <= 0.75:
        reasons.append(f"近 5 日量縮 {f['vol_dry_ratio']:.0%} (籌碼沉澱)")
        score += 10
    if f.get("vol_expand"):
        reasons.append(f"量能開始放大 ({f.get('vol_expand_ratio', '?')}x)")
        score += 10
    if (f.get("chip_health") or 0) >= 70:
        reasons.append(f"籌碼健康度 {f['chip_health']}/100")
        score += 15
    if f.get("chip_consensus_direction") == "bullish" and (f.get("chip_consensus_score") or 0) >= 2:
        reasons.append(f"法人共識買 ({f['chip_consensus_score']}/3)")
        score += 10
    if (f.get("it_consecutive") or 0) >= 3:
        reasons.append(f"投信連 {f['it_consecutive']} 天買")
        score += 5
    if (f.get("rsi") or 50) <= 55:
        reasons.append(f"RSI {f['rsi']:.0f} (未過熱)")
        score += 5

    if not f.get("vol_expand"):
        warnings.append("量能尚未明顯放大")
    if (f.get("today_pct") or 0) < 0:
        warnings.append(f"今日小跌 {f['today_pct']:.2f}%")

    # 起漲初期至少要有「籌碼轉好」或「BB 壓縮」其中之一才算 setup 成熟
    if p["require_bb_squeeze_or_chip_bullish"]:
        if not (f.get("bb_squeeze") or (f.get("chip_consensus_direction") == "bullish")):
            return None

    upside_pct = abs(f.get("pct_from_52w_high") or 15)
    return _build_pick(f, "early_stage", score, reasons, warnings, upside_pct)


def _check_momentum(f: Dict) -> Optional[Dict]:
    """動能繼續: 已漲但動能未竭, 距 52w 高還有空間."""
    p = MOMENTUM_PARAMS
    pct_hi = f.get("pct_from_52w_high")
    if pct_hi is None or pct_hi > p["max_pct_from_52w_high"]:
        return None
    fp = f.get("five_pct")
    if fp is None or fp < p["min_5d_pct"] or fp > p["max_5d_pct"]:
        return None
    tp = f.get("today_pct")
    if tp is None or tp < p["min_today_pct"] or tp > p["max_today_pct"]:
        return None
    rsi_v = f.get("rsi")
    if rsi_v is None or rsi_v > p["max_rsi"] or rsi_v < p["min_rsi"]:
        return None
    if p["require_ma_bullish_alignment"] and not f.get("ma_bullish_alignment"):
        return None
    if (f.get("chip_health") or 0) < p["min_chip_health"]:
        return None

    reasons = []
    warnings = []
    score = 35

    reasons.append(f"距 52w 高還有 {-pct_hi:.1f}% 空間")
    reasons.append(f"5 日 +{fp:.1f}% (動能未竭)")
    if f.get("ma_bullish_alignment"):
        reasons.append("MA 5/10/20/60 多頭排列")
        score += 15
    if (f.get("twenty_pct") or 0) >= 5:
        reasons.append(f"20 日 +{f['twenty_pct']:.1f}%")
        score += 5
    if (f.get("chip_health") or 0) >= 65:
        reasons.append(f"籌碼健康 {f['chip_health']}/100")
        score += 15
    if (f.get("fi_consecutive") or 0) >= 3:
        reasons.append(f"外資連 {f['fi_consecutive']} 天買")
        score += 10
    if (f.get("it_consecutive") or 0) >= 3:
        reasons.append(f"投信連 {f['it_consecutive']} 天買")
        score += 10
    vrt = f.get("vol_ratio_today") or 0
    if 1.2 <= vrt <= 2.5:
        reasons.append(f"量比 {vrt:.1f}x (健康放量)")
        score += 8
    elif vrt > 3:
        warnings.append(f"量比 {vrt:.1f}x 過高, 留意短線過熱")

    if f.get("rsi_top_divergence"):
        warnings.append("RSI 頂背離, 動能可能轉弱")
        score -= 15
    if f.get("short_margin_ratio") and f["short_margin_ratio"] >= 25:
        reasons.append(f"券資比 {f['short_margin_ratio']:.0f}% (軋空潛能)")
        score += 8

    upside_pct = abs(pct_hi)
    return _build_pick(f, "momentum", score, reasons, warnings, upside_pct)


def _check_reversal(f: Dict) -> Optional[Dict]:
    """反轉型: 超賣反彈 + 底背離 + 法人轉買."""
    p = REVERSAL_PARAMS
    tp20 = f.get("twenty_pct")
    if tp20 is None or tp20 > p["max_20d_pct"]:
        return None
    sp = f.get("sixty_pct")
    if sp is not None and sp < p["min_60d_pct"]:
        return None  # 跌太多, 可能基本面問題
    if (f.get("today_pct") or 0) < p["min_today_pct"]:
        return None
    if (f.get("vol_ratio_today") or 0) < p["min_vol_ratio_today"]:
        return None
    cons_score = f.get("chip_consensus_score") or 0
    if f.get("chip_consensus_direction") != "bullish" and cons_score < p["min_chip_consensus_score"]:
        # 至少要有 1 家法人轉買 (寬鬆條件)
        if (f.get("it_5d_net") or 0) <= 0 and (f.get("fi_5d_net") or 0) <= 0:
            return None

    reasons = []
    warnings = []
    score = 30

    reasons.append(f"20 日跌 {tp20:.1f}% (修正完成)")
    reasons.append(f"今日 +{f['today_pct']:.2f}% 收紅且爆量 {f['vol_ratio_today']:.1f}x")
    if f.get("rsi_bottom_divergence"):
        reasons.append("RSI 底背離 (下跌動能耗盡)")
        score += 25
    elif (f.get("rsi") or 50) <= 40:
        reasons.append(f"RSI {f['rsi']:.0f} 超賣區回升")
        score += 10
    if (f.get("it_5d_net") or 0) > 0:
        reasons.append(f"投信 5d 由賣轉買 +{f['it_5d_net']:,}")
        score += 15
    if (f.get("fi_5d_net") or 0) > 0:
        reasons.append(f"外資 5d 由賣轉買 +{f['fi_5d_net']:,}")
        score += 15
    if f.get("current") and f.get("ma20") and f["current"] > f["ma20"]:
        reasons.append("已重新站上 MA20")
        score += 10

    # 警示: 還在空頭排列 (反轉初期常見)
    if not f.get("ma_bullish_alignment"):
        warnings.append("MA 仍空頭排列, 反轉確認需 1-2 週")
    if sp is not None and sp <= -25:
        warnings.append(f"60 日跌 {sp:.0f}%, 大幅修正需留意基本面")

    # 反轉型上漲空間 = 從近期低點回升的潛在空間 (用 60d 跌幅推估)
    upside_pct = abs(tp20) * 0.6  # 反彈通常回補修正幅度的 50-80%
    return _build_pick(f, "reversal", score, reasons, warnings, upside_pct)


def _build_pick(f: Dict, category: str, score: int,
                 reasons: List[str], warnings: List[str], upside_pct: float) -> Dict:
    """組裝統一輸出格式."""
    score = max(0, min(100, int(score)))
    metrics = {
        "今日%": f.get("today_pct"), "5日%": f.get("five_pct"),
        "20日%": f.get("twenty_pct"), "60日%": f.get("sixty_pct"),
        "pct_from_52w_high": f.get("pct_from_52w_high"),
        "pct_from_52w_low": f.get("pct_from_52w_low"),
        "rsi": f.get("rsi"),
        "bb_width%": f.get("bb_width"),
        "bb_squeeze": f.get("bb_squeeze"),
        "atr_pct": f.get("atr_pct"),
        "vol_ratio_today": f.get("vol_ratio_today"),
        "chip_health": f.get("chip_health"),
        "chip_consensus": f.get("chip_consensus_direction"),
        "ma_bullish_alignment": f.get("ma_bullish_alignment"),
    }
    return {
        "stock_id": f["stock_id"], "name": f["name"], "market": f["market"],
        "category": category,
        "current": f["current"],
        "score": score,
        "upside_pct": round(float(upside_pct), 1),
        "reasons": reasons,
        "warnings": warnings,
        "levels": f.get("levels") or {},
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# 對外接口
# ---------------------------------------------------------------------------
# Cache TTL: 15 分鐘. 太長會用到舊資料, 太短失去 cache 意義 (FinMind 日線 1 天才更新一次).
# 排程跑 (午盤 / 收盤後) 跟 dashboard 互動共用一份結果, 大幅降低 FinMind quota 消耗.
_UPSIDE_CACHE_TTL = 15 * 60


@st.cache_data(ttl=_UPSIDE_CACHE_TTL, show_spinner=False)
def _cached_upside_screen(market: str, max_stocks: int, exclude_etf: bool,
                            max_workers: int) -> Dict:
    """Streamlit-cached 版本 — 在 Streamlit context 內被 dashboard 使用.
    參數都是 hashable (str/int/bool), 才能正確 cache.
    progress_cb 不能 cache, 所以由 wrapper run_upside_screen 處理.
    """
    return _run_upside_screen_impl(
        market=market, max_stocks=max_stocks,
        max_workers=max_workers, progress_cb=None,
        exclude_etf=exclude_etf,
    )


def run_upside_screen(market: str = "all", max_stocks: int = DEFAULT_MAX_STOCKS,
                       max_workers: int = 5, progress_cb=None,
                       exclude_etf: bool = True, use_cache: bool = True) -> Dict:
    """對外主接口.

    use_cache=True (預設) — 走 @st.cache_data 包裝的版本 (網站部署必用).
                            注意: progress_cb 在 cache hit 時不會被觸發.
    use_cache=False         — 直接跑 (CLI / 排程 / 想看實時進度).

    其他參數: 見 _run_upside_screen_impl.
    """
    if use_cache and progress_cb is None:
        return _cached_upside_screen(market, max_stocks, exclude_etf, max_workers)
    # progress_cb 需要 / 明確不要 cache → 走直接 impl
    return _run_upside_screen_impl(
        market=market, max_stocks=max_stocks,
        max_workers=max_workers, progress_cb=progress_cb,
        exclude_etf=exclude_etf,
    )


def _run_upside_screen_impl(market: str = "all", max_stocks: int = DEFAULT_MAX_STOCKS,
                              max_workers: int = 5, progress_cb=None,
                              exclude_etf: bool = True) -> Dict:
    """掃描 max_stocks 檔台股, 分三類輸出潛在上漲標的.

    market: 'twse' | 'tpex' | 'all'
    progress_cb(stage:str, pct:int): 進度回呼.
    """
    def _p(stage, pct):
        if progress_cb:
            try:
                progress_cb(stage, pct)
            except Exception:
                pass

    _p("取得台股清單…", 5)
    info = ds.get_taiwan_stock_info()
    if info.empty:
        return {"early_stage": [], "momentum": [], "reversal": [], "all": [],
                "meta": {"scanned": 0, "error": "info empty"}}
    info = info.drop_duplicates(subset=["stock_id"], keep="first")
    info = ds.filter_tradeable_stocks(info, exclude_etf=exclude_etf)
    if market == "twse":
        info = info[info["type"] == "twse"]
    elif market == "tpex":
        info = info[info["type"] == "tpex"]

    universe = info["stock_id"].head(max_stocks).tolist()
    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}
    market_map = info.set_index("stock_id")["type"].to_dict() if "type" in info.columns else {}

    _p(f"並行抓取 {len(universe)} 檔資料…", 15)
    features_list: List[Dict] = []
    done = 0
    total = len(universe)
    last_dates: List = []  # M5: 收集每檔的最新日期, 用來算 data_date
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_one_full, sid): sid for sid in universe}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                d = fut.result()
            except Exception:
                d = None
            if d:
                if d.get("last_date") is not None:
                    last_dates.append(d["last_date"])
                feat = _compute_features(
                    sid, name_map.get(sid, ""), market_map.get(sid, ""),
                    d.get("daily"), d.get("chip"),
                )
                if feat:
                    features_list.append(feat)
            done += 1
            if done % 20 == 0:
                _p(f"已掃 {done}/{total}…", 15 + int(done / total * 70))

    _p("分類評分…", 90)
    early, momentum, reversal = [], [], []
    for f in features_list:
        # 一檔股票可能同時符合多類, 但實務上多為其中一類. 仍允許多類入列.
        e = _check_early_stage(f)
        if e:
            early.append(e)
        m = _check_momentum(f)
        if m:
            momentum.append(m)
        r = _check_reversal(f)
        if r:
            reversal.append(r)

    early.sort(key=lambda x: x["score"], reverse=True)
    momentum.sort(key=lambda x: x["score"], reverse=True)
    reversal.sort(key=lambda x: x["score"], reverse=True)

    # 合併 (去重 by stock_id, 保留 score 最高的)
    by_sid: Dict[str, Dict] = {}
    for p in early + momentum + reversal:
        sid = p["stock_id"]
        if sid not in by_sid or p["score"] > by_sid[sid]["score"]:
            by_sid[sid] = p
    all_picks = sorted(by_sid.values(), key=lambda x: x["score"], reverse=True)

    # M5 修正: data_date 用「universe 中最常見的最新日」, 而非死硬寫今天.
    # 多數股當天有更新 → mode(last_dates) = 今天的交易日; 假日跑 = 上個交易日.
    data_date = None
    if last_dates:
        try:
            import collections
            most_common = collections.Counter(
                pd.Timestamp(d).date() for d in last_dates
            ).most_common(1)
            if most_common:
                data_date = most_common[0][0].strftime("%Y-%m-%d")
        except Exception:
            pass
    if data_date is None:
        data_date = dt.date.today().strftime("%Y-%m-%d")

    _p("完成!", 100)
    return {
        "early_stage": early[:30],
        "momentum": momentum[:30],
        "reversal": reversal[:30],
        "all": all_picks[:50],
        "meta": {
            "scanned": len(features_list),
            "universe_size": total,
            "data_date": data_date,
            "early_count": len(early),
            "momentum_count": len(momentum),
            "reversal_count": len(reversal),
        },
    }


# ---------------------------------------------------------------------------
# 格式化輸出 (給 TG / Streamlit 用)
# ---------------------------------------------------------------------------
CATEGORY_LABEL = {
    "early_stage": "起漲初期",
    "momentum":    "動能繼續",
    "reversal":    "反轉型",
}


def fmt_pick_md(p: Dict) -> str:
    """單檔 markdown 卡片."""
    lv = p.get("levels") or {}
    m = p.get("metrics") or {}
    lines = [
        f"### {p['stock_id']} {p.get('name', '')}  [{CATEGORY_LABEL.get(p['category'], p['category'])}]",
        f"**現價** {p.get('current')} · **分數** {p.get('score')}/100 · **上漲空間** ~{p.get('upside_pct')}%",
    ]
    if lv.get("entry_low"):
        lines.append(f"- **進場** {lv['entry_low']}~{lv['entry_high']} | **目標** {lv.get('target')} | **停損** {lv.get('stop')} | R:R **{lv.get('rr')}** (ATR {lv.get('atr_pct')}%)")
    if m.get("pct_from_52w_high") is not None:
        lines.append(f"- 52w 位置: 距高 {m['pct_from_52w_high']:.1f}% / 距低 +{m.get('pct_from_52w_low', 0):.1f}%")
    lines.append(f"- RSI {m.get('rsi')} | 量比 {m.get('vol_ratio_today')}x | 籌碼健康 {m.get('chip_health')}/100 ({m.get('chip_consensus', '?')})")
    for r in p.get("reasons", [])[:5]:
        lines.append(f"  ✓ {r}")
    for w in p.get("warnings", [])[:2]:
        lines.append(f"  ⚠ {w}")
    return "\n".join(lines)


def fmt_summary_tg(result: Dict, per_category: int = 3) -> str:
    """TG HTML 摘要 — 三類各取前 N 檔."""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)

    lines = ["<b>🌱 上漲潛力股清單</b>"]
    meta = result.get("meta", {})
    lines.append(f"<i>掃描 {meta.get('scanned', '?')} 檔 · 資料日 {meta.get('data_date', '?')}</i>")
    lines.append("")

    for key in ("early_stage", "momentum", "reversal"):
        picks = (result.get(key) or [])[:per_category]
        label = CATEGORY_LABEL.get(key, key)
        if not picks:
            lines.append(f"<b>【{label}】無符合標的</b>")
            lines.append("")
            continue
        lines.append(f"<b>【{label}】(共 {len(result.get(key) or [])} 檔, 顯示前 {len(picks)})</b>")
        for i, p in enumerate(picks, 1):
            lv = p.get("levels") or {}
            m = p.get("metrics") or {}
            lines.append(
                f"{i}. <b>{_esc(p['stock_id'])} {_esc(p.get('name', ''))}</b> · "
                f"分數 {p.get('score')} · 空間 ~{p.get('upside_pct')}%"
            )
            lines.append(
                f"   現價 {_esc(p.get('current'))} · 進 {_esc(lv.get('entry_low'))}~{_esc(lv.get('entry_high'))} · "
                f"目 {_esc(lv.get('target'))} · 損 {_esc(lv.get('stop'))} (R:R {_esc(lv.get('rr'))})"
            )
            for r in p.get("reasons", [])[:3]:
                lines.append(f"   ✓ {_esc(r)}")
            if p.get("warnings"):
                lines.append(f"   ⚠ {_esc(p['warnings'][0])}")
        lines.append("")
    lines.append("<i>※ 本清單為演算法產出, 不構成投資建議</i>")
    return "\n".join(lines).rstrip()
