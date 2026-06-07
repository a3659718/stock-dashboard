"""
daily_outlook_advisor.py
Gemini 當日走勢預測 — 預測台股 / 美股 開高走低 / 開低走高 / 震盪 / 趨勢明朗.

輸入: 隔夜美股 + 期貨夜盤 + 亞股先開 + VIX + DXY + 美債 + Trump 重大新聞
輸出 (Gemini 結構化):
  {
    "scenario": "開高走低 / 開高走高 / 開低走高 / 開低走低 / 震盪整理",
    "scenario_probability": 65,
    "key_signals": ["費半 +2.5%", "VIX -8%", "日韓開盤強"],
    "trade_action_long": "強勢族群可短追, ...",
    "trade_action_short": "若大盤反轉, 弱勢族群短空 ...",
    "stop_loss_advice": "短線停損 -3%, 短空停損 +3%"
  }

API:
  - predict_tw_outlook() -> Dict   # TPE 08:30 前用
  - predict_us_outlook() -> Dict   # TPE 21:15 (US 開盤前 30min)
  - format_outlook_for_tg(o) -> str
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, Optional, List

import data_sources as ds


# ---------------------------------------------------------------------------
# 抓 macro snapshot
# ---------------------------------------------------------------------------
def _safe_pct(sym: str, period: str = "5d", interval: str = "1d") -> Optional[float]:
    """抓最近一日 % 變化."""
    try:
        df = ds.fetch_yf_history(sym, period=period, interval=interval)
        if df is None or df.empty or len(df) < 2:
            return None
        c = df["Close"].astype(float)
        return round((float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100, 2)
    except Exception:
        return None


def _safe_level(sym: str, period: str = "5d", interval: str = "1d") -> Optional[float]:
    """抓最新收盤."""
    try:
        df = ds.fetch_yf_history(sym, period=period, interval=interval)
        if df is None or df.empty:
            return None
        return round(float(df["Close"].iloc[-1]), 2)
    except Exception:
        return None


def _intraday_pct(sym: str) -> Optional[float]:
    """抓今日 intraday open→current %."""
    try:
        df = ds.fetch_yf_history(sym, period="2d", interval="5m")
        if df is None or df.empty or len(df) < 2:
            return None
        import pandas as pd
        date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
        df = df.copy()
        df["_dt"] = pd.to_datetime(df[date_col])
        df["_d"] = df["_dt"].dt.date
        today = df["_d"].max()
        today_bars = df[df["_d"] == today].sort_values("_dt")
        if today_bars.empty or len(today_bars) < 2:
            return None
        op = float(today_bars["Open"].iloc[0])
        cu = float(today_bars["Close"].iloc[-1])
        return round((cu / op - 1) * 100, 2) if op > 0 else None
    except Exception:
        return None


def _build_tw_snapshot() -> Dict:
    """台股 08:30 預測前的 snapshot (含外資籌碼面)."""
    snap = {
        "us_sp500_pct": _safe_pct("^GSPC"),
        "us_nasdaq_pct": _safe_pct("^IXIC"),
        "us_sox_pct": _safe_pct("^SOX"),
        "us_dow_pct": _safe_pct("^DJI"),
        "vix": _safe_level("^VIX"),
        "vix_pct": _safe_pct("^VIX"),
        "us_10y_yield": _safe_level("^TNX"),
        "dxy": _safe_level("DX-Y.NYB"),
        "nikkei_intraday_pct": _intraday_pct("^N225"),
        "kospi_intraday_pct": _intraday_pct("^KS11"),
        "twii_yest_close": _safe_level("^TWII"),
        "twii_5d_pct": None,
    }
    # 新增: 外資籌碼 snapshot
    try:
        import institutional_positioning as _ip
        snap["positioning"] = _ip.fetch_institutional_snapshot()
    except Exception as _e:
        print(f"[outlook] positioning fail: {_e}", flush=True)
        snap["positioning"] = {}
    return snap


def _build_us_snapshot() -> Dict:
    """美股開盤前 (TPE 21:15 = US 09:00 EDT) 的 snapshot."""
    return {
        "yest_sp500_pct": _safe_pct("^GSPC"),
        "yest_nasdaq_pct": _safe_pct("^IXIC"),
        "es_futures_pct": _safe_pct("ES=F"),
        "nq_futures_pct": _safe_pct("NQ=F"),
        "vix": _safe_level("^VIX"),
        "vix_pct": _safe_pct("^VIX"),
        "us_10y_yield": _safe_level("^TNX"),
        "dxy": _safe_level("DX-Y.NYB"),
        "asia_close_n225_pct": _safe_pct("^N225"),
        "asia_close_ks11_pct": _safe_pct("^KS11"),
        "asia_close_twii_pct": _safe_pct("^TWII"),
    }


# ---------------------------------------------------------------------------
# Gemini 預測
# ---------------------------------------------------------------------------
def _gemini_predict(market: str, snapshot: Dict) -> Dict:
    """呼叫 Gemini 結構化預測.

    market: "TW" or "US"
    return: {scenario, scenario_probability, key_signals, trade_action_long,
             trade_action_short, stop_loss_advice}
    """
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return {}
        import google.generativeai as genai
        model = genai.GenerativeModel("gemini-2.5-flash")
    except Exception as e:
        print(f"[outlook_advisor] gemini init fail: {e}", flush=True)
        return {}

    label = "台股加權指數 (^TWII)" if market == "TW" else "美股 (S&P 500 / Nasdaq)"
    session_label = "台股 09:00 開盤" if market == "TW" else "美股 09:30 EST 開盤"

    # 構造 prompt
    sig_lines = []
    if market == "TW":
        if snapshot.get("us_sp500_pct") is not None:
            sig_lines.append(f"昨夜 S&P 500: {snapshot['us_sp500_pct']:+.2f}%")
        if snapshot.get("us_nasdaq_pct") is not None:
            sig_lines.append(f"昨夜 Nasdaq: {snapshot['us_nasdaq_pct']:+.2f}%")
        if snapshot.get("us_sox_pct") is not None:
            sig_lines.append(f"昨夜 費半 SOX: {snapshot['us_sox_pct']:+.2f}%")
        if snapshot.get("vix") is not None:
            vp = snapshot.get("vix_pct")
            vpt = f" ({vp:+.2f}%)" if vp is not None else ""
            sig_lines.append(f"VIX: {snapshot['vix']:.2f}{vpt}")
        if snapshot.get("us_10y_yield") is not None:
            sig_lines.append(f"美10年公債殖利率: {snapshot['us_10y_yield']:.2f}%")
        if snapshot.get("dxy") is not None:
            sig_lines.append(f"美元指數 DXY: {snapshot['dxy']:.2f}")
        if snapshot.get("nikkei_intraday_pct") is not None:
            sig_lines.append(f"日股 (盤中): {snapshot['nikkei_intraday_pct']:+.2f}%")
        if snapshot.get("kospi_intraday_pct") is not None:
            sig_lines.append(f"韓股 (盤中): {snapshot['kospi_intraday_pct']:+.2f}%")
        # 外資籌碼面 (台股關鍵)
        try:
            import institutional_positioning as _ip
            pos_str = _ip.summarize_for_gemini(snapshot.get("positioning") or {})
            if pos_str:
                sig_lines.append(f"外資籌碼: {pos_str}")
        except Exception:
            pass
    else:
        if snapshot.get("yest_sp500_pct") is not None:
            sig_lines.append(f"昨日 S&P 500: {snapshot['yest_sp500_pct']:+.2f}%")
        if snapshot.get("yest_nasdaq_pct") is not None:
            sig_lines.append(f"昨日 Nasdaq: {snapshot['yest_nasdaq_pct']:+.2f}%")
        if snapshot.get("es_futures_pct") is not None:
            sig_lines.append(f"S&P 期貨 (盤前): {snapshot['es_futures_pct']:+.2f}%")
        if snapshot.get("nq_futures_pct") is not None:
            sig_lines.append(f"Nasdaq 期貨 (盤前): {snapshot['nq_futures_pct']:+.2f}%")
        if snapshot.get("vix") is not None:
            vp = snapshot.get("vix_pct")
            vpt = f" ({vp:+.2f}%)" if vp is not None else ""
            sig_lines.append(f"VIX: {snapshot['vix']:.2f}{vpt}")
        if snapshot.get("us_10y_yield") is not None:
            sig_lines.append(f"美10年公債: {snapshot['us_10y_yield']:.2f}%")
        if snapshot.get("dxy") is not None:
            sig_lines.append(f"美元指數: {snapshot['dxy']:.2f}")
        if snapshot.get("asia_close_n225_pct") is not None:
            sig_lines.append(f"亞洲日股收盤: {snapshot['asia_close_n225_pct']:+.2f}%")
        if snapshot.get("asia_close_ks11_pct") is not None:
            sig_lines.append(f"亞洲韓股收盤: {snapshot['asia_close_ks11_pct']:+.2f}%")
        if snapshot.get("asia_close_twii_pct") is not None:
            sig_lines.append(f"台股收盤: {snapshot['asia_close_twii_pct']:+.2f}%")

    sig_block = "\n".join(f"- {l}" for l in sig_lines) if sig_lines else "(資料不足)"

    prompt = f"""你是專業交易員. 根據下方訊號預測{session_label}的走勢.

訊號:
{sig_block}

請以 JSON 格式回答 (繁體中文), 只輸出 JSON 不要其他字:
{{
  "scenario": "從以下五選一: 開高走高 / 開高走低 / 開低走高 / 開低走低 / 震盪整理",
  "scenario_probability": 0-100 整數 (你對 scenario 的信心),
  "alt_scenario": "次可能情境 (五選一)",
  "key_signals": ["重點訊號 1", "重點訊號 2", "重點訊號 3"],
  "trade_action_long": "做多操作建議 (1-2 句, 含族群方向)",
  "trade_action_short": "做空操作建議 (1-2 句, 含族群方向)",
  "stop_loss_advice": "停損建議 (1 句)"
}}

判斷準則:
- 美股大漲 + VIX 跌 + 亞股先開強 → 開高走高機率高
- 美股大跌 + VIX 漲 + 亞股先開弱 → 開低走低
- 美股漲但盤中翻黑 + VIX 微升 → 開高走低 (常見台股反轉)
- 期貨平盤 + VIX 平盤 → 震盪整理
"""

    try:
        resp = model.generate_content(prompt)
        raw = resp.text.strip()
        # 去掉 ```json wrapper
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:].strip()
        import json
        data = json.loads(raw)
        return data
    except Exception as e:
        print(f"[outlook_advisor] gemini predict fail: {e}", flush=True)
        return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def predict_tw_outlook() -> Dict:
    """台股 08:30 預測."""
    snap = _build_tw_snapshot()
    pred = _gemini_predict("TW", snap)
    return {"market": "TW", "snapshot": snap, "prediction": pred}


def predict_us_outlook() -> Dict:
    """美股 21:15 預測."""
    snap = _build_us_snapshot()
    pred = _gemini_predict("US", snap)
    return {"market": "US", "snapshot": snap, "prediction": pred}


# ---------------------------------------------------------------------------
# Format for TG
# ---------------------------------------------------------------------------
SCENARIO_EMOJI = {
    "開高走高": "🚀",
    "開高走低": "📉",
    "開低走高": "📈",
    "開低走低": "🔻",
    "震盪整理": "↔️",
}


def format_outlook_for_tg(outlook: Dict) -> str:
    """格式化預測為 TG 訊息. 直接給 user 看「該怎麼操作」."""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)

    market = outlook.get("market", "TW")
    pred = outlook.get("prediction", {})
    if not pred:
        return ""  # 沒有預測就不推

    label = "台股" if market == "TW" else "美股"
    scenario = pred.get("scenario", "—")
    emoji = SCENARIO_EMOJI.get(scenario, "🎯")
    prob = pred.get("scenario_probability", 0)
    alt = pred.get("alt_scenario", "")

    lines = [
        f"{emoji} <b>{label}走勢預測</b> (Gemini)",
        f"<b>主情境</b>: {_esc(scenario)} (信心 {prob}%)",
    ]
    if alt and alt != scenario:
        lines.append(f"<i>次可能: {_esc(alt)}</i>")
    lines.append("")

    # 訊號
    sigs = pred.get("key_signals", [])
    sigs = pred.get("key_signals", [])
    if sigs:
        lines.append("<b>📊 關鍵訊號</b>")
        for s in sigs[:3]:
            lines.append(f"• {_esc(s)}")
        lines.append("")

    # 多空建議
    al = pred.get("trade_action_long", "")
    sh = pred.get("trade_action_short", "")
    if al:
        lines.append(f"🟢 <b>多單</b>: {_esc(al)}")
    if sh:
        lines.append(f"🔴 <b>空單</b>: {_esc(sh)}")
    sl = pred.get("stop_loss_advice", "")
    if sl:
        lines.append(f"🛑 <b>停損</b>: {_esc(sl)}")

    lines.append("")
    lines.append("<i>※ AI 預測僅供參考, 實際盤中以價量為準.</i>")
    return "\n".join(lines)
