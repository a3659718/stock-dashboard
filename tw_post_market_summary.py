"""
tw_post_market_summary.py
TPE 16:00 台股盤後總結推播.

內容:
  1. 加權今日 (OHLC + 漲跌 + 區間)
  2. 強弱族群 (top 3 / bottom 3)
  3. 上漲家數 vs 下跌家數
  4. 龍頭股當日表現 (2330 / 2317 / 2454)
  5. AI 隔日策略建議 (Gemini)

API:
  build_post_market_msg() -> str
"""
from __future__ import annotations

from typing import Dict, List


def _twii_snap() -> Dict:
    try:
        import data_sources as ds
        df = ds.fetch_yf_history("^TWII", period="3d", interval="1d")
        if df is None or df.empty:
            return {}
        # Bug fix (2026-08): 這是「今日總結」推播裡的大盤漲跌%, 之前完全沒檢查
        # yfinance 回來的最後一筆日線是不是真的「今天」— 如果剛好卡到延遲 (跟
        # index_alerts.py._fetch_systemic_snapshot 註解講的同一種 yfinance 延遲),
        # 會把前一交易日的收盤/漲跌幅原封不動當「今日」推出去, 使用者看不出來。
        # 用共用的 data_sources.check_daily_freshness() 檢查, 過期就標記 stale,
        # 由 build_post_market_msg() 在訊息開頭加警語, 而不是悄悄照送。
        # Bug fix (2026-09-07): 預設 max_staleness_days=2 表示「落後 1 天仍算新鮮」,
        # 但這支是在 tw_close (TPE 15:03, 已收盤) 跑的, 落後 1 天正是它要防的那一種
        # (yfinance 還沒更新今日日線 -> 把昨天的收盤/漲跌幅當今日推出去)。
        is_fresh, latest_d = ds.check_daily_freshness(df, max_staleness_days=1)
        c = df["Close"].astype(float)
        o = df["Open"].astype(float)
        h = df["High"].astype(float)
        l = df["Low"].astype(float)
        cur = float(c.iloc[-1])
        op = float(o.iloc[-1])
        hi = float(h.iloc[-1])
        lo = float(l.iloc[-1])
        prev = float(c.iloc[-2]) if len(c) >= 2 else cur
        return {
            "current": round(cur, 2),
            "open": round(op, 2),
            "high": round(hi, 2),
            "low": round(lo, 2),
            "prev_close": round(prev, 2),
            "pct_vs_prev": round((cur / prev - 1) * 100, 2) if prev > 0 else 0,
            "range_pct": round((hi - lo) / lo * 100, 2) if lo > 0 else 0,
            "stale": not is_fresh,
            "latest_date": str(latest_d) if latest_d else None,
        }
    except Exception:
        return {}


def _leader_stocks() -> List[Dict]:
    """龍頭股當日表現."""
    leaders = ["2330", "2317", "2454", "2412", "2308", "6669"]
    out = []
    try:
        import data_sources as ds
        for sid in leaders:
            try:
                df = ds.fetch_yf_history(f"{sid}.TW", period="3d", interval="1d")
                if df is None or df.empty:
                    df = ds.fetch_yf_history(f"{sid}.TWO", period="3d", interval="1d")
                if df is None or df.empty or len(df) < 2:
                    continue
                # Bug fix (2026-08): 同 _twii_snap() 的新鮮度檢查 — 個股跟指數
                # 有可能各自延遲的程度不同, 這裡各檔獨立檢查, 不能只靠 TWII
                # 那一筆代表全部。
                is_fresh, _latest_d = ds.check_daily_freshness(df, max_staleness_days=1)
                c = df["Close"].astype(float)
                cur = float(c.iloc[-1])
                prev = float(c.iloc[-2])
                pct = (cur / prev - 1) * 100 if prev > 0 else 0
                out.append({
                    "stock_id": sid,
                    "current": round(cur, 2),
                    "today_pct": round(pct, 2),
                    "stale": not is_fresh,
                })
            except Exception:
                continue
    except Exception:
        pass
    return out


def _sector_pulse_summary() -> Dict:
    """sector_pulse 強弱族群 top 3 / bottom 3."""
    try:
        import sector_pulse as sp
        data = sp.compute_strong_sectors(top_n=100)
        sec_df = data.get("sectors")
        if sec_df is None or sec_df.empty:
            return {}
        ind_col = "industry_category" if "industry_category" in sec_df.columns else None
        if not ind_col:
            return {}
        top = sec_df.head(3).to_dict("records")
        bot = sec_df.tail(3).iloc[::-1].to_dict("records")
        return {
            "top3": [
                {
                    "sector": r.get(ind_col, "—"),
                    "avg": round(float(r.get("avg_change", 0) or 0), 2),
                    "up_ratio": round(float(r.get("up_ratio", 0) or 0) * 100, 0),
                }
                for r in top
            ],
            "bot3": [
                {
                    "sector": r.get(ind_col, "—"),
                    "avg": round(float(r.get("avg_change", 0) or 0), 2),
                }
                for r in bot
            ],
            "total_stocks": int(sec_df["n"].sum()) if "n" in sec_df.columns else 0,
        }
    except Exception as e:
        print(f"[post_market] sector_pulse fail: {e}", flush=True)
        return {}


def _market_breadth() -> Dict:
    """上漲家數 / 下跌家數 (用 sector_pulse 數據估)."""
    try:
        import sector_pulse as sp
        data = sp.compute_strong_sectors(top_n=200)
        stocks_df = data.get("stocks")
        if stocks_df is None or stocks_df.empty or "今日%" not in stocks_df.columns:
            return {}
        up_n = int((stocks_df["今日%"] > 0).sum())
        dn_n = int((stocks_df["今日%"] < 0).sum())
        flat_n = int((stocks_df["今日%"] == 0).sum())
        return {"up": up_n, "down": dn_n, "flat": flat_n,
                "up_ratio_pct": round(up_n / (up_n + dn_n) * 100, 0) if (up_n + dn_n) > 0 else 0}
    except Exception:
        return {}


def _gemini_next_day_advice(twii: Dict, sectors: Dict, breadth: Dict, leaders: List) -> str:
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return ""
        lines = ["以下是台股今日盤後數據, 請依下列項目給精簡繁中結論:"]
        if twii.get("current"):
            lines.append(f"加權 收 {twii['current']:.2f}, {twii.get('pct_vs_prev', 0):+.2f}%, "
                          f"區間 {twii.get('low', 0):.2f}-{twii.get('high', 0):.2f}")
        if breadth:
            lines.append(f"上漲 {breadth.get('up', 0)} / 下跌 {breadth.get('down', 0)} "
                          f"(上漲比 {breadth.get('up_ratio_pct', 0)}%)")
        if sectors.get("top3"):
            top_parts = [f"{s['sector']} {s['avg']:+.2f}%" for s in sectors["top3"][:3]]
            lines.append(f"強勢族群: {', '.join(top_parts)}")
        if sectors.get("bot3"):
            bot_parts = [f"{s['sector']} {s['avg']:+.2f}%" for s in sectors["bot3"][:3]]
            lines.append(f"弱勢族群: {', '.join(bot_parts)}")
        lines.append("")
        lines.append("(1) 今日盤勢解讀 (一句)")
        lines.append("(2) 隔日操作建議 + 具體可留意台股個股 (代號, 2-3 檔)")
        lines.append("(3) 🇺🇸 今晚美股可留意個股 (ticker, 對應台股連動族群, 2-3 檔)")
        lines.append("(4) 留意風險 (一句)")
        lines.append("個股務必含代號/ticker; 美股、台股都要給; 聚焦結論不要列數據。")
        prompt = "\n".join(lines)
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None:
            return ""
        resp = model.generate_content(prompt)
        return (resp.text or "").strip() if resp else ""
    except Exception as e:
        print(f"[post_market] gemini fail: {e}", flush=True)
        return ""


def _post_tldr(twii: Dict, breadth: Dict) -> str:
    """盤後一句話定調 — 優先 Gemini 潤飾, 失敗/無 quota 自動退回規則版。"""
    _rule = _post_tldr_rule(twii, breadth)
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return _rule
        _pct = twii.get("pct_vs_prev", 0) if twii else 0
        _rng = twii.get("range_pct", 0) if twii else 0
        _ur = breadth.get("up_ratio_pct") if breadth else None
        _urs = f", 上漲比 {_ur:.0f}%" if _ur is not None else ""
        prompt = (
            f"你是台股策略師。今日台股收盤: 加權 {_pct:+.2f}% (振幅 {_rng:.2f}%){_urs}。"
            "用繁體中文寫【一句話】做今日定調 + 隔日方向: 多空氛圍、隔日偏多偏空、該注意什麼。"
            "要具體可行動、40 字內、不要開場白或引號、只回那一句。"
        )
        from ai_analyzer import _get_model
        _m = _get_model()
        if _m is None:
            return _rule
        _resp = _m.generate_content(prompt)
        _txt = (_resp.text or "").strip() if _resp else ""
        _txt = _txt.replace("\n", " ").strip().strip('「」"\'` ')
        return _txt if _txt else _rule
    except Exception as _e:
        print(f"[post_market] gemini tldr fail: {_e}", flush=True)
        return _rule


def _post_tldr_rule(twii: Dict, breadth: Dict) -> str:
    """規則版盤後一句話 (Gemini 後備) — 依加權漲跌 + 上漲比。"""
    pct = twii.get("pct_vs_prev", 0) if twii else 0
    up_ratio = breadth.get("up_ratio_pct") if breadth else None
    br = f", 上漲比 {up_ratio:.0f}%" if up_ratio is not None else ""
    if pct >= 1.0:
        return f"加權 {pct:+.2f}% 收紅{br} → 多方主導, 隔日偏續強 (留意美股是否同步)"
    if pct >= 0.3:
        return f"加權 {pct:+.2f}% 小漲{br} → 偏多但力道普通, 隔日看美股臉色"
    if pct <= -1.0:
        return f"加權 {pct:+.2f}% 收黑{br} → 空方主導, 隔日偏弱, 保守 / 減碼優先"
    if pct <= -0.3:
        return f"加權 {pct:+.2f}% 小跌{br} → 偏空整理, 隔日觀望"
    return f"加權 {pct:+.2f}% 近平盤{br} → 盤整待變, 看美股 + 籌碼再決定方向"


def build_post_market_msg() -> str:
    """TPE 16:00 台股今日總結 TG 訊息."""
    # Bug fix: 台股假日不該推 (內容會是前一交易日資料但講「今日」)
    try:
        import holiday_check
        if holiday_check.is_market_closed_today("TW"):
            return ""  # 空字串 = caller 不會 send
    except Exception:
        pass

    try:
        from notifier import _esc, _truncate_tg_msg
    except Exception:
        def _esc(x): return str(x)
        _truncate_tg_msg = lambda x: x

    twii = _twii_snap()
    sectors = _sector_pulse_summary()
    breadth = _market_breadth()
    leaders = _leader_stocks()

    # 註: 此段已併入 15:00 台股盤後總結推播 (tw_close), 作為「今日總結 + 隔日策略」區塊。
    lines = ["📊 <b>今日總結 + 隔日策略</b>", "━━━━━━━━━━━━━━━━━"]
    # Bug fix (2026-08): 大盤 / 龍頭股數據新鮮度警語 — 只要 twii 或任一龍頭股被
    # check_daily_freshness() 標記過期, 就在最上面明講, 不要讓使用者誤以為
    # 「加權 +0.8%」是今天的數字, 結果其實是昨天的 (yfinance 延遲時會這樣)。
    if twii.get("stale") or any(ld.get("stale") for ld in leaders):
        d = twii.get("latest_date") or "?"
        lines.append(f"⚠️ <i>部分數據可能延遲更新 (資料日期: {d}), 請以看盤軟體實際報價為準</i>")
        lines.append("")
    # 一句話定調 (置頂, 最醒目)
    _tldr = _post_tldr(twii, breadth)
    if _tldr:
        lines.append(f"🧭 <b>一句話</b>:{_tldr}")
        lines.append("")
    # 加權
    if twii.get("current"):
        pct = twii.get("pct_vs_prev", 0)
        tag = "🟢" if pct >= 0 else "🔴"
        lines.append(
            f"{tag} 加權 收 <b>{twii['current']:,.2f}</b> "
            f"({pct:+.2f}%)"
        )
        lines.append(
            f"  區間 {twii.get('low', 0):,.2f} - {twii.get('high', 0):,.2f} "
            f"(振幅 {twii.get('range_pct', 0):.2f}%)"
        )
    # breadth
    if breadth:
        lines.append(
            f"📈 上漲 <b>{breadth.get('up', 0)}</b> / 下跌 {breadth.get('down', 0)} "
            f"(上漲比 {breadth.get('up_ratio_pct', 0):.0f}%)"
        )
    lines.append("")
    # 強弱族群
    if sectors.get("top3"):
        # Bug fix (2026-09-07): top3 只是 sec_df.head(3), 沒有 > 0 過濾。全盤下跌日
        # 會出現「🚀 強勢族群 Top 3 / ✅ 航運業 均 -3.20%」這種自相矛盾的行。
        _all_neg = all((s.get("avg") or 0) < 0 for s in sectors["top3"])
        lines.append("📉 <b>相對抗跌族群 Top 3</b>" if _all_neg else "🚀 <b>強勢族群 Top 3</b>")
        for s in sectors["top3"]:
            _tag = "✅" if (s.get("avg") or 0) >= 0 else "🔻"
            lines.append(
                f"  {_tag} {_esc(s['sector'])} 均 <b>{s['avg']:+.2f}%</b> "
                f"(上漲 {s['up_ratio']:.0f}%)"
            )
    if sectors.get("bot3"):
        bot_line = " · ".join(f"{_esc(s['sector'])} {s['avg']:+.2f}%" for s in sectors["bot3"])
        lines.append(f"📉 弱勢族群: {bot_line}")
    lines.append("")
    # 龍頭股
    if leaders:
        lines.append("🏆 <b>龍頭股表現</b>")
        for ld in leaders:
            sid = _esc(ld.get("stock_id", ""))
            pct = ld.get("today_pct", 0)
            tag = "🟢" if pct >= 0 else "🔴"
            stale_mark = " ⚠️" if ld.get("stale") else ""
            lines.append(
                f"  {tag} <code>{sid}</code> {ld.get('current', 0):,.2f} <b>{pct:+.2f}%</b>{stale_mark}"
            )
        lines.append("")
    # === 新增: 今日決策摘要 (微台/台指期操作專用) ===
    try:
        decision = _build_decision_recap(twii, sectors, breadth, leaders)
        if decision:
            lines.append("━━━━━━━ 🎯 今日決策摘要 ━━━━━━━")
            lines.append(decision)
            lines.append("")
    except Exception as _de:
        print(f"[post_market] decision_recap fail: {_de}", flush=True)

    # === 新增 (#8): 持倉 RSI 背離 → 只給「動作建議」, 不顯示指標細節 ===
    try:
        import rsi_divergence as _rd
        divs = _rd.scan_holdings_for_divergence()
        if divs:
            bear = [d for d in divs if d.get("type") == "bearish"]
            bull = [d for d in divs if d.get("type") == "bullish"]
            if bear or bull:
                lines.append("━━━━━━━ 🚨 持倉動作建議 ━━━━━━━")
                for d in bear:
                    sid = _esc(d.get("symbol", ""))
                    nm = _esc(d.get("stock_name", ""))
                    strength = d.get("strength", 1)
                    if strength >= 2:
                        lines.append(f"  ⚠️ <code>{sid}</code> {nm} <b>建議減碼</b> (動能衰竭)")
                    else:
                        lines.append(f"  🟡 <code>{sid}</code> {nm} <b>留意減碼</b> (動能轉弱)")
                for d in bull:
                    sid = _esc(d.get("symbol", ""))
                    nm = _esc(d.get("stock_name", ""))
                    strength = d.get("strength", 1)
                    if strength >= 2:
                        lines.append(f"  ✅ <code>{sid}</code> {nm} <b>反彈在即</b> (可加碼)")
                    else:
                        lines.append(f"  🔺 <code>{sid}</code> {nm} <b>可留意反彈</b>")
                lines.append("")
    except Exception as _re:
        print(f"[post_market] rsi_divergence fail: {_re}", flush=True)

    # === 整合: watchlist 觸發摘要 (整合 watchlist_triggers, 不再盤中推) ===
    try:
        import watchlist_store as _ws
        state = _ws.load_monitor_state()
        wt_today = state.get("watchlist_triggers_today") or []
        # 過濾今天
        import datetime as _dt
        # Bug fix: dt.date.today() 是伺服器 UTC 日期。目前 tw_close/tw_post_market
        # 只在 TPE 15:03 (UTC 07:03) 跑, 這個時間點 UTC 日期剛好等於 TPE 日期不會
        # 出錯, 但改成 TPE 日期跟 scripts/market_open_alert.py 寫入這批資料時用的
        # 日期一致, 之後排程時間調整或手動觸發也不會有邊界問題。
        today_str = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).date().strftime("%Y-%m-%d")
        wt_today = [t for t in wt_today if t.get("date") == today_str]
        if wt_today:
            lines.append("━━━━━━━ ⭐ Watchlist 觸發 ━━━━━━━")
            for t in wt_today[:5]:
                sid = _esc(t.get("stock_id", ""))
                # type_label (人類看得懂的中文標籤, 例如「現價跌破 X」) 是這次新增
                # 補存的欄位; 舊資料 / 補存失敗時退回原始 trigger_type 代碼字串。
                tt = _esc(t.get("type_label") or t.get("trigger_type", ""))
                cur = t.get("current", "—")
                val = t.get("value", "—")
                lines.append(f"  <code>{sid}</code> {tt} (現價 {cur} / 條件 {val})")
            if len(wt_today) > 5:
                lines.append(f"  ... 還有 {len(wt_today) - 5} 個")
            lines.append("")
    except Exception as _wte:
        print(f"[post_market] watchlist_today fail: {_wte}", flush=True)

    # === 新增 (#7): 組合風險檢查 — 若有持倉, 警示集中度 ===
    try:
        import portfolio_risk as _pr
        risk = _pr.analyze_portfolio_risk()
        if risk and risk.get("holdings_n", 0) > 0:
            warns = risk.get("warnings", [])
            # 過濾掉「分散度良好」(綠燈) — 只顯示有問題的
            real_warns = [w for w in warns if "🔴" in w or "🟡" in w]
            if real_warns:
                lines.append("━━━━━━━ ⚠️ 組合風險檢查 ━━━━━━━")
                for w in real_warns[:3]:
                    lines.append(f"  {w}")
                # 列前 2 大 sector
                top_sectors = risk.get("sectors", [])[:2]
                if top_sectors:
                    parts = [f"{s['sector']} {s['weight_pct']:.0f}% ({s['n']}檔)" for s in top_sectors]
                    lines.append(f"  📊 主要持倉: {' · '.join(parts)}")
                lines.append("")
    except Exception as _pe:
        print(f"[post_market] portfolio_risk fail: {_pe}", flush=True)

    # === 推播命中回顧 (近 30 日各訊號準確率) — 檢視推播準不準, 建立信任感 ===
    try:
        import signal_tracker as _sig
        _acc = _sig.fmt_accuracy_block(lookback_days=30)
        if _acc:
            lines.append("━━━━━━━ 🎯 推播命中回顧 (近 30 日) ━━━━━━━")
            lines.append(_acc)  # 已含 HTML 標記, 不再 _esc
            lines.append("")
    except Exception as _se:
        print(f"[post_market] signal_tracker fail: {_se}", flush=True)

    # Gemini advice
    gem = _gemini_next_day_advice(twii, sectors, breadth, leaders)
    if gem:
        lines.append("━━━━━━━ 🤖 Gemini 隔日策略 ━━━━━━━")
        lines.append(_esc(gem))
        lines.append("")
    lines.append("<i>※ 盤後總結, 用於規劃隔日策略. 留意美股隔夜變化.</i>")
    # 不再自己 truncate: 這封內容很豐富 (定調/族群/龍頭/決策/持倉/命中回顧/Gemini 隔日策略),
    # 超過 4096 會把最後面的 Gemini 整段砍掉。改回傳完整內容, 由 caller 用
    # notifier._split_tg_msg() 拆成多封送出 → 一個字都不掉。
    return "\n".join(lines).rstrip()


def _html_esc(s) -> str:
    if s is None:
        return ""
    import html
    return html.escape(str(s), quote=False)


def _build_decision_recap(twii: Dict, sectors: Dict, breadth: Dict, leaders: List) -> str:
    """產出「今日該做的 1-2 筆」+「明日方向」. 紀律專用."""
    lines = []
    pct = twii.get("pct_vs_prev", 0) if twii else 0
    range_pct = twii.get("range_pct", 0) if twii else 0
    if pct >= 1.0:
        market_today = f"🟢 今日大漲 {pct:+.2f}% (振幅 {range_pct:.2f}%)"
    elif pct >= 0.3:
        market_today = f"🟢 今日偏多 {pct:+.2f}%"
    elif pct <= -1.0:
        market_today = f"🔴 今日大跌 {pct:+.2f}% (振幅 {range_pct:.2f}%)"
    elif pct <= -0.3:
        market_today = f"🔴 今日偏空 {pct:+.2f}%"
    else:
        market_today = f"⚪ 今日盤整 {pct:+.2f}% (振幅 {range_pct:.2f}%)"
    lines.append(f"<b>大盤</b>: {market_today}")

    # 強弱族群
    if sectors:
        top3 = sectors.get("top3") or []
        bot3 = sectors.get("bot3") or []
        if top3:
            tops = ", ".join(f"{s['sector']} {s['avg']:+.2f}%" for s in top3[:3])
            lines.append(f"<b>強族</b>: {tops}")
        if bot3:
            bots = ", ".join(f"{s['sector']} {s['avg']:+.2f}%" for s in bot3[:3])
            lines.append(f"<b>弱族</b>: {bots}")

    # 行動
    lines.append("")
    lines.append("<b>🎬 今日該做的</b>")
    up_ratio = breadth.get("up_ratio_pct", 0) if breadth else 0
    if pct >= 1.0 and up_ratio >= 60:
        lines.append("  ✅ 順勢多單 (微台 / 強族龍頭), 上漲家數多, 多方有效")
    elif pct <= -1.0 and up_ratio <= 40:
        lines.append("  ✅ 順勢空單 (微台空 / 弱族領跌), 下跌家數多, 空方有效")
    elif abs(pct) < 0.3 and range_pct < 1.0:
        lines.append("  ⚪ 今日無明確機會, 觀望為佳")
    else:
        lines.append("  🟡 震盪日, 不追高不殺低, 等隔日方向明確再進場")

    lines.append("")
    lines.append("<b>🔮 明日方向</b>")
    if pct >= 1.0:
        lines.append("  續勢機率高, 但留意美股是否同步漲")
    elif pct <= -1.0:
        lines.append("  接續弱勢, 留意美股是否轉強")
    else:
        lines.append("  盤整待變, 看美股 + 籌碼面再決定方向")

    lines.append("")
    lines.append("<i>* 紀律: 一天最多 1-2 筆, 嚴守停損, 不追高不殺低</i>")

    return "\n".join(lines)
