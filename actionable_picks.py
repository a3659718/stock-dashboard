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
# L1 修正: 把 indicators import 移到模組頂部, 避免每次 _dynamic_levels 都 re-import
try:
    import indicators as _ind
except Exception:
    _ind = None  # 容錯: 若 indicators 載入失敗, _dynamic_levels 自動走 fallback


# ---------------------------------------------------------------------------
# B8 修正: 取代固定 % 為 ATR-based 動態 levels.
# 對非 upside_screener 的來源 (breakout / emerging / leader) 也提供合理停損.
# 若 ATR 抓不到 (新股 / 停牌 / 資料不足) 才 fallback 用固定 %, 並標記降級.
# ---------------------------------------------------------------------------
def _dynamic_levels(stock_id: str, current: float,
                     stop_atr_mult: float = 1.5,
                     target_atr_mult: float = 3.0,
                     fallback_stop_pct: float = 0.03,
                     fallback_target_pct: float = 0.06,
                     market: str = "TW") -> Optional[Dict]:
    """從 yfinance 抓近 3 個月日線, 算 ATR 後給出動態 levels.
    抓失敗時用固定 % fallback. 回傳 dict (entry_low/high, stop, target, rr, source).
    """
    try:
        # L1 修正: indicators 已在模組頂部 import; 若 import 失敗就走 fallback
        if _ind is None:
            raise RuntimeError("indicators module not available")
        # 同檔需 .TW / .TWO 兩個 fallback
        suffixes = [".TW", ".TWO"] if market == "TW" else [""]
        df = None
        for suf in suffixes:
            sym = f"{stock_id}{suf}" if market == "TW" else stock_id
            try:
                d = ds.fetch_yf_history(sym, period="3mo", interval="1d")
                if d is not None and not d.empty and len(d) >= 20:
                    df = d
                    break
            except Exception:
                continue
        if df is not None and len(df) >= 20:
            lv = _ind.atr_based_levels(
                df["High"].astype(float),
                df["Low"].astype(float),
                df["Close"].astype(float),
                entry_price=float(current),
                stop_atr_mult=stop_atr_mult,
                target_atr_mult=target_atr_mult,
            )
            if lv:
                lv["source"] = "atr"
                return lv
    except Exception:
        pass
    # Fallback: 固定 % (跟舊行為一致, 但加 flag)
    try:
        cur_f = float(current)
    except (TypeError, ValueError):
        return None
    return {
        "entry_low": round(cur_f * 0.99, 2),
        "entry_high": round(cur_f * 1.01, 2),
        "stop": round(cur_f * (1 - fallback_stop_pct), 2),
        "target": round(cur_f * (1 + fallback_target_pct), 2),
        "atr": None, "atr_pct": None,
        "rr": round(fallback_target_pct / fallback_stop_pct, 2),
        "source": "fallback_pct",
    }


def _score_pick(pick: Dict) -> float:
    """組合分數 — R:R 加權 + 訊號交叉 + 機率 + (新) upside_score 加權.
    upside_screener 來的 pick 已經做過完整的技術 / 籌碼 / 動能評分,
    若帶 _upside_score (0-100) 直接折算為基礎分, 其他來源用舊邏輯.
    """
    score = 0.0
    upside = pick.get("_upside_score")
    if upside is not None:
        # 0-100 → 0-8 base, 再用 reasons / warnings 微調
        score += min(float(upside) / 12.5, 8.0)
    rr = pick.get("rr") or 0
    score += min(rr * 1.5, 5.0)  # R:R 上限 5 分
    score += min(len(pick.get("reasons", [])) * 0.6, 3.0)  # 多訊號加分
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
                                respect_regime: bool = True,
                                open_data: Optional[Dict] = None,
                                mainstream_only: bool = False) -> List[Dict]:
    """從各訊號源 mash up 成 top N 可下單清單.

    來源優先序 (高 → 低):
      0. upside_screener 三類 (走 @st.cache_data, 15 分鐘共享)
      1. potential_picks (market_open_picks 已算好 entry/target/stop/win_prob)
      2. next_day_breakout (closing_analyzer)
      3. sector_pulse leaders + 催化劑
      4. emerging_themes leading stocks

    每個 pick 會「跟其他訊號 cross-check」加 reasons / warnings.

    respect_regime=True 時, 如果 regime=bear, 直接回空清單 (不推薦進場).
    回傳前會 attach regime banner 進 result[0]["_regime"] 給 caller render.

    B9 修正 - open_data: 若 caller 已經呼叫過 market_open_picks.get_tw_open_picks(),
        可傳入結果避免重複跑 (省 30-60 秒 + Gemini quota). None 時自己抓.
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

    # 來源 0 (最高優先): upside_screener 三類潛力股.
    # 走 @st.cache_data 包裝版本 — 同一份結果 15 分鐘內共用,
    # 避免每次 dashboard 互動都重打 ~300 個 FinMind API.
    # max_stocks=120 平衡掃描廣度與速度 (~25 秒 cold, <1 秒 cached).
    try:
        import upside_screener
        up = upside_screener.run_upside_screen(
            market="all", max_stocks=120, use_cache=True,
        )
        # 三類各取前 3 檔, 已是去重後排序
        for cat_key in ("early_stage", "momentum", "reversal"):
            for p in (up.get(cat_key) or [])[:3]:
                cand = _build_from_upside(p)
                if cand:
                    candidates.append(cand)
    except Exception as e:
        print(f"[actionable_picks] upside_screener failed: {e}", flush=True)

    # 來源 1: market_open_picks 的 potential_picks (給目標價最完整)
    # B9 修正: 若 caller 已傳入 open_data 就直接用, 避免重複跑 30-60 秒的 get_tw_open_picks
    try:
        if open_data is not None:
            d = open_data
        else:
            import market_open_picks
            # tw_open_picks 在收盤後跑會給最新; 開盤前跑會 fallback 用昨資料
            d = market_open_picks.get_tw_open_picks()
        for p in (d.get("potential_picks") or []):
            cand = _build_from_potential(p, d)
            if cand:
                candidates.append(cand)
    except Exception as e:
        print(f"[actionable_picks] potential_picks failed: {e}", flush=True)
        d = {}  # 給後面 cross-check 用空 dict 避免 NameError

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

    # Dedup by stock_id, 合併 reasons.
    # 同一檔有多個來源時, 「upside_*」的 levels (ATR-based) 優先取代固定 % 的 levels.
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
            # upside 來源的 levels 覆蓋舊的 (ATR-based 比固定 % 合理)
            is_new_upside = str(c.get("source", "")).startswith("upside_")
            is_old_upside = str(existing.get("source", "")).startswith("upside_")
            if is_new_upside and not is_old_upside:
                for k in ("entry_low", "entry_high", "target", "stop", "rr",
                          "win_prob", "hold_period"):
                    if c.get(k):
                        existing[k] = c[k]
                existing["source"] = c["source"]
                # 保留 upside score 給排序加權
                if c.get("_upside_score") is not None:
                    existing["_upside_score"] = c["_upside_score"]
            else:
                # 取較完整的 entry/target/stop (有的優先, 不覆蓋)
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

    # 對 result 加 entry_label (對應 US Top 10 / 強勢族群 leaders 同樣標籤)
    try:
        import entry_label_helper as _el
        syms = [str(p.get("stock_id", "")) for p in result if p.get("stock_id")]
        pairs = [(s, "TW") for s in syms]
        if pairs:
            eval_map = _el.batch_evaluate(pairs, max_workers=8)
            for p in result:
                sid = str(p.get("stock_id", ""))
                ev = eval_map.get(sid) or {}
                p["entry_label"] = ev.get("entry_label", "—")
                p["entry_emoji"] = ev.get("entry_emoji", "")
                p["entry_score"] = ev.get("entry_score")
                p["entry_action"] = ev.get("entry_action", "—")
    except Exception as _e:
        print(f"[actionable] entry_label 計算失敗 (non-fatal): {_e}", flush=True)

    # ABCDF enrich: 3 層目標 + chip_price_divergence + 強勢族群 boost + 主力券商
    # + 主流板塊 +0.5 (mainstream_only=True 時 filter 只回主流板塊)
    try:
        import actionable_enhancer as _ae
        result = _ae.enhance_picks(result, market=market,
                                    mainstream_only=mainstream_only)
    except Exception as _e:
        print(f"[actionable] enhance_picks 失敗 (non-fatal): {_e}", flush=True)

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


def _build_from_upside(p: Dict) -> Optional[Dict]:
    """從 upside_screener 一筆 pick 轉成 actionable.
    優點: 已有 ATR-based levels (修正 B8), 不需 fallback.
    分數會被加權 (upside 來源是最強的 upstream 訊號之一).
    """
    sid = str(p.get("stock_id", ""))
    if not sid:
        return None
    lv = p.get("levels") or {}
    el = lv.get("entry_low")
    eh = lv.get("entry_high")
    target = lv.get("target")
    stop = lv.get("stop")
    rr = lv.get("rr")
    cur = p.get("current")
    if cur is None:
        return None

    # 反推勝率 (粗估): 起漲初期 65, 動能繼續 60, 反轉型 55
    win_prob_map = {"early_stage": "65%", "momentum": "60%", "reversal": "55%"}
    hold_map = {"early_stage": "10-20 日", "momentum": "5-10 日", "reversal": "5-15 日"}
    cat = p.get("category", "")

    reasons = list(p.get("reasons", []))[:5]
    warnings = list(p.get("warnings", []))[:3]
    # 標記類別
    label_zh = {"early_stage": "起漲初期", "momentum": "動能繼續", "reversal": "反轉型"}.get(cat, cat)
    if label_zh:
        reasons.insert(0, f"[{label_zh}] upside ~{p.get('upside_pct', '?')}%")

    return {
        "stock_id": sid,
        "name": p.get("name", ""),
        "theme": label_zh,
        "current": cur,
        "entry_low": el, "entry_high": eh,
        "target": target, "stop": stop, "rr": rr,
        "win_prob": win_prob_map.get(cat, "55%"),
        "hold_period": hold_map.get(cat, "5-10 日"),
        "catalyst": "",
        "reasons": reasons,
        "warnings": warnings,
        "source": f"upside_{cat}",
        "_upside_score": p.get("score", 0),  # 給 _score_pick 加權用
    }


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
    """從 next_day_breakout 一筆轉成 actionable. B8 修正: 用 ATR-based 動態 levels."""
    sid = str(p.get("stock_id", ""))
    if not sid:
        return None
    metrics = p.get("metrics") or {}
    cur = metrics.get("close")
    if not cur:
        return None
    # B8: ATR-based dynamic levels (fallback 為原本的固定 % 5/3)
    lv = _dynamic_levels(sid, cur, stop_atr_mult=1.5, target_atr_mult=2.5,
                          fallback_stop_pct=0.03, fallback_target_pct=0.05)
    if not lv:
        return None
    reasons = [f"突破訊號 (分數 {p.get('score', '—')})"]
    if lv.get("source") == "atr":
        reasons.append(f"ATR%={lv.get('atr_pct')}, 動態停損")
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
        "entry_low": lv["entry_low"], "entry_high": lv["entry_high"],
        "target": lv["target"], "stop": lv["stop"], "rr": lv["rr"],
        "win_prob": "60%",
        "hold_period": "1-3 日",
        "catalyst": "",
        "reasons": reasons,
        "warnings": [],
        "source": f"breakout_{lv.get('source', 'unknown')}",
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
    # B8 修正: ATR-based 動態 levels (萌芽期偏好較大 R:R, target_mult 3 = R:R 2)
    lv = _dynamic_levels(sid, cur_f, stop_atr_mult=1.5, target_atr_mult=3.0,
                          fallback_stop_pct=0.04, fallback_target_pct=0.08)
    if not lv:
        return None
    reasons = [f"萌芽族群領先股: {emerging_theme.get('theme', '')}"]
    if lv.get("source") == "atr":
        reasons.append(f"ATR%={lv.get('atr_pct')}, 動態停損")
    for r in (emerging_theme.get("reasons") or [])[:2]:
        reasons.append(f"族群: {r}")
    return {
        "stock_id": sid,
        "name": stock.get("name", ""),
        "theme": emerging_theme.get("theme", ""),
        "current": cur_f,
        "entry_low": lv["entry_low"], "entry_high": lv["entry_high"],
        "target": lv["target"], "stop": lv["stop"], "rr": lv["rr"],
        "win_prob": "60%",
        "hold_period": "5-10 日",
        "catalyst": "",
        "reasons": reasons,
        "warnings": [],
        "source": f"emerging_{lv.get('source', 'unknown')}",
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
    # B8 修正: ATR-based 動態 levels (族群龍頭波動較大, 給 mult 1.5/3)
    lv = _dynamic_levels(sid, cur_f, stop_atr_mult=1.5, target_atr_mult=3.0,
                          fallback_stop_pct=0.04, fallback_target_pct=0.06)
    if not lv:
        return None
    reasons = [f"族群熱: {theme}"]
    if lv.get("source") == "atr":
        reasons.append(f"ATR%={lv.get('atr_pct')}, 動態停損")
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
        "entry_low": lv["entry_low"], "entry_high": lv["entry_high"],
        "target": lv["target"], "stop": lv["stop"], "rr": lv["rr"],
        "win_prob": "55%",
        "hold_period": "3-5 日",
        "catalyst": catalyst,
        "reasons": reasons,
        "warnings": [],
        "source": f"leader_{lv.get('source', 'unknown')}",
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
        if p.get("target"):
            lines.append(f"   目標 {_esc(p['target'])} · 停損 {_esc(p.get('stop','—'))}")
        if p.get("win_prob"):
            lines.append(f"   勝率 {_esc(p['win_prob'])} · 持有 {_esc(p.get('hold_period','—'))}")
        if p.get("entry_label"):
            lines.append(f"   {p.get('entry_emoji','')} 入場標籤: {p.get('entry_label')} (分數 {p.get('entry_score','—')})")
        reasons = p.get("reasons") or []
        for r in reasons[:3]:
            lines.append(f"   • {_esc(r)}")
        warnings = p.get("warnings") or []
        for w in warnings[:2]:
            lines.append(f"   ⚠️ {_esc(w)}")
    return "\n".join(lines)
