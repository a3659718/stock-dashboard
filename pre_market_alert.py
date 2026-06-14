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





def _interpret_asia_leading(asia_pcts: Dict[str, float]) -> str:
    """根據日韓盤中漲跌, 解讀對台股的 leading 影響.

    asia_pcts: {"^N225": +0.85, "^KS11": -0.50}
    回一句話, 例: "日韓同向偏多, 台股可能跟漲, 留意半導體/AI"
    """
    if not asia_pcts:
        return ""
    n225 = asia_pcts.get("^N225")
    ks11 = asia_pcts.get("^KS11")

    # 兩個都有
    if n225 is not None and ks11 is not None:
        if n225 >= 0.5 and ks11 >= 0.5:
            return "日韓同步走強 → 台股偏多開機率高, 可看半導體/AI"
        if n225 <= -0.5 and ks11 <= -0.5:
            return "日韓同步走弱 → 台股偏空開機率高, 持倉留意減碼"
        if n225 >= 0.5 and ks11 <= -0.5:
            return f"日強韓弱 (混合訊號) → 台股可能震盪, 觀察韓股是否反彈 (現 {ks11:+.2f}%)"
        if n225 <= -0.5 and ks11 >= 0.5:
            return f"日弱韓強 → 台股可能震盪, 觀察日股是否反彈 (現 {n225:+.2f}%)"
        # 兩者皆小幅
        return "日韓盤整 → 台股可能平盤震盪, 等開盤明朗"
    # 只有一個
    if n225 is not None:
        if n225 >= 0.5:
            return "日經偏強 → 台股偏多開"
        if n225 <= -0.5:
            return "日經偏弱 → 台股偏空開"
        return "日經平盤 → 台股可能跟著平盤"
    if ks11 is not None:
        if ks11 >= 0.5:
            return "KOSPI 偏強 → 台股可能跟漲 (韓股對半導體連動高)"
        if ks11 <= -0.5:
            return "KOSPI 偏弱 → 台股可能跟跌"
    return ""


def _build_tldr(us_snaps: Dict, asia_snaps: Dict) -> str:
    """一句話 TL;DR — 給 08:30 推播第一行用. 整合美股強弱 + 亞股先開 + 籌碼 bias.

    例: "美股費半 +1.8%, 日韓盤中弱 → 台股偏多開, 多看半導體拉回, 空看航運"
    """
    parts = []

    # 1. 美股核心訊號 (費半 + 那指, 取最大絕對值)
    us_strong = []
    us_weak = []
    for sym, label in [("^SOX", "費半"), ("^IXIC", "那指"), ("QQQ", "QQQ")]:
        s = us_snaps.get(sym, {})
        if s and s.get("pct_vs_prev") is not None:
            p = s["pct_vs_prev"]
            if p >= 1.5:
                us_strong.append(f"{label} {p:+.2f}%")
            elif p <= -1.5:
                us_weak.append(f"{label} {p:+.2f}%")
    if us_strong:
        parts.append(", ".join(us_strong[:2]))
    elif us_weak:
        parts.append(", ".join(us_weak[:2]))

    # 2. 亞股先開方向
    asia_avg = None
    try:
        ps = [asia_snaps[s].get("pct_vs_open", 0)
              for s in asia_snaps if asia_snaps[s]]
        if ps:
            asia_avg = sum(ps) / len(ps)
    except Exception:
        pass
    asia_tag = ""
    if asia_avg is not None:
        if asia_avg >= 0.5:
            asia_tag = "日韓強"
        elif asia_avg <= -0.5:
            asia_tag = "日韓弱"

    # 3. 籌碼 bias (若 FinMind 可用)
    bias = ""
    try:
        import institutional_positioning as _ip
        snap = _ip.fetch_institutional_snapshot()
        b_label = snap.get("bias_label", "")
        if "偏多" in b_label:
            bias = "外資偏多"
        elif "偏空" in b_label:
            bias = "外資偏空"
    except Exception:
        pass

    # 4. 走勢 + 操作方向
    direction = ""
    if us_strong and (asia_tag != "日韓弱"):
        direction = "→ 台股偏多開, 多看半導體/AI拉回"
    elif us_weak or asia_tag == "日韓弱":
        direction = "→ 台股偏空開, 空看弱勢族群, 多單觀望"
    elif asia_tag == "日韓強":
        direction = "→ 台股偏多開, 留意拉回買點"
    else:
        direction = "→ 台股平盤震盪, 等開盤明朗"

    tldr_parts = []
    if parts: tldr_parts.append(" / ".join(parts))
    if asia_tag: tldr_parts.append(asia_tag)
    if bias: tldr_parts.append(bias)
    tldr = " · ".join(tldr_parts) + " " + direction
    return tldr.strip()


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
    # Bug fix: 台股假日 (元旦/春節/清明/端午等) 不該推, 資料會是錯的
    try:
        import holiday_check
        if holiday_check.is_market_closed_today("TW"):
            return ""
    except Exception:
        pass

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

    # TL;DR — 第一行濃縮重點 (只在 08:30 完整版顯示, 08:15 資料不夠)
    tldr = ""
    if slot == "08:30":
        try:
            tldr = _build_tldr(us_snaps, asia_snaps)
        except Exception as _te:
            print(f"[pre_market] tldr fail: {_te}", flush=True)

    lines = [f"🌅 <b>台股盤前 ({slot})</b>"]
    if tldr:
        lines.append(f"⚡ <b>TL;DR</b>: {tldr}")
    lines.append("━━━━━━━━━━━━━━━━━")

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

    # 日韓盤中 — 含當下價位 + 對台股的 leading 解讀
    asia_parts = []
    asia_pcts = {}
    for sym, label, flag in [("^N225", "日經 225", "🇯🇵"), ("^KS11", "KOSPI", "🇰🇷")]:
        s = asia_snaps.get(sym, {})
        if s.get("current"):
            pct = s.get("pct_vs_open", 0)
            cur = s.get("current", 0)
            asia_pcts[sym] = pct
            tag = "🟢" if pct >= 0 else "🔴"
            asia_parts.append(f"{tag} {flag} {label} {cur:,.0f} (開盤 <b>{pct:+.2f}%</b>)")
    if asia_parts:
        offset = "日韓開盤 +15min" if slot == "08:15" else "日韓開盤 +30min"
        lines.append(f"🌏 <b>日韓股市盤中 ({offset})</b>")
        lines.append(f"<i>※ 日韓 08:00 先開, 台股 09:00 開盤前的領先指標</i>")
        for p in asia_parts:
            lines.append(f"  {p}")
        # Leading 解讀: 對台股影響
        leading_msg = _interpret_asia_leading(asia_pcts)
        if leading_msg:
            lines.append(f"  💡 <b>對台股</b>: {leading_msg}")
        lines.append("")

    # 強勢族群預測 (基於美股 sector ETF)
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

    # 外資籌碼面 (8:30 推, FinMind 抓不到自動 skip)
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

        # 大戶空單 (借券餘額 / 借券賣出 / 融券 / 券資比)
        try:
            import short_interest_alert as _si
            si_snap = _si.fetch_short_interest_snapshot()
            si_msg = _si.format_short_for_tg(si_snap)
            if si_msg:
                lines.append(si_msg)
                lines.append("")
        except Exception as _se:
            print(f"[pre_market] short_interest fail: {_se}", flush=True)

    # Gemini 結構化走勢預測 (08:30 才推)
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

    # Gemini 文字建議
    gem = _gemini_summary(us_snaps, asia_snaps, slot)
    if gem:
        lines.append("━━━━━━━ 🤖 Gemini 對台股建議 ━━━━━━━")
        lines.append(_esc(gem))
        lines.append("")

    # Fallback 簡易推論
    if not gem:
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
                lines.append(f"💡 美股+亞股平均 {combined:+.2f}% → 台股無明顯方向, 看開盤反應")
        except Exception:
            pass


    # 今日台股可買 Top 5 (08:30 才推) — 過濾 entry_label=BUY 且 entry_score>=70
    if slot == "08:30":
        try:
            import actionable_picks as _ap
            picks = _ap.compute_actionable_picks(top_n=10) or []
            # BUG #1 fix: 空頭 regime 時 picks=[{"_no_picks_reason": "空頭 regime 暫停進場推薦"}]
            # (沒 stock_id), 若不特別處理會被 filter 掉 → 顯示「無高品質 BUY」誤導用戶
            if picks and picks[0].get("_no_picks_reason") and not picks[0].get("stock_id"):
                lines.append("")
                lines.append(f"🎯 <i>⚠ {_esc(picks[0]['_no_picks_reason'])} (保護資金優先)</i>")
                return "\n".join(lines)
            buy_picks = [
                p for p in picks
                if p.get("stock_id")
                and p.get("entry_label") == "BUY"
                and (p.get("entry_score") is None or float(p.get("entry_score") or 0) >= 70)
            ][:5]
            if buy_picks:
                lines.append("")
                lines.append("━━━━━━━ 🎯 今日台股可買 Top 5 ━━━━━━━")
                for i, p in enumerate(buy_picks, 1):
                    sid = _esc(p.get("stock_id", ""))
                    name = _esc(p.get("name", ""))
                    theme = _esc(p.get("theme", "—"))
                    cur = p.get("current")
                    el = p.get("entry_low")
                    eh = p.get("entry_high")
                    tgt = p.get("target")
                    stop = p.get("stop")
                    score = p.get("entry_score")
                    head = f"<b>{i}. {sid} {name}</b> [{theme}]"
                    if score is not None:
                        head += f" · 入場分 {float(score):.0f}"
                    lines.append(head)
                    if cur is not None and el and eh:
                        lines.append(f"   現價 {cur} · 進場 {el}~{eh}")
                    if tgt:
                        lines.append(f"   目標 {tgt} · 停損 {_esc(stop or '—')}")
                    if p.get("win_prob"):
                        lines.append(f"   勝率 {_esc(p['win_prob'])} · 持有 {_esc(p.get('hold_period','—'))}")
            else:
                lines.append("")
                lines.append("🎯 <i>今日無高品質 BUY (entry_label=BUY + 分數 >=70), 觀望為主</i>")
        except Exception as _bpe:
            print(f"[pre_market] tw buy picks fail: {_bpe}", flush=True)

    return "\n".join(lines)
