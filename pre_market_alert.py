"""
pre_market_alert.py
台股盤前推播 (TPE 08:15 + 08:30) — 美股隔夜 + 日韓即時 + 對台股影響 (Gemini).

08:15 TPE = 日韓開盤 +15min
08:30 TPE = 日韓開盤 +30min + 美股已收盤 11hr

整合資訊:
  1. 美股昨日收盤 (SPY/QQQ/SOX/IXIC/F&G)
  2. 日股開盤後即時 (^N225)
  3. 韓股開盤後即時 (^KS11)
  4. 對台股當日操作建議 (Gemini)
  5. 強勢族群預測 (基於美股板塊輪動)

API:
  build_pre_market_msg(slot="08:15"|"08:30") -> str
"""
from __future__ import annotations

from typing import Dict, List


def _fetch_snap(symbol: str, intraday: bool = False) -> Dict:
    try:
        import data_sources as ds
        if intraday:
            df = ds.fetch_yf_history(symbol, period="2d", interval="5m")
            if df is None or df.empty:
                df = ds.fetch_yf_history(symbol, period="3d", interval="1d")
        else:
            df = ds.fetch_yf_history(symbol, period="3d", interval="1d")
        if df is None or df.empty:
            return {}
        c = df["Close"].astype(float)
        cur = float(c.iloc[-1])
        if intraday:
            try:
                if hasattr(df.index, "date"):
                    today = df.index[-1].date()
                    today_df = df[df.index.date == today]
                    if not today_df.empty:
                        op = float(today_df["Open"].iloc[0])
                        prev = float(c.iloc[-len(today_df) - 1]) if len(c) > len(today_df) else op
                    else:
                        op = float(df["Open"].iloc[-1])
                        prev = float(c.iloc[-2]) if len(c) >= 2 else cur
                else:
                    op = float(df["Open"].iloc[-1])
                    prev = float(c.iloc[-2]) if len(c) >= 2 else cur
            except Exception:
                op = float(df["Open"].iloc[-1])
                prev = float(c.iloc[-2]) if len(c) >= 2 else cur
        else:
            op = float(df["Open"].iloc[-1])
            prev = float(c.iloc[-2]) if len(c) >= 2 else cur
        return {
            "symbol": symbol,
            "current": round(cur, 2),
            "open": round(op, 2),
            "prev_close": round(prev, 2),
            "pct_vs_open": round((cur / op - 1) * 100, 2) if op > 0 else 0,
            "pct_vs_prev": round((cur / prev - 1) * 100, 2) if prev > 0 else 0,
        }
    except Exception:
        return {}





def _predict_tw_strong_sectors() -> list:
    """預測台股今日可能強勢族群 — 基於昨夜美股板塊 + 台股昨日 sector_pulse."""
    out = []
    # 1. 抓美股 sector ETF 隔夜表現 (top 3)
    try:
        import data_sources as _ds
        us_etfs = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLC", "XLU"]
        us_rank = []
        for etf in us_etfs:
            df = _ds.fetch_yf_history(etf, period="3d", interval="1d")
            if df is not None and not df.empty and len(df) >= 2:
                c = df["Close"].astype(float)
                pct = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
                us_rank.append((etf, round(pct, 2)))
        us_rank.sort(key=lambda x: x[1], reverse=True)
        # mapping US sector → TW sector
        us_to_tw = {
            "XLK": "半導體業 / AI 伺服器",
            "XLC": "通信網路 / 媒體",
            "XLF": "金融保險業",
            "XLE": "油電燃氣業",
            "XLV": "生技醫療業",
            "XLI": "電機機械 / 重電",
            "XLY": "貿易百貨 / 觀光",
            "XLP": "食品工業",
            "XLU": "公用事業",
        }
        for etf, pct in us_rank[:3]:
            tw = us_to_tw.get(etf, etf)
            out.append({"tw_sector": tw, "us_etf": etf, "us_pct": pct,
                        "source": "美股對應"})
    except Exception as e:
        print(f"[pre_market] us sector rank fail: {e}", flush=True)
    return out


def _gemini_summary(us_snaps: Dict, asia_snaps: Dict, slot: str) -> str:
    """用 Gemini 對美股+日韓 → 給台股當日建議."""
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return ""
        lines = [f"以下是台股開盤前 {slot} 的市場數據, 請給 3 句中文白話結論:"]
        if us_snaps:
            us_parts = []
            for sym in ["SPY", "QQQ", "^SOX", "^IXIC"]:
                s = us_snaps.get(sym)
                if s and s.get("pct_vs_prev") is not None:
                    us_parts.append(f"{sym} {s['pct_vs_prev']:+.2f}%")
            if us_parts:
                lines.append(f"美股昨夜: {', '.join(us_parts)}")
        if asia_snaps:
            asia_parts = []
            for sym, name in [("^N225", "日經"), ("^KS11", "KOSPI")]:
                s = asia_snaps.get(sym)
                if s and s.get("pct_vs_open") is not None:
                    asia_parts.append(f"{name} 開盤 {s['pct_vs_open']:+.2f}%")
            if asia_parts:
                lines.append(f"亞股盤中: {', '.join(asia_parts)}")
        lines.append("")
        lines.append("請給: (1) 台股今日開盤方向預測 (2) 偏好族群 (3) 風險提醒")
        prompt = "\n".join(lines)
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None:
            return ""
        resp = model.generate_content(prompt)
        return (resp.text or "").strip() if resp else ""
    except Exception as e:
        print(f"[pre_market] gemini fail: {e}", flush=True)
        return ""


def build_pre_market_msg(slot: str = "08:15") -> str:
    """建立盤前推播訊息. slot = '08:15' 或 '08:30'."""
    try:
        from notifier import _esc, _truncate_tg_msg
    except Exception:
        def _esc(x): return str(x)
        _truncate_tg_msg = lambda x: x

    # 1. 美股昨日 (daily)
    us_snaps = {
        sym: _fetch_snap(sym, intraday=False)
        for sym in ["SPY", "QQQ", "^SOX", "^IXIC"]
    }
    # 2. 日韓即時 (intraday)
    asia_snaps = {
        sym: _fetch_snap(sym, intraday=True)
        for sym in ["^N225", "^KS11"]
    }
    # 3. F&G
    fg = {}
    try:
        import data_sources as ds
        fg = ds.fetch_fear_greed() or {}
    except Exception:
        pass

    lines = [f"🌅 <b>台股盤前 ({slot})</b>", "━━━━━━━━━━━━━━━━━"]

    # 美股昨夜
    us_parts = []
    for sym, label in [("SPY", "SPY"), ("QQQ", "QQQ"),
                       ("^SOX", "費半"), ("^IXIC", "那指")]:
        s = us_snaps.get(sym, {})
        if s.get("current"):
            tag = "🟢" if s.get("pct_vs_prev", 0) >= 0 else "🔴"
            us_parts.append(f"{tag} {label} {s.get('pct_vs_prev', 0):+.2f}%")
    if us_parts:
        lines.append("📊 <b>美股昨夜</b>")
        lines.append("  " + " · ".join(us_parts))
        if fg.get("score") is not None:
            lines.append(f"  CNN F&amp;G: <b>{fg['score']:.0f}</b> ({_esc(fg.get('rating', ''))})")
        lines.append("")

    # 日韓盤中
    asia_parts = []
    for sym, label in [("^N225", "日經"), ("^KS11", "KOSPI")]:
        s = asia_snaps.get(sym, {})
        if s.get("current"):
            pct = s.get("pct_vs_open", 0)
            tag = "🟢" if pct >= 0 else "🔴"
            asia_parts.append(f"{tag} {label} 開盤 <b>{pct:+.2f}%</b>")
    if asia_parts:
        offset = "開盤 +15min" if slot == "08:15" else "開盤 +30min"
        lines.append(f"🌏 <b>亞股盤中 ({offset})</b>")
        for p in asia_parts:
            lines.append(f"  {p}")
        lines.append("")

    # 強勢族群預測 (基於美股 sector ETF)
    pred_sectors = _predict_tw_strong_sectors()
    if pred_sectors:
        lines.append("🚀 <b>今日可能強勢族群</b> (依美股對應推測)")
        for ps in pred_sectors:
            tag = "🟢" if ps["us_pct"] >= 0 else "🔴"
            lines.append(
                f"  {tag} {_esc(ps['tw_sector'])} "
                f"<i>(對應 {ps['us_etf']} {ps['us_pct']:+.2f}%)</i>"
            )
        lines.append("")

    # 新增: 外資籌碼面 (8:30 推, 期交所昨日數據已 release)
    if slot == "08:30":
        try:
            import institutional_positioning as _ip
            pos_snap = _ip.fetch_institutional_snapshot()
            pos_msg = _ip.format_positioning_for_tg(pos_snap)
            if pos_msg:
                lines.append(pos_msg)
                lines.append("")
        except Exception as _pe:
            print(f"[pre_market] positioning fail: {_pe}", flush=True)

    # 新增: Gemini 結構化走勢預測 (08:30 才推, 08:15 太早資料不全)
    if slot == "08:30":
        try:
            import daily_outlook_advisor as _doa
            outlook = _doa.predict_tw_outlook()
            ot_msg = _doa.format_outlook_for_tg(outlook)
            if ot_msg:
                lines.append("━━━━━━━ 🎯 走勢預測 ━━━━━━━")
                lines.append(ot_msg)
                lines.append("")
        except Exception as _oe:
            print(f"[pre_market] outlook fail: {_oe}", flush=True)

    # Gemini 建議
    gem = _gemini_summary(us_snaps, asia_snaps, slot)
    if gem:
        lines.append("━━━━━━━ 🤖 Gemini 對台股建議 ━━━━━━━")
        lines.append(_esc(gem))
        lines.append("")

    # 簡易推論 fallback (Gemini 失敗)
    if not gem:
        # 用美股+日韓平均推測台股偏多/偏空
        try:
            us_avg = sum(us_snaps[s].get("pct_vs_prev", 0) for s in us_snaps
                          if us_snaps[s]) / max(1, sum(1 for s in us_snaps if us_snaps[s]))
            asia_avg = sum(asia_snaps[s].get("pct_vs_open", 0) for s in asia_snaps
                           if asia_snaps[s]) / max(1, sum(1 for s in asia_snaps if asia_snaps[s]))
            combined = (us_avg + asia_avg) / 2
            if combined >= 0.5:
                lines.append(f"💡 美股+亞股平均 {combined:+.2f}% → 台股偏多開盤機率高")
            elif combined <= -0.5:
                lines.append(f"💡 美股+亞股平均 {combined:+.2f}% → 台股偏空開盤機率高")
            else:
                lines.append(f"💡 美股+亞股平均 {combined:+.2f}% → 台股可能平盤震盪")
            lines.append("")
        except Exception:
            pass

    lines.append("<i>※ 09:00 開盤前最後參考, 不構成投資建議.</i>")
    return _truncate_tg_msg("\n".join(lines).rstrip())
