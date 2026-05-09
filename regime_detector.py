"""
regime_detector.py
判斷當前大盤 regime: bull (多頭) / range (震盪) / bear (空頭).

讓「找強勢股」的訊號可以根據環境自動降溫. 空頭環境下:
  - actionable_picks 加 banner 警告, 並把 score 整體打折
  - 部位規模建議自動降到原本 50%
  - 推播訊息頂部加 ⚠ regime banner

判斷指標 (TW + US 各自算):
  TW (用 ^TWII):
    1. 加權 vs 5MA, 20MA, 60MA
    2. 5 日累積漲跌
    3. 5 日 ATR (波動率)
    4. (可選) VIX 替代品: TW VIX 不常用, 用美股 VIX 當參考

  US (用 ^GSPC / SPY):
    1. SPY vs 5MA, 20MA, 60MA
    2. VIX 絕對值 + 5d 變化
    3. SPY 5 日漲跌

加總成 score (-10 ~ +10):
  > +3  → bull
  -3 ~ +3 → range
  < -3  → bear

對外接口:
  detect_market_regime(market="TW") -> Dict
  get_position_size_multiplier(market="TW") -> float  (0.3 ~ 1.0)
  fmt_regime_banner(regime: Dict) -> str  (給推播首段用)
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, Optional

import pandas as pd

import data_sources as ds


def _vix_state() -> Optional[Dict]:
    """美股 VIX 狀態 (TW 也參考, 因為相關性高)."""
    try:
        df = ds.fetch_yf_history("^VIX", period="2mo", interval="1d")
        if df is None or df.empty or len(df) < 6:
            return None
        close = df["Close"].astype(float)
        vix_now = float(close.iloc[-1])
        vix_5d_ago = float(close.iloc[-6])
        vix_change_5d = (vix_now / vix_5d_ago - 1) * 100
        return {
            "vix": round(vix_now, 2),
            "vix_5d_ago": round(vix_5d_ago, 2),
            "change_5d_pct": round(vix_change_5d, 2),
        }
    except Exception:
        return None


def _index_state(symbol: str, label: str) -> Optional[Dict]:
    """單一指數 (^TWII / ^GSPC) 的 MA 與漲跌狀態."""
    try:
        df = ds.fetch_yf_history(symbol, period="6mo", interval="1d")
        if df is None or df.empty or len(df) < 65:
            return None
        close = df["Close"].astype(float)
        high = df["High"].astype(float)
        low = df["Low"].astype(float)

        last = float(close.iloc[-1])
        ma5 = float(close.tail(5).mean())
        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean())
        chg_5d = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0
        chg_20d = (last / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0

        # ATR(14) — 用來描述波動性, 也給 index_alerts 動態門檻用
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr14 = float(tr.tail(14).mean())

        return {
            "label": label,
            "symbol": symbol,
            "current": round(last, 2),
            "ma5": round(ma5, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "above_ma5": last > ma5,
            "above_ma20": last > ma20,
            "above_ma60": last > ma60,
            "ma5_above_ma20": ma5 > ma20,
            "ma20_above_ma60": ma20 > ma60,
            "chg_5d": round(chg_5d, 2),
            "chg_20d": round(chg_20d, 2),
            "atr14": round(atr14, 2),
        }
    except Exception as e:
        print(f"[regime_detector] _index_state {symbol} failed: {e}", flush=True)
        return None


def _score_regime(idx: Dict, vix: Optional[Dict] = None) -> tuple:
    """從 index_state + vix 算 regime score (-10 ~ +10) 跟分項貢獻."""
    score = 0.0
    contrib = []

    # MA 排列 — 多頭排列 +3, 空頭排列 -3, 混亂 0
    if idx["above_ma5"] and idx["above_ma20"] and idx["above_ma60"]:
        score += 3
        contrib.append("多頭排列 (站上 5/20/60 MA)")
    elif (not idx["above_ma5"]) and (not idx["above_ma20"]) and (not idx["above_ma60"]):
        score -= 3
        contrib.append("空頭排列 (跌破 5/20/60 MA)")
    elif idx["above_ma5"] and idx["above_ma20"]:
        score += 1
        contrib.append("短中期偏多 (站上 5/20 MA)")
    elif (not idx["above_ma5"]) and (not idx["above_ma20"]):
        score -= 1
        contrib.append("短中期偏空 (跌破 5/20 MA)")

    # MA 黃金 / 死亡交叉 (5 vs 20, 20 vs 60)
    if idx["ma5_above_ma20"] and idx["ma20_above_ma60"]:
        score += 1
    elif (not idx["ma5_above_ma20"]) and (not idx["ma20_above_ma60"]):
        score -= 1

    # 5d / 20d 漲跌
    chg5 = idx["chg_5d"]
    chg20 = idx["chg_20d"]
    if chg5 > 2:
        score += 1
        contrib.append(f"近 5 日 {chg5:+.1f}%")
    elif chg5 < -2:
        score -= 1.5
        contrib.append(f"近 5 日 {chg5:+.1f}%")

    if chg20 > 5:
        score += 1
    elif chg20 < -5:
        score -= 2
        contrib.append(f"近 20 日 {chg20:+.1f}% (中期承壓)")

    # VIX 影響 (對美股直接, 對台股是參考)
    if vix:
        v = vix.get("vix", 0)
        v_chg = vix.get("change_5d_pct", 0)
        if v < 15:
            score += 0.5
        elif v > 25:
            score -= 1.5
            contrib.append(f"VIX 高 ({v})")
        if v_chg > 30:  # VIX 5 日漲 30% → 恐慌升溫
            score -= 1
            contrib.append(f"VIX 急升 5d {v_chg:+.0f}%")

    return round(score, 2), contrib


def _classify(score: float) -> tuple:
    """score → regime label + 中文 + 進場建議.

    對稱: bull >= 4, bull_weak >= 2, range -2 ~ 2, bear_weak < -2, bear < -4.
    """
    if score >= 4:
        return "bull", "多頭", "可正常進場做多, 部位 100%"
    if score >= 2:
        return "bull_weak", "偏多 (謹慎)", "可進場但選擇性買, 部位 80%"
    if score >= -2:
        return "range", "震盪", "降低部位, 只買最強訊號 (R:R≥2.5), 部位 60%"
    if score > -4:
        return "bear_weak", "偏空 (警戒)", "暫停新進場, 觀察與管理現有持倉, 部位 30%"
    return "bear", "空頭", "暫停所有進場推薦, 只留警報與停損, 部位 0% (現金)"


def detect_market_regime(market: str = "TW") -> Dict:
    """偵測指定市場 regime.

    Returns:
        {
            "market": "TW",
            "regime": "bull" / "bull_weak" / "range" / "bear_weak" / "bear",
            "regime_label": "多頭",
            "score": 5.5,
            "guidance": "可正常進場做多, 部位 100%",
            "contrib": ["多頭排列", "近 5 日 +1.5%", ...],
            "index": {...},  # _index_state 內容
            "vix": {...},
            "position_multiplier": 1.0,  # 給 actionable_picks 降部位用
        }
    """
    if market.upper() == "US":
        idx = _index_state("^GSPC", "S&P 500")
    else:
        idx = _index_state("^TWII", "加權指數")
    if idx is None:
        return {
            "market": market,
            "regime": "unknown",
            "regime_label": "未知",
            "score": 0,
            "guidance": "資料不足, 建議手動判斷",
            "contrib": [],
            "index": None,
            "vix": None,
            "position_multiplier": 0.5,
        }

    vix = _vix_state()
    score, contrib = _score_regime(idx, vix)
    regime, label, guidance = _classify(score)

    # 部位倍數 — 給 actionable_picks / position_sizer 用
    multiplier_map = {
        "bull": 1.0, "bull_weak": 0.8, "range": 0.6,
        "bear_weak": 0.3, "bear": 0.0, "unknown": 0.5,
    }
    return {
        "market": market.upper(),
        "regime": regime,
        "regime_label": label,
        "score": score,
        "guidance": guidance,
        "contrib": contrib,
        "index": idx,
        "vix": vix,
        "position_multiplier": multiplier_map.get(regime, 0.5),
    }


def get_position_size_multiplier(market: str = "TW") -> float:
    """快速接口 — 只回 multiplier 給 position_sizer / actionable_picks."""
    try:
        return float(detect_market_regime(market).get("position_multiplier", 0.5))
    except Exception:
        return 0.5


def fmt_regime_banner(regime: Dict) -> str:
    """格式化 regime banner — 給推播首段, dashboard top 用. 簡短一段."""
    if not regime:
        return ""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)
    label = _esc(regime.get("regime_label", ""))
    score = regime.get("score", 0)
    guidance = _esc(regime.get("guidance", ""))
    market = _esc(regime.get("market", ""))
    icon_map = {
        "bull": "🟢", "bull_weak": "🟡", "range": "⚪",
        "bear_weak": "🟠", "bear": "🔴", "unknown": "❓",
    }
    icon = icon_map.get(regime.get("regime", ""), "")
    idx = regime.get("index") or {}
    cur = idx.get("current")
    ma20 = idx.get("ma20")
    detail_parts = []
    if cur is not None and ma20 is not None:
        diff_pct = (float(cur) / float(ma20) - 1) * 100
        detail_parts.append(f"{_esc(idx.get('label',''))} {cur} (vs 20MA {diff_pct:+.1f}%)")
    vix = regime.get("vix")
    if vix:
        detail_parts.append(f"VIX {vix.get('vix')}")
    detail = " · ".join(detail_parts)

    lines = [f"{icon} <b>{market} 大盤狀態: {label}</b> (score {score})"]
    if detail:
        lines.append(f"   {detail}")
    if guidance:
        lines.append(f"   建議: {guidance}")
    return "\n".join(lines)
