"""
ensemble_voter.py
多策略投票 — 對一支股跑所有策略, 加總票數給綜合 verdict.

用途: 解決「Minervini 說 BUY, Buffett 說 AVOID, 該信誰?」的策略衝突問題.

跑的策略 (6 個):
  1. Minervini SEPA + Trend Template (動能)
  2. CANSLIM 啟發式 (RS + 突破)
  3. Stan Weinstein Stage 2 (趨勢)
  4. Buffett quality (ROE / Debt / FCF)
  5. Speculation (題材 + 量爆)
  6. entry_evaluator (技術 + 籌碼 + 基本面 + AI)

輸出:
  {
    "stock_id": "2330",
    "market": "TW",
    "buy_votes": 4, "hold_votes": 1, "avoid_votes": 1,
    "total_strategies": 6,
    "verdict": "STRONG_BUY" | "BUY" | "HOLD" | "AVOID",
    "verdict_emoji": "🟢🟢" | "🟢" | "🟡" | "🔴",
    "details": [
        {"strategy": "Minervini", "vote": "BUY", "reason": "..."},
        ...
    ],
    "summary": "綜合 4/6 看多, 中長線買進, 不適合短線投機"
  }

API:
  vote_for_stock(stock_id, market="auto") -> Dict
  fmt_ensemble_for_tg(vote_result) -> str  # 推播用
"""
from __future__ import annotations

from typing import Dict, List, Optional


# 策略權重 (BUY 加分, AVOID 減分)
STRATEGY_WEIGHTS = {
    "Minervini": 1.5,    # 強動能, 權重高
    "CANSLIM": 1.2,
    "Weinstein": 1.3,    # 中長線
    "Buffett": 1.0,      # 質量, 中性
    "Speculation": 0.7,  # 投機, 權重低
    # Bug fix (2026-08): 權重是用 strategy 字串的第一個空白前綴查表, 而 _vote_entry_evaluator
    # 回的 strategy 是中文「綜合評估」, 查不到 "entry_evaluator" 這個 key → 落到 default 1.0,
    # 權重最高的策略反而被降權。key 改成實際會被查的字串。
    "綜合評估": 1.5,  # entry_evaluator 的 strategy 字串, 綜合分數, 權重高
}


def _detect_market(stock_id: str) -> str:
    """偵測市場 (TW / US). 全數字 → TW, 其他 → US."""
    sid = str(stock_id).strip().upper()
    if sid.isdigit() and len(sid) >= 4:
        return "TW"
    # 興櫃: 4 數字 + 字母
    if len(sid) == 5 and sid[:4].isdigit() and sid[4].isalpha():
        return "TW"
    return "US"


def _vote_minervini(stock_id: str, market: str) -> Dict:
    """Minervini SEPA + Trend Template 投票."""
    out = {"strategy": "Minervini SEPA+VCP", "vote": "—", "reason": ""}
    try:
        import minervini_screener as mv
        # Bug fix: 原本呼叫 mv.count_trend_conditions 不存在, 改用 check_trend_template
        sym = f"{stock_id}.TW" if market == "TW" else stock_id
        result = mv.check_trend_template(sym)
        passed_n = result.get("pass_n") if result else None
        if passed_n is None or passed_n == 0:
            # 嘗試 .TWO (上櫃)
            if market == "TW":
                result = mv.check_trend_template(f"{stock_id}.TWO")
                passed_n = result.get("pass_n") if result else None
        if passed_n is None:
            return out
        if passed_n >= 7:
            out["vote"] = "BUY"
            out["reason"] = f"Trend Template 通過 {passed_n}/8 條 (強動能)"
        elif passed_n >= 5:
            out["vote"] = "HOLD"
            out["reason"] = f"Trend Template {passed_n}/8 條 (中等)"
        else:
            out["vote"] = "AVOID"
            out["reason"] = f"Trend Template 僅 {passed_n}/8 條 (動能不足)"
    except (ImportError, AttributeError):
        # 模組不存在或 API 不同, 用簡化版
        try:
            import data_sources as ds
            sym = f"{stock_id}.TW" if market == "TW" else stock_id
            df = ds.fetch_yf_history(sym, period="6mo", interval="1d")
            if df is None or df.empty or len(df) < 60:
                return out
            c = df["Close"].astype(float)
            cur = float(c.iloc[-1])
            ma50 = c.tail(50).mean()
            ma150 = c.tail(150).mean() if len(c) >= 150 else c.mean()
            ma200 = c.tail(200).mean() if len(c) >= 200 else c.mean()
            high_52w = c.tail(252).max() if len(c) >= 60 else c.max()

            cond_passed = 0
            if cur > ma50: cond_passed += 1
            if cur > ma150: cond_passed += 1
            if cur > ma200: cond_passed += 1
            if ma50 > ma150: cond_passed += 1
            if ma150 > ma200: cond_passed += 1
            if cur >= high_52w * 0.75: cond_passed += 1
            if cur >= high_52w * 0.95: cond_passed += 1
            ma200_30d_ago = c.tail(30).iloc[0] if len(c) >= 30 else cur
            if ma200 > ma200_30d_ago: cond_passed += 1

            if cond_passed >= 7:
                out["vote"] = "BUY"
                out["reason"] = f"動能條件 {cond_passed}/8 (簡化版)"
            elif cond_passed >= 5:
                out["vote"] = "HOLD"
                out["reason"] = f"動能條件 {cond_passed}/8 (簡化版)"
            else:
                out["vote"] = "AVOID"
                out["reason"] = f"動能條件 {cond_passed}/8 (動能不足)"
        except Exception:
            pass
    except Exception as e:
        out["reason"] = f"err: {e}"[:60]
    return out


def _vote_weinstein(stock_id: str, market: str) -> Dict:
    """Stan Weinstein Stage Analysis 投票."""
    out = {"strategy": "Weinstein Stage", "vote": "—", "reason": ""}
    try:
        import stage_analysis as sa
        result = sa.classify_stage(stock_id, market=market)
        if not result:
            return out
        stage = result.get("stage")
        if stage == 2:
            out["vote"] = "BUY"
            out["reason"] = "Stage 2 (advancing 趨勢上升)"
        elif stage == 3:
            out["vote"] = "HOLD"
            out["reason"] = "Stage 3 (topping 高檔整理)"
        elif stage == 4:
            out["vote"] = "AVOID"
            out["reason"] = "Stage 4 (declining 趨勢下跌)"
        elif stage == 1:
            out["vote"] = "HOLD"
            out["reason"] = "Stage 1 (basing 築底中)"
    except Exception as e:
        out["reason"] = f"err: {e}"[:60]
    return out


def _vote_buffett(stock_id: str, market: str) -> Dict:
    """Buffett quality 投票. 只對美股 (台股財報細節不夠)."""
    out = {"strategy": "Buffett Quality", "vote": "—", "reason": ""}
    if market != "US":
        out["reason"] = "(僅美股, 跳過)"
        return out
    try:
        import buffett_quality_filter as bf
        # Bug fix: 原本呼叫 bf.evaluate_quality 不存在, 改用 check_quality
        result = bf.check_quality(stock_id)
        if not result:
            return out
        score = result.get("score", 0) or 0
        # buffett score 0-20 範圍 (5 條件 × 4-5 分)
        if score >= 12:
            out["vote"] = "BUY"
            out["reason"] = f"Buffett 級 (score {score}/20)"
        elif score >= 6:
            out["vote"] = "HOLD"
            out["reason"] = f"中品質 (score {score}/20)"
        else:
            out["vote"] = "AVOID"
            out["reason"] = f"品質差 (score {score}/20)"
    except Exception as e:
        out["reason"] = f"err: {e}"[:60]
    return out


def _vote_canslim(stock_id: str, market: str) -> Dict:
    """CANSLIM 啟發式投票 (RS + 突破 + 量)."""
    out = {"strategy": "CANSLIM", "vote": "—", "reason": ""}
    try:
        import data_sources as ds
        sym = f"{stock_id}.TW" if market == "TW" else stock_id
        df = ds.fetch_yf_history(sym, period="6mo", interval="1d")
        if df is None or df.empty or len(df) < 60:
            return out
        c = df["Close"].astype(float)
        v = df["Volume"].astype(float)
        cur = float(c.iloc[-1])
        high_60d = c.tail(60).max()
        avg_vol = v.tail(50).mean()
        recent_vol = v.tail(5).mean()
        # 60d % 變化
        pct_60d = (cur / float(c.iloc[-60]) - 1) * 100 if len(c) >= 60 else 0

        score = 0
        reasons_l = []
        # R: relative strength (60d > +20%)
        if pct_60d >= 20:
            score += 2; reasons_l.append("RS 60d +20%")
        elif pct_60d >= 10:
            score += 1
        # N: new highs (≥ 95% of 60d high)
        if cur >= high_60d * 0.98:
            score += 2; reasons_l.append("近 60d 新高")
        # 量: 近 5d 平均量 > 50d 平均量 × 1.3
        if avg_vol > 0 and recent_vol / avg_vol >= 1.3:
            score += 1; reasons_l.append("量增 1.3x")
        if score >= 4:
            out["vote"] = "BUY"
            out["reason"] = ", ".join(reasons_l) or f"score {score}"
        elif score >= 2:
            out["vote"] = "HOLD"
            out["reason"] = f"score {score}"
        else:
            out["vote"] = "AVOID"
            out["reason"] = f"動能不足 score {score}"
    except Exception as e:
        out["reason"] = f"err: {e}"[:60]
    return out


def _vote_speculation(stock_id: str, market: str) -> Dict:
    """投機 / 題材股投票 — 適合短線追題材."""
    out = {"strategy": "Speculation", "vote": "—", "reason": ""}
    try:
        import theme_analyzer as ta
        try:
            themes = ta.theme_score(stock_id)
        except Exception:
            themes = None
        if themes and isinstance(themes, dict):
            # Bug fix (2026-08): theme_analyzer.theme_score() 回的 key 是 narrative_tags,
            # 沒有 "themes" → 這裡恆為空 list, Speculation 永遠投 HOLD, 六策略中永遠少一張 BUY 票。
            hot_themes = themes.get("narrative_tags") or themes.get("themes") or []
            if hot_themes:
                out["vote"] = "BUY"
                out["reason"] = f"熱門題材: {', '.join(hot_themes[:2])}"
            else:
                out["vote"] = "HOLD"
                out["reason"] = "無熱門題材曝險"
        else:
            out["vote"] = "HOLD"
            out["reason"] = "題材中性"
    except Exception as e:
        out["reason"] = f"err: {e}"[:60]
    return out


def _vote_entry_evaluator(stock_id: str, market: str) -> Dict:
    """綜合 entry_evaluator (技術 + 籌碼 + 基本面 + AI)."""
    out = {"strategy": "綜合評估", "vote": "—", "reason": ""}
    try:
        import entry_evaluator as ee
        result = ee.quick_evaluate(stock_id, market=market)
        if not result:
            return out
        label = result.get("entry_label")
        score = result.get("entry_score") or 0
        if label in ("BUY", "STRONG_BUY") or score >= 70:
            out["vote"] = "BUY"
            out["reason"] = f"綜合 {score}/100"
        elif label == "WAIT" or 45 <= score < 70:
            out["vote"] = "HOLD"
            out["reason"] = f"綜合 {score}/100 (中性)"
        else:
            out["vote"] = "AVOID"
            out["reason"] = f"綜合 {score}/100 (弱)"
    except Exception as e:
        out["reason"] = f"err: {e}"[:60]
    return out


def vote_for_stock(stock_id: str, market: str = "auto") -> Dict:
    """對一支股跑所有策略 → 加總投票 → 綜合 verdict."""
    if market == "auto":
        market = _detect_market(stock_id)

    voters = [
        _vote_minervini,
        _vote_canslim,
        _vote_weinstein,
        _vote_buffett,
        _vote_speculation,
        _vote_entry_evaluator,
    ]
    details = []
    buy_w = 0.0; avoid_w = 0.0; hold_w = 0.0
    buy_n = 0; avoid_n = 0; hold_n = 0; na_n = 0

    for voter in voters:
        try:
            d = voter(stock_id, market)
        except Exception as e:
            d = {"strategy": voter.__name__, "vote": "—", "reason": f"err: {e}"[:60]}
        details.append(d)
        _strat = (d.get("strategy") or "").split()  # Bug fix: 空 strategy 時 "".split()[0] 會 IndexError
        strat_name = _strat[0] if _strat else ""     # "Minervini SEPA+VCP" → "Minervini"
        w = STRATEGY_WEIGHTS.get(strat_name, 1.0)
        if d["vote"] == "BUY":
            buy_w += w; buy_n += 1
        elif d["vote"] == "AVOID":
            avoid_w += w; avoid_n += 1
        elif d["vote"] == "HOLD":
            hold_w += w; hold_n += 1
        else:
            na_n += 1

    total_valid = buy_n + hold_n + avoid_n
    if total_valid == 0:
        verdict, verdict_emoji = "N/A", "⚪"
    else:
        # 用加權分數判 (避免 N/A 影響)
        if buy_w >= avoid_w + 2 and buy_n >= 3:
            verdict = "STRONG_BUY"; verdict_emoji = "🟢🟢"
        elif buy_w > avoid_w + 1:
            verdict = "BUY"; verdict_emoji = "🟢"
        elif avoid_w > buy_w + 1:
            verdict = "AVOID"; verdict_emoji = "🔴"
        else:
            verdict = "HOLD"; verdict_emoji = "🟡"

    # summary 一句話
    if verdict == "STRONG_BUY":
        summary = f"綜合 {buy_n}/{total_valid} 強烈看多, 多策略共識, 適合中長線買進"
    elif verdict == "BUY":
        summary = f"綜合 {buy_n}/{total_valid} 看多, 可考慮進場, 留意停損"
    elif verdict == "HOLD":
        summary = f"綜合 {buy_n} 多 / {avoid_n} 空, 訊號分歧, 觀望或小倉位"
    elif verdict == "AVOID":
        summary = f"綜合 {avoid_n}/{total_valid} 看空, 不適合進場"
    else:
        summary = "資料不足, 無法綜合判斷"

    return {
        "stock_id": stock_id,
        "market": market,
        "buy_votes": buy_n,
        "hold_votes": hold_n,
        "avoid_votes": avoid_n,
        "na_votes": na_n,
        "total_strategies": len(voters),
        "buy_weight": round(buy_w, 2),
        "avoid_weight": round(avoid_w, 2),
        "verdict": verdict,
        "verdict_emoji": verdict_emoji,
        "details": details,
        "summary": summary,
    }


def fmt_ensemble_for_tg(vote: Dict) -> str:
    """格式化推播用. 一個 vote dict → 多行 HTML."""
    if not vote:
        return ""
    import html as _html
    def _esc(s): return _html.escape(str(s) if s is not None else "", quote=False)
    sid = _esc(vote.get("stock_id", ""))
    emoji = vote.get("verdict_emoji", "")
    verdict = vote.get("verdict", "")
    summary = _esc(vote.get("summary", ""))
    lines = [
        f"{emoji} <code>{sid}</code> <b>{verdict}</b>",
        f"  <i>{summary}</i>",
    ]
    for d in vote.get("details", []):
        v = d.get("vote", "—")
        strat = _esc(d.get("strategy", ""))
        reason = _esc(d.get("reason", "")[:50])
        mark = {"BUY": "✓", "HOLD": "○", "AVOID": "✗", "—": "·"}.get(v, "·")
        lines.append(f"  {mark} {strat}: {reason}")
    return "\n".join(lines)
