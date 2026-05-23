"""
gemini_target_estimator.py
Gemini-based fundamental + technical target price estimator.

Used by us_upside_screener as the "long-term target" alongside:
  - ATR-based short-term (1-4 wks)
  - Fibonacci / measured move mid-term (1-3 mos)
  - This module: long-term 3-6 mo target with basic fundamentals reasoning

Cost control:
  - Only called when breakout/acceleration score >= threshold
  - Cached per (symbol, date) for 24h to avoid burning quota
  - Batch up to 5 symbols per Gemini call to amortize cost
  - Returns None if Gemini unavailable / quota exceeded

Usage:
    estimator = GeminiTargetEstimator()
    targets = estimator.estimate_batch(["RKLB", "PLTR", "SMCI"])
    # {'RKLB': {'target': 145, 'confidence': 65, 'reasoning': '...'}, ...}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from typing import Dict, List, Optional

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


# In-process 24h cache (key = (symbol, date_str), value = (ts, target_dict))
_GEMINI_TARGET_CACHE: Dict[tuple, tuple] = {}
_GEMINI_CACHE_TTL = 24 * 3600
_GEMINI_CACHE_LOCK = threading.Lock()


def _cache_get(key: tuple):
    with _GEMINI_CACHE_LOCK:
        v = _GEMINI_TARGET_CACHE.get(key)
        if v is None:
            return None
        ts, data = v
        if time.time() - ts > _GEMINI_CACHE_TTL:
            _GEMINI_TARGET_CACHE.pop(key, None)
            return None
        return dict(data) if isinstance(data, dict) else data


def _cache_set(key: tuple, data):
    with _GEMINI_CACHE_LOCK:
        _GEMINI_TARGET_CACHE[key] = (time.time(), dict(data) if isinstance(data, dict) else data)
        if len(_GEMINI_TARGET_CACHE) > 200:
            oldest = sorted(_GEMINI_TARGET_CACHE.items(), key=lambda x: x[1][0])[:30]
            for k, _ in oldest:
                _GEMINI_TARGET_CACHE.pop(k, None)


def _gemini_available() -> bool:
    try:
        import ai_analyzer
        return ai_analyzer.gemini_available()
    except Exception:
        return False


def estimate_target(symbol: str, current_price: float,
                     features: Optional[Dict] = None,
                     theme: Optional[Dict] = None,
                     model: str = "gemini-2.5-flash") -> Optional[Dict]:
    """單檔長線目標估算. 失敗/沒 key/沒 quota 回 None.

    回傳 {target_3m, target_6m, confidence, reasoning, bull_target, bear_target}
    """
    today_str = dt.date.today().isoformat()
    key = (symbol.upper(), today_str)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    if not _gemini_available():
        return None

    try:
        import ai_analyzer as _ai
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)

        # 組 context — 把 features / theme 簡化成 prompt
        f = features or {}
        t = theme or {}
        ctx_lines = [f"Symbol: {symbol}", f"Current price: ${current_price}"]
        if f.get("pct_from_52w_high") is not None:
            ctx_lines.append(f"Distance from 52w high: {f['pct_from_52w_high']:+.1f}%")
        if f.get("pct_from_ath") is not None:
            ctx_lines.append(f"Distance from ATH: {f['pct_from_ath']:+.1f}%")
        if f.get("twenty_pct") is not None:
            ctx_lines.append(f"20-day return: {f['twenty_pct']:+.1f}%")
        if f.get("sixty_pct") is not None:
            ctx_lines.append(f"60-day return: {f['sixty_pct']:+.1f}%")
        if f.get("rsi"):
            ctx_lines.append(f"RSI: {f['rsi']}")
        if t.get("narrative_tags"):
            ctx_lines.append(f"Narratives: {', '.join(t['narrative_tags'][:4])}")
        if t.get("total_score") is not None:
            ctx_lines.append(f"Theme heat: {t['total_score']}/100 ({t.get('theme_strength', '?')})")

        prompt = (
            "You are a sell-side equity analyst. Based on the technical setup + narrative "
            "context below, estimate this US stock's 3-month and 6-month price target. "
            "Consider: business momentum, sector tailwinds, valuation (P/S, EV/EBITDA), "
            "and chart structure. Be realistic, not promotional.\n\n"
            "Context:\n" + "\n".join(ctx_lines) + "\n\n"
            "Respond in strict JSON only (no markdown), with fields:\n"
            '  {"target_3m": float, "target_6m": float, '
            '"bull_target": float, "bear_target": float, '
            '"confidence": int (0-100), "reasoning": "≤80 words English"}'
        )

        resp = m.generate_content(
            prompt,
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 400,
                "response_mime_type": "application/json",
            },
            safety_settings=_ai.get_safety_settings(),
        )
        text = (getattr(resp, "text", "") or "").strip()
        if not text:
            return None
        # Strip markdown fences if any
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        # Sanitize
        out = {
            "target_3m": float(data.get("target_3m", 0) or 0),
            "target_6m": float(data.get("target_6m", 0) or 0),
            "bull_target": float(data.get("bull_target", 0) or 0),
            "bear_target": float(data.get("bear_target", 0) or 0),
            "confidence": int(data.get("confidence", 0) or 0),
            "reasoning": str(data.get("reasoning", ""))[:200],
            "source": "gemini-" + model,
            "estimated_at": today_str,
        }
        _cache_set(key, out)
        return out
    except Exception as e:
        print(f"[gemini_target_estimator] {symbol} failed: {e}", flush=True)
        return None


def estimate_batch(symbols: List[str],
                    features_map: Optional[Dict[str, Dict]] = None,
                    theme_map: Optional[Dict[str, Dict]] = None,
                    max_calls: int = 10) -> Dict[str, Optional[Dict]]:
    """批次估算. max_calls 限制最多打幾次 Gemini (省 quota).

    通常上層只對「最高分」的 picks 才呼叫 (e.g., score >= 80),
    此處再 cap max_calls 為硬上限.
    """
    features_map = features_map or {}
    theme_map = theme_map or {}
    out: Dict[str, Optional[Dict]] = {}
    n_called = 0
    for sym in symbols[:max_calls]:
        f = features_map.get(sym) or {}
        cur = f.get("current") or 0
        if cur <= 0:
            out[sym] = None
            continue
        out[sym] = estimate_target(
            sym, current_price=cur,
            features=f, theme=theme_map.get(sym),
        )
        n_called += 1
    return out


def format_target_block(target_data: Optional[Dict], current: float) -> str:
    """格式化單檔 target 結果為一行中文摘要."""
    if not target_data:
        return ""
    t3 = target_data.get("target_3m", 0)
    t6 = target_data.get("target_6m", 0)
    bull = target_data.get("bull_target", 0)
    bear = target_data.get("bear_target", 0)
    conf = target_data.get("confidence", 0)
    reason = target_data.get("reasoning", "")
    pct_3m = (t3 / current - 1) * 100 if current > 0 and t3 > 0 else 0
    pct_6m = (t6 / current - 1) * 100 if current > 0 and t6 > 0 else 0
    parts = [f"3m ${t3:.0f} ({pct_3m:+.0f}%)", f"6m ${t6:.0f} ({pct_6m:+.0f}%)"]
    if bull > 0:
        parts.append(f"bull ${bull:.0f}")
    if bear > 0:
        parts.append(f"bear ${bear:.0f}")
    return " · ".join(parts) + f" · 信心 {conf}/100" + (f"\n  💬 {reason}" if reason else "")
