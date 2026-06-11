"""
rsi_divergence.py — RSI 背離偵測

「頂背離 (Bearish Divergence)」: 股價創新高, 但 RSI 不破前高 → 動能衰竭, 賣訊
「底背離 (Bullish Divergence)」: 股價創新低, 但 RSI 不破前低 → 反彈訊號

掃描方法:
  1. 找近 N 日的兩個 swing high (或 swing low) - 局部峰/谷
  2. 比較對應位置的 RSI 值
  3. 若價格趨勢與 RSI 趨勢相反 → 背離

API:
  detect_divergence(symbol, market="TW", lookback=60) -> Dict
  scan_holdings_for_divergence() -> List[Dict]  # 對持倉掃一次
  fmt_divergence_alert(divergences: List[Dict]) -> str  # TG 訊息
"""
from __future__ import annotations

from typing import Dict, List, Optional


# 設定
LOOKBACK = 60            # 看近 N 日
SWING_WINDOW = 5         # swing high/low 確認窗口 (左右各 5 根)
RSI_PERIOD = 14
MIN_PRICE_DIFF_PCT = 1.0  # 兩個 swing 至少價差 ≥ 1% 才算數
MIN_RSI_DIFF = 2.0        # RSI 至少差 2 以上才視為「明顯背離」


def _compute_rsi(closes, period: int = RSI_PERIOD):
    """Wilder RSI."""
    try:
        delta = closes.diff()
        up = delta.clip(lower=0)
        dn = -delta.clip(upper=0)
        roll_up = up.ewm(alpha=1/period, min_periods=period).mean()
        roll_dn = dn.ewm(alpha=1/period, min_periods=period).mean()
        rs = roll_up / roll_dn.replace(0, 0.0001)
        return 100 - (100 / (1 + rs))
    except Exception:
        return None


def _find_swing_points(series, window: int = SWING_WINDOW, high: bool = True) -> List[int]:
    """找局部峰 (high=True) 或局部谷 (high=False).

    定義: index i 是 swing high 若 series[i] 比左右 window 根都高.
    回傳 index list (相對 series 開頭).
    """
    n = len(series)
    swings = []
    vals = series.tolist() if hasattr(series, 'tolist') else list(series)
    for i in range(window, n - window):
        v = vals[i]
        left = vals[i-window:i]
        right = vals[i+1:i+1+window]
        if high:
            if v == max([v] + left + right):
                swings.append(i)
        else:
            if v == min([v] + left + right):
                swings.append(i)
    return swings


def detect_divergence(symbol: str, market: str = "TW", lookback: int = LOOKBACK) -> Dict:
    """偵測 symbol 的 RSI 背離.

    回傳:
      type: "bearish" / "bullish" / "none"
      strength: 0-3 (差距大強)
      detail: 文字說明
      cur_price / cur_rsi
    """
    out = {
        "symbol": symbol,
        "market": market,
        "type": "none",
        "strength": 0,
        "detail": "",
        "cur_price": None,
        "cur_rsi": None,
    }
    try:
        import data_sources as ds
        # 抓資料
        if market == "TW":
            df = None
            for sfx in [".TW", ".TWO"]:
                df = ds.fetch_yf_history(f"{symbol}{sfx}", period="6mo", interval="1d")
                if df is not None and not df.empty and len(df) >= lookback:
                    break
        else:
            df = ds.fetch_yf_history(symbol, period="6mo", interval="1d")
        if df is None or df.empty or len(df) < lookback:
            return out

        df = df.tail(lookback).reset_index(drop=True)
        closes = df["Close"].astype(float)
        highs = df["High"].astype(float)
        lows = df["Low"].astype(float)
        rsi = _compute_rsi(closes)
        if rsi is None:
            return out

        out["cur_price"] = round(float(closes.iloc[-1]), 2)
        out["cur_rsi"] = round(float(rsi.iloc[-1]), 1) if rsi.iloc[-1] == rsi.iloc[-1] else None

        # === 頂背離 ===
        # 找 highs 的 swing points (近期 2 個)
        sh_idx = _find_swing_points(highs, high=True)
        if len(sh_idx) >= 2:
            i1, i2 = sh_idx[-2], sh_idx[-1]
            p1, p2 = float(highs.iloc[i1]), float(highs.iloc[i2])
            r1 = float(rsi.iloc[i1]) if rsi.iloc[i1] == rsi.iloc[i1] else None
            r2 = float(rsi.iloc[i2]) if rsi.iloc[i2] == rsi.iloc[i2] else None
            if r1 is not None and r2 is not None:
                price_higher = p2 > p1 * (1 + MIN_PRICE_DIFF_PCT / 100)
                rsi_lower = r2 < r1 - MIN_RSI_DIFF
                if price_higher and rsi_lower:
                    strength = 1
                    if r1 - r2 >= 5: strength = 2
                    if r1 - r2 >= 10: strength = 3
                    out.update(
                        type="bearish",
                        strength=strength,
                        detail=(
                            f"股價新高 {p1:.2f}→{p2:.2f} (+{(p2/p1-1)*100:.2f}%), "
                            f"但 RSI {r1:.1f}→{r2:.1f} (-{r1-r2:.1f}) — 頂背離"
                        ),
                    )
                    return out

        # === 底背離 ===
        sl_idx = _find_swing_points(lows, high=False)
        if len(sl_idx) >= 2:
            i1, i2 = sl_idx[-2], sl_idx[-1]
            p1, p2 = float(lows.iloc[i1]), float(lows.iloc[i2])
            r1 = float(rsi.iloc[i1]) if rsi.iloc[i1] == rsi.iloc[i1] else None
            r2 = float(rsi.iloc[i2]) if rsi.iloc[i2] == rsi.iloc[i2] else None
            if r1 is not None and r2 is not None:
                price_lower = p2 < p1 * (1 - MIN_PRICE_DIFF_PCT / 100)
                rsi_higher = r2 > r1 + MIN_RSI_DIFF
                if price_lower and rsi_higher:
                    strength = 1
                    if r2 - r1 >= 5: strength = 2
                    if r2 - r1 >= 10: strength = 3
                    out.update(
                        type="bullish",
                        strength=strength,
                        detail=(
                            f"股價新低 {p1:.2f}→{p2:.2f} ({(p2/p1-1)*100:+.2f}%), "
                            f"但 RSI {r1:.1f}→{r2:.1f} (+{r2-r1:.1f}) — 底背離"
                        ),
                    )
                    return out
    except Exception as e:
        print(f"[rsi_div] {symbol} fail: {e}", flush=True)
    return out


def scan_holdings_for_divergence() -> List[Dict]:
    """對持倉每一檔, 偵測 RSI 背離. 回傳有背離的清單."""
    out = []
    try:
        import holdings_store
        holdings = holdings_store.load_holdings() or []
    except Exception:
        return out
    for h in holdings:
        sid = str(h.get("stock_id", "")).strip().upper()
        if not sid:
            continue
        mk = h.get("market", "TW" if sid.isdigit() else "US")
        d = detect_divergence(sid, mk)
        if d.get("type") in ("bearish", "bullish"):
            d["stock_name"] = h.get("stock_name", "")
            out.append(d)
    return out


def fmt_divergence_alert(divergences: List[Dict]) -> str:
    """組 TG 訊息. 只在有背離才呼叫."""
    if not divergences:
        return ""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)

    bear = [d for d in divergences if d.get("type") == "bearish"]
    bull = [d for d in divergences if d.get("type") == "bullish"]
    lines = ["📐 <b>RSI 背離警示</b>"]
    if bear:
        lines.append("")
        lines.append("🔻 <b>頂背離 (賣訊)</b> — 股價新高 RSI 未跟上, 動能衰竭")
        for d in bear:
            sid = _esc(d.get("symbol", ""))
            nm = _esc(d.get("stock_name", ""))
            star = "⭐" * d.get("strength", 1)
            lines.append(f"  <code>{sid}</code> {nm} {star}")
            lines.append(f"  <i>{_esc(d.get('detail', ''))}</i>")
    if bull:
        lines.append("")
        lines.append("🔺 <b>底背離 (買訊)</b> — 股價新低 RSI 未跟跌, 可能反彈")
        for d in bull:
            sid = _esc(d.get("symbol", ""))
            nm = _esc(d.get("stock_name", ""))
            star = "⭐" * d.get("strength", 1)
            lines.append(f"  <code>{sid}</code> {nm} {star}")
            lines.append(f"  <i>{_esc(d.get('detail', ''))}</i>")
    lines.append("")
    lines.append("<i>※ RSI 背離為輔助訊號, 需配合趨勢/量能判斷.</i>")
    return "\n".join(lines)
