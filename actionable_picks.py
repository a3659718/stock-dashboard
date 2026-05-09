"""
actionable_picks.py
把 sector_pulse / market_open_picks / closing_analyzer / chip_analyzer 散在
各 tab 的訊號整合成「可下單清單」, 給 dashboard 頂部「今日可行動」卡片用.

每個 pick 是一個 dict, 含可下單需要的所有欄位:
{
    "stock_id":     "2330",
    "name":         "台積電",
    "theme":        "AI 半導體",
    "current":      1050,
    "entry_low":    1040,
    "entry_high":   1055,
    "target":       1150,
    "stop":         1000,
    "rr":           2.0,
    "win_prob":     "65%",
    "hold_period":  "5-10 日",
    "catalyst":     "AI 訂單湧入",
    "reasons":      ["族群熱", "投信買超", "美股隔夜強勢"],   # 訊號交叉驗證
    "warnings":     ["美元走強壓力"],
    "score":        8.5,    # 綜合分數
    "position":     {       # 部位建議 (用 user 設定)
        "lots": 0.2,
        "shares": 200,
        "position_value": 210000,
        "risk_dollars": 10000,
        "risk_pct": 1.0,
    }
}

對外接口:
    compute_actionable_picks(top_n=5) -> List[Dict]
    fmt_actionable_card_md(pick) -> str   # markdown for streamlit
    fmt_actionable_picks_tg(picks) -> str  # HTML for telegram
"""

from __future__ import annotations

from typing import Dict, List, Optional

import data_sources as ds


def _score_pick(pick: Dict) -> float:
    """簡單組合分數 — R:R 加權最重, 訊號交叉次之, 機率加分."""
    score = 0.0
    rr = pick.get("rr") or 0
    score += min(rr * 1.5, 5.0)  # R:R 上限 5 分
    score += min(len(pick.get("reasons", [])) * 0.8, 3.0)  # 多訊號加分
    win_prob = pick.get("win_prob")
    if win_prob and isinstance(win_prob, (int, float, str)):
        try:
            wp = float(str(win_prob).rstrip("%"))
            if wp >= 70:
                score += 2.0
            elif wp >= 60:
                score += 1.0
        except (TypeError, ValueError):
            pass
    score -= len(pick.get("warnings", [])) * 0.5
    return round(max(0, score), 2)


def compute_actionable_picks(top_n: int = 5, market: str = "TW",
                                respect_regime: bool = True) -> List[Dict]:
    """從各訊號源 mash up 成 top N 可下單清單.

    來源優先序 (高 → 低):
      1. potential_picks (market_open_picks 已算好 entry/target/stop/win_prob)
      2. next_day_breakout (closing_analyzer)
      3. sector_pulse leaders + 催化劑
      4. emerging_themes leading stocks (萌芽族群裡的領先股)

    每個 pick 會「跟其他訊號 cross-check」加 reasons / warnings.

    respect_regime=True (default) 時, 如果 regime=bear, 直接回空清單 (不推薦進場).
    回傳前會 attach regime banner 進 result[0]["_regime"] 給 caller render.
    """
    if market != "TW":
        return []

    # 先檢查 regime — 空頭 regime 直接回空 (不該推薦進場)
    regime: Dict = {}
    try:
        import regime_detector
        regime = regime_detector.detect_market_regime("TW")
    except Exception as e:
        print(f"[actionable_picks] regime failed: {e}", flush=True)
    if respect_regime and regime.get("regime") == "bear":
        # 仍然回 1 筆 dummy 帶 regime banner — caller 可顯示警告
        return [{"_regime": regime, "_no_picks_reason": "空頭 regime 暫停進場推薦"}]

    # 用 try/except 包起來 — 任一源失敗仍能組部分清單
    candidates: List[Dict] = []

    # 來源 1: market_open_picks 的 potential_picks (給目標價最完整)
    try:
        import market_open_picks
        # tw_open_picks 在收盤後跑會給最新; 開盤前跑會 fallback 用昨資料
        d = market_open_picks.get_tw_open_picks()
        for p in (d.get("potential_picks") or []):
            cand = _build_from_potential(p, d)
            if cand:
                candidates.append(cand)
    except Exception as e:
        print(f"[actionable_picks] potential_picks failed: {e}", flush=True)

    # 來源 4: emerging_themes 萌芽族群裡的領先股 (這是真正能賺的訊號)
    try:
        import emerging_themes
        emerging = emerging_themes.find_emerging_themes(top_n=3)
        for et in emerging:
            for ls in et.get("leading_stocks", [])[:2]:
                cand = _build_from_emerging(ls, et)
                if cand:
                    candidates.append(cand)
    except Exception as e:
        print(f"[actionable_picks] emerging failed: {e}", flush=True)

    # 來源 2: closing_analyzer 的 next_day_breakout
    try:
        import closing_analyzer
        bo = closing_analyzer.pick_next_day_breakout(top_n=10, max_scan=100)
        for p in bo:
            cand = _build_from_breakout(p)
            if cand:
                candidates.append(cand)
    except Exception as e:
        print(f"[actionable_picks] breakout failed: {e}", flush=True)

    # 來源 3: hot_themes leaders (有催化劑的優先)
    try:
        import sector_pulse
        hot = sector_pulse.compute_hot_themes()
        leaders_map = hot.get("leaders") or {}
        themes_df = hot.get("themes")
        if themes_df is not None and not themes_df.empty:
            for theme in themes_df["題材"].head(3).tolist():
                ldf = leaders_map.get(theme)
                if ldf is None or ldf.empty:
                    continue
                for _, r in ldf.head(2).iterrows():
                    cand = _build_from_leader(r.to_dict(), theme)
                    if cand:
                        candidates.append(cand)
    except Exception as e:
        print(f"[actionable_picks] leaders failed: {e}", flush=True)

    # Dedup by stock_id, 合併 reasons
    by_sid: Dict[str, Dict] = {}
    for c in candidates:
        sid = c.get("stock_id")
        if not sid:
            continue
        if sid in by_sid:
            existing = by_sid[sid]
            # 合併 reasons / warnings
            for r in c.get("reasons", []):
                if r not in existing["reasons"]:
                    existing["reasons"].append(r)
            for w in c.get("warnings", []):
                if w not in existing["warnings"]:
                    existing["warnings"].append(w)
            # 取較完整的 entry/target/stop (有的優先)
            for k in ("entry_low", "entry_high", "target", "stop", "rr",
                      "win_prob", "hold_period", "catalyst"):
                if not existing.get(k) and c.get(k):
                    existing[k] = c[k]
        else:
            by_sid[sid] = c

    picks = list(by_sid.values())
    # 算分數 + 排序
    for p in picks:
        p["score"] = _score_pick(p)

    # 部位建議 — 用 regime multiplier 動態調整 risk_per_trade_pct
    regime_mult = float(regime.get("position_multiplier", 1.0)) if regime else 1.0
    try:
        import position_sizer as _ps
        cfg = _ps.load_user_config()
        adjusted_risk = cfg["risk_per_trade_pct"] * regime_mult
        for p in picks:
            try:
                el = p.get("entry_low")
                eh = p.get("entry_high")
                if el is None or eh is None:
                    continue
                entry_mid = (float(el) + float(eh)) / 2
                stop = p.get("stop")
                if stop is None:
                    continue
                sizing = _ps.compute_position_size(
                    entry_price=entry_mid, stop_price=stop,
                    account_capital=cfg["account_capital"],
                    risk_per_trade_pct=adjusted_risk,
                    max_position_pct=cfg["max_position_pct"],
                    market=market,
                )
                if sizing:
                    sizing["regime_adjusted"] = (regime_mult < 1.0)
                p["position"] = sizing
            except Exception:
                continue
    except Exception:
        pass

    picks.sort(key=lambda x: x.get("score", 0), reverse=True)
    result = picks[:top_n]
    # attach regime 給第一筆 (caller render banner 用)
    if result and regime:
        result[0]["_regime"] = regime
    return result


def _compute_rr(entry_low, entry_high, target, stop) -> Optional[float]:
    try:
        el, eh, t, s = float(entry_low), float(entry_high), float(target), float(stop)
        entry = (el + eh) / 2
        if entry <= s:
            return None
        risk = entry - s
        reward = t - entry
        if risk <= 0:
            return None
        return round(reward / risk, 2)
    except (TypeError, ValueError):
        return None


def _build_from_potential(p: Dict, parent_data: Dict) -> Optional[Dict]:
    """從 potential_picker 的一筆轉成 actionable. 來自 market_open_picks."""
    sid = str(p.get("stock_id", ""))
    if not sid:
        return None
    el = p.get("entry_low")
    eh = p.get("entry_high")
    target = p.get("target_price")
    stop = p.get("stop_loss")
    rr = _compute_rr(el, eh, target, stop)

    reasons = []
    # cross-ref: 是不是熱門題材?
    theme = p.get("theme", "")
    if theme:
        reasons.append(f"族群熱: {theme}")
    # 催化劑?
    catalyst = ""
    catalysts = parent_data.get("catalysts", {})
    if isinstance(catalysts, dict) and sid in catalysts:
        catalyst = catalysts[sid]
        if catalyst:
            reasons.append(f"催化劑: {catalyst[:60]}")
    # 美股隔夜?
    us_ov = parent_data.get("us_overnight") or {}
    spy_pct = (us_ov.get("SPY") or {}).get("pct")
    if spy_pct is not None and spy_pct > 0.3:
        reasons.append(f"美股隔夜偏多 SPY +{spy_pct:.2f}%")

    # 財報事件? (從 parent_data["events"] 拉)
    events = parent_data.get("events") or {}
    ev = events.get(sid) if isinstance(events, dict) else None
    if ev and isinstance(ev, dict):
        ev_summary = ev.get("summary") or ""
        if ev_summary and ev_summary != "—":
            reasons.append(f"財報: {ev_summary[:50]}")

    return {
        "stock_id": sid,
        "name": p.get("name", ""),
        "theme": theme,
        "current": p.get("current"),
        "entry_low": el, "entry_high": eh,
        "target": target, "stop": stop, "rr": rr,
        "win_prob": p.get("win_prob", ""),
        "hold_period": p.get("hold_period", ""),
        "catalyst": catalyst,
        "reasons": reasons,
        "warnings": [],
        "source": "potential_picker",
    }


def _build_from_breakout(p: Dict) -> Optional[Dict]:
    """從 next_day_breakout 一筆轉成 actionable. 沒 entry/target → 算 default."""
    sid = str(p.get("stock_id", ""))
    if not sid:
        return None
    metrics = p.get("metrics") or {}
    cur = metrics.get("close")
    if not cur:
        return None
    # default: entry = 現價±1%, target = +5%, stop = -3%
    el = round(cur * 0.99, 2)
    eh = round(cur * 1.01, 2)
    target = round(cur * 1.05, 2)
    stop = round(cur * 0.97, 2)
    rr = _compute_rr(el, eh, target, stop)
    reasons = [f"突破訊號 (分數 {p.get('score', '—')})"]
    if metrics.get("vol_ratio"):
        try:
            vr = float(metrics["vol_ratio"])
            if vr > 1.5:
                reasons.append(f"放量 {vr:.1f}x")
        except (TypeError, ValueError):
            pass
    return {
        "stock_id": sid,
        "name": p.get("name", ""),
        "theme": "",
        "current": cur,
        "entry_low": el, "entry_high": eh,
        "target": target, "stop": stop, "rr": rr,
        "win_prob": "60%",
        "hold_period": "1-3 日",
        "catalyst": "",
        "reasons": reasons,
        "warnings": [],
        "source": "breakout",
    }


def _build_from_emerging(stock: Dict, emerging_theme: Dict) -> Optional[Dict]:
    """從 emerging_themes 的 leading_stock 一筆轉成 actionable. 萌芽族群領先股是
    最早期的 setup, R:R 通常最好."""
    sid = str(stock.get("stock_id", ""))
    if not sid:
        return None
    cur = stock.get("today_pct")  # 此 dict 的 current 沒帶, 從 today_pct 推不了現價
    # 先試從 yfinance 抓現價
    try:
        df = ds.fetch_yf_history(f"{sid}.TW", period="2d", interval="1d")
        if df is None or df.empty:
            df = ds.fetch_yf_history(f"{sid}.TWO", period="2d", interval="1d")
        if df is None or df.empty:
            return None
        cur_f = float(df["Close"].astype(float).iloc[-1])
    except Exception:
        return None
    # entry/target/stop: 萌芽期還沒大漲, 進場區間貼近現價, 目標較大
    el = round(cur_f * 0.99, 2)
    eh = round(cur_f * 1.01, 2)
    target = round(cur_f * 1.08, 2)  # 萌芽期目標較大 (8%)
    stop = round(cur_f * 0.96, 2)
    rr = _compute_rr(el, eh, target, stop)
    reasons = [f"萌芽族群領先股: {emerging_theme.get('theme', '')}"]
    for r in (emerging_theme.get("reasons") or [])[:2]:
        reasons.append(f"族群: {r}")
    return {
        "stock_id": sid,
        "name": stock.get("name", ""),
        "theme": emerging_theme.get("theme", ""),
        "current": cur_f,
        "entry_low": el, "entry_high": eh,
        "target": target, "stop": stop, "rr": rr,
        "win_prob": "60%",
        "hold_period": "5-10 日",
        "catalyst": "",
        "reasons": reasons,
        "warnings": [],
        "source": "emerging",
    }


def _build_from_leader(r: Dict, theme: str) -> Optional[Dict]:
    """從 hot_themes leader 一筆轉成 actionable."""
    sid = str(r.get("stock_id", ""))
    if not sid:
        return None
    cur = r.get("現價")
    if not cur:
        return None
    try:
        cur_f = float(cur)
    except (TypeError, ValueError):
        return None
    el = round(cur_f * 0.99, 2)
    eh = round(cur_f * 1.01, 2)
    target = round(cur_f * 1.06, 2)
    stop = round(cur_f * 0.96, 2)
    rr = _compute_rr(el, eh, target, stop)
    reasons = [f"族群熱: {theme}"]
    today_pct = r.get("今日%")
    if today_pct:
        try:
            tp = float(today_pct)
            if tp > 3:
                reasons.append(f"今日強漲 +{tp:.2f}%")
        except (TypeError, ValueError):
            pass
    cat = r.get("催化劑")
    catalyst = ""
    if cat:
        catalyst = str(cat)
        reasons.append(f"催化劑: {catalyst[:60]}")
    return {
        "stock_id": sid,
        "name": r.get("stock_name", ""),
        "theme": theme,
        "current": cur,
        "entry_low": el, "entry_high": eh,
        "target": target, "stop": stop, "rr": rr,
        "win_prob": "55%",
        "hold_period": "3-5 日",
        "catalyst": catalyst,
        "reasons": reasons,
        "warnings": [],
        "source": "leader",
    }


def fmt_actionable_picks_tg(picks: List[Dict]) -> str:
    """格式化 top picks 成 TG HTML 訊息."""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)

    if not picks:
        return ""

    first = picks[0] if picks else {}
    if first.get("_no_picks_reason") and not first.get("stock_id"):
        regime = first.get("_regime") or {}
        try:
            import regime_detector
            banner = regime_detector.fmt_regime_banner(regime)
        except Exception:
            banner = ""
        return (
            "<b>🎯 今日 Top 可行動</b>\n\n"
            + (banner + "\n\n" if banner else "")
            + f"<b>⚠ {_esc(first['_no_picks_reason'])}</b>\n"
            "保護資金優先, 觀察空頭結束後再進場."
        )

    lines = ["<b>🎯 今日 Top 可行動 (整合各訊號)</b>"]
    regime = first.get("_regime") or {}
    if regime:
        try:
            import regime_detector
            banner = regime_detector.fmt_regime_banner(regime)
            if banner:
                lines.append("")
                lines.append(banner)
        except Exception:
            pass
    lines.append("")
    for i, p in enumerate(picks, 1):
        if not p.get("stock_id"):
            continue  # skip dummy regime entry
        rr = p.get("rr")
        rr_str = f"R:R {rr}" + (" ⭐⭐" if rr and rr >= 3 else " ⭐" if rr and rr >= 2 else "")
        score = p.get("score", 0)
        lines.append(
            f"<b>{i}. {_esc(p.get('stock_id'))} {_esc(p.get('name'))}</b>  "
            f"[{_esc(p.get('theme', '—'))}] · {rr_str} · 分數 {score}"
        )
        cur = p.get("current")
        el = p.get("entry_low")
        eh = p.get("entry_high")
        if cur is not None and el and eh:
            lines.append(f"   現價 {_esc(cur)} · 進場 {_esc(el)}~{_esc(eh)}")
        if p.get("target") and p.get("stop"):
            lines.append(f"   目標 {_esc(p['target'])} · 停損 {_esc(p['stop'])}")
        for r in p.get("reasons", [])[:3]:
            lines.append(f"   ✓ {_esc(r)}")
        for w in p.get("warnings", [])[:2]:
            lines.append(f"   ⚠ {_esc(w)}")
        pos = p.get("position") or {}
        if pos and pos.get("shares", 0) > 0:
            try:
                import position_sizer
                advice = position_sizer.fmt_position_advice(pos, market="TW")
                if advice:
                    if pos.get("regime_adjusted"):
                        advice += " (regime 已自動降部位)"
                    lines.append(f"   💰 {_esc(advice)}")
            except Exception:
                pass
        lines.append("")
    return "\n".join(lines).rstrip()
