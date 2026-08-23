"""
entry_timing.py
進場時機精準化 — 給推播 / actionable_picks 使用.

3 種進場模式 (根據當下價 vs 5MA / 前高 / Fib 判斷):
  1. 今日盤中進 (突破已成立 + 量價配合, 不等)
  2. 等回測 X 點再進 (給 5MA / Fib 0.382 / 前高 retest 價位)
  3. 等突破 Y 點才進 (尚未突破阻力, 確認再追)

API:
  determine_entry_mode(stock_id, market="auto") -> Dict
    {
      "mode": "buy_now" | "wait_pullback" | "wait_breakout",
      "label": "今日盤中進" | "等回測 245 接" | "等突破 268 追",
      "current": 252.5,
      "trigger_price": 245.0,
      "explanation": "近 60d 高 268, 突破才追",
      "confidence": "high" | "medium" | "low"
    }
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, Optional

import pandas as pd

import data_sources as ds


def _detect_market(stock_id: str) -> str:
    sid = str(stock_id).strip().upper()
    if sid.isdigit() and len(sid) >= 4:
        return "TW"
    if len(sid) == 5 and sid[:4].isdigit() and sid[4].isalpha():
        return "TW"
    return "US"


def determine_entry_mode(stock_id: str, market: str = "auto") -> Dict:
    """根據當下價 vs 5MA / 前高 / Fib 判進場模式.

    回:
      mode: buy_now / wait_pullback / wait_breakout
      label: 給用戶看的一句話
      current: 當前價
      trigger_price: 該模式的觸發價 (buy_now 無)
      explanation: 邏輯說明
      confidence: high / medium / low
    """
    out = {
        "mode": "—", "label": "—", "current": None,
        "trigger_price": None, "explanation": "", "confidence": "low"
    }
    if market == "auto":
        market = _detect_market(stock_id)

    sym = f"{stock_id}.TW" if market == "TW" else stock_id
    try:
        df = ds.fetch_yf_history(sym, period="6mo", interval="1d")
        if df is None or df.empty or len(df) < 30:
            # 嘗試 TWO 上櫃
            if market == "TW":
                df = ds.fetch_yf_history(f"{stock_id}.TWO", period="6mo", interval="1d")
            if df is None or df.empty or len(df) < 30:
                return out
        # Bug fix (2026-08): 資料新鮮度檢查 — 這支是「今日該不該買」的即時判斷,
        # 如果 yfinance 剛好卡住回舊資料 (rate limit / 台股常見延遲, 見
        # index_alerts.py._fetch_systemic_snapshot 的同類註解), 沒檢查會照樣
        # 算出一個「今日盤中進 (高信心)」, 使用者看不出這是根據過期價格算的。
        # 跟 index_alerts.py 同一套「差距 >= 2 天視為過期」門檻, "今天" 用 TPE
        # 校正過的日期 (不是 dt.date.today() 的 UTC 日期, 見 holiday_check.py)。
        date_col = "Date" if "Date" in df.columns else df.columns[0]
        try:
            latest_d = pd.to_datetime(df[date_col]).dt.date.iloc[-1]
        except Exception:
            latest_d = None
        today_tpe = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
        stale = latest_d is not None and (today_tpe - latest_d).days >= 2

        def _finalize(o: Dict) -> Dict:
            """過期資料算出來的結論一律降到低信心 + 標註, 不當作正常結果直接顯示."""
            if stale:
                o["confidence"] = "low"
                o["explanation"] = f"⚠️ 資料可能過期 (最新 {latest_d}) — {o['explanation']}"
            return o

        c = df["Close"].astype(float).reset_index(drop=True)
        h = df["High"].astype(float).reset_index(drop=True)
        l = df["Low"].astype(float).reset_index(drop=True)
        v = df["Volume"].astype(float).reset_index(drop=True)

        cur = float(c.iloc[-1])
        ma5 = float(c.tail(5).mean())
        ma10 = float(c.tail(10).mean())
        ma20 = float(c.tail(20).mean())
        high_20d = float(h.tail(20).max())
        high_60d = float(h.tail(60).max())
        low_20d = float(l.tail(20).min())
        avg_vol = float(v.tail(20).mean())
        recent_vol = float(v.iloc[-1])

        out["current"] = round(cur, 2)

        # Fib 0.382 retracement (from low_20d to high_20d)
        fib_382 = high_20d - (high_20d - low_20d) * 0.382

        # ============================================================
        # 判定邏輯 (優先序: 已突破 > 等回測 > 等突破)
        # ============================================================

        # 1. 「今日盤中進」: 當下價 已突破 20d 高 且 量增
        if cur >= high_20d * 0.99 and cur >= ma5 and recent_vol >= avg_vol * 1.3:
            out["mode"] = "buy_now"
            out["label"] = "今日盤中進"
            out["trigger_price"] = round(cur, 2)
            out["explanation"] = f"突破 20d 高 ({high_20d:.2f}) + 量增 ({recent_vol/avg_vol:.1f}x), 趨勢確認"
            out["confidence"] = "high"
            return _finalize(out)

        # 2. 「等回測 X 接」: 當下價 在 高位區域 (≥ 5MA 之上 5%+) 但已偏離
        if cur > ma5 * 1.04 and cur >= high_20d * 0.95:
            # 拉回 5MA 或 Fib 0.382, 取較高者 (更近)
            pullback_price = max(ma5, fib_382)
            out["mode"] = "wait_pullback"
            out["label"] = f"等回測 {pullback_price:.2f} 接"
            out["trigger_price"] = round(pullback_price, 2)
            distance = (cur / pullback_price - 1) * 100
            out["explanation"] = f"當前 {cur:.2f} 偏離 5MA ({ma5:.2f}) {distance:.1f}%, 等回測買到較好價位"
            out["confidence"] = "medium"
            return _finalize(out)

        # 3. 「等突破 Y 追」: 當下價 還在 20d 中段, 沒突破
        if cur < high_20d * 0.97:
            out["mode"] = "wait_breakout"
            out["label"] = f"等突破 {high_20d:.2f} 追"
            out["trigger_price"] = round(high_20d, 2)
            distance = (high_20d / cur - 1) * 100
            out["explanation"] = f"近 20d 高 {high_20d:.2f} (距離 +{distance:.1f}%), 突破才追, 防假突破"
            out["confidence"] = "medium"
            return _finalize(out)

        # 4. 中間區域 (高位但未明顯偏離) — 等 5MA 接
        if cur >= ma5:
            out["mode"] = "wait_pullback"
            out["label"] = f"等回測 {ma5:.2f} (5MA) 接"
            out["trigger_price"] = round(ma5, 2)
            out["explanation"] = f"當前 {cur:.2f} 在 5MA 附近, 拉回支撐買進"
            out["confidence"] = "low"
            return _finalize(out)

        # 5. fallback
        out["mode"] = "wait_breakout"
        out["label"] = f"等站回 5MA ({ma5:.2f}) 再評估"
        out["trigger_price"] = round(ma5, 2)
        out["explanation"] = f"當前 {cur:.2f} 跌破 5MA, 等回升再評估"
        out["confidence"] = "low"
        return _finalize(out)
    except Exception as e:
        print(f"[entry_timing] {stock_id} fail: {e}", flush=True)
        return out


def fmt_entry_mode(timing: Dict) -> str:
    """格式化單一 timing dict → 一行字 (推播 / dashboard 用)."""
    if not timing or timing.get("mode") == "—":
        return ""
    mode_emoji = {
        "buy_now": "🎯",
        "wait_pullback": "⏳",
        "wait_breakout": "🚪",
    }.get(timing.get("mode", ""), "💡")
    label = timing.get("label", "—")
    conf = timing.get("confidence", "")
    conf_tag = {"high": "(高信心)", "medium": "(中)", "low": "(低)"}.get(conf, "")
    return f"{mode_emoji} {label} {conf_tag}".strip()
