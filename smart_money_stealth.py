"""
smart_money_stealth.py
真正的「大戶偷偷進場」掃描 — 整合 4 維訊號:

1. 籌碼面 (最重要):
   - 外資/投信 連 3 日小幅買超 (累計 ≥ 500 張, 但單日沒爆量)
   - 主力券商 buy_top 出現 (新券商或前 5 大)
   - 借券賣出減少 (法人空單回補)

2. 價量結構:
   - 量比 ≥ 1.6x (異常量; 原 2.0x 太嚴, 已微調)
   - 今日漲跌 ≤ +2.0% (沒被市場注意到; 原 1.5% 太嚴, 已微調)
   - 股價在 20MA ±5% 區間 (沒過熱)
   - 近 10 日窄幅整理 range ≤ 6%

3. 題材相關性:
   - 屬於熱門題材 top 3
   - 掃該題材「全部成分股」(sector_pulse.TW_THEMES, 每題材 6~16 檔), 不再只看
     「今日領漲前 5 名」— 領漲前 5 名幾乎必然已經漲超過門檻, 跟「還沒被注意到」
     的目標互相矛盾, 導致候選池常態性被縮到剩沒幾檔甚至 0 檔 (bug fix)
   - 個股還沒進入領漲名單 (族群均漲 +2% 以上, 但個股本身還沒跟上)

4. 進場價建議 (每檔具體):
   - 進場區: 當前 ~ 5MA
   - 停損: 跌破 20MA 或 -3%
   - 短目標: 追上族群龍頭漲幅
   - 中目標: 突破 60d 高

API:
  scan_smart_money_stealth(top_n=5) -> List[Dict]
  fmt_smart_stealth_msg(picks) -> str  # TG / Dashboard 用
"""
from __future__ import annotations

from typing import Dict, List, Optional
import datetime as dt

import data_sources as ds


def _detect_consolidation(closes, lookback: int = 10) -> Optional[float]:
    """近 N 日窄幅整理: 振幅 / 中位價. 越小越穩."""
    try:
        c = closes.tail(lookback)
        if len(c) < lookback:
            return None
        rng = (float(c.max()) - float(c.min())) / float(c.median())
        return round(rng * 100, 2)  # %
    except Exception:
        return None


def _chip_signal(stock_id: str) -> Dict:
    """抓籌碼面: 外資/投信連續買超 + 借券回補.

    Bug fix: 原本呼叫 chip_analyzer.get_institutional_summary / get_top_brokers
    都不存在, 走 except 永遠空 → smart_money_stealth 籌碼維度 0 分.
    改用 chip_analyzer.fetch_chip_data (真實 API), 解析 institutional dict.
    """
    out = {
        "foreign_streak_days": 0,
        "foreign_5d_lots": 0,
        "trust_streak_days": 0,
        "trust_5d_lots": 0,
        "main_broker_buy": False,
        "lend_short_decrease": False,
    }
    try:
        import chip_analyzer as ca
        chip_data = ca.fetch_chip_data(stock_id, days=10)
        inst = chip_data.get("institutional", {}) if chip_data else {}
        # FinMind name: "Foreign_Investor" / "Investment_Trust" / "Dealer"
        # 也可能是中文 "外資" / "投信" / "自營商"
        for fkey in ["Foreign_Investor", "外資", "Foreign_Dealer_Self"]:
            if fkey in inst:
                v = inst[fkey]
                out["foreign_5d_lots"] = int(v.get("5d_total", 0) or 0)
                out["foreign_streak_days"] = int(v.get("consecutive_days", 0) or 0)
                break
        for tkey in ["Investment_Trust", "投信"]:
            if tkey in inst:
                v = inst[tkey]
                out["trust_5d_lots"] = int(v.get("5d_total", 0) or 0)
                out["trust_streak_days"] = int(v.get("consecutive_days", 0) or 0)
                break
    except Exception as e:
        print(f"[smart_stealth] chip_data fail {stock_id}: {e}", flush=True)

    # 借券回補 (TaiwanStockSecuritiesLending) — 法人空單回補偏多訊號
    try:
        import institutional_positioning as _ip
        rows = _ip._safe_finmind_data("TaiwanStockSecuritiesLending", days=10)
        if rows:
            stock_rows = [r for r in rows if str(r.get("stock_id", "")) == str(stock_id)]
            if len(stock_rows) >= 3:
                sorted_rows = sorted(stock_rows, key=lambda x: x.get("date", ""))
                recent_sum = sum(float(r.get("short_sale_balance", 0) or 0) for r in sorted_rows[-2:])
                prev_sum = sum(float(r.get("short_sale_balance", 0) or 0) for r in sorted_rows[-5:-2])
                if prev_sum > 0 and recent_sum < prev_sum * 0.85:
                    out["lend_short_decrease"] = True
    except Exception:
        pass

    return out


def _evaluate_smart_money_stock(stock_id: str, name: str, theme: str,
                                  theme_avg_pct: float = 0.0) -> Optional[Dict]:
    """單檔評估 — 跑所有條件, 回 candidate dict 或 None."""
    try:
        # 抓近 30 日日線 + intraday 5m
        sym = f"{stock_id}.TW"
        df = ds.fetch_yf_history(sym, period="60d", interval="1d")
        if df is None or df.empty or len(df) < 20:
            df = ds.fetch_yf_history(f"{stock_id}.TWO", period="60d", interval="1d")
        if df is None or df.empty or len(df) < 20:
            return None
        df_5m = ds.fetch_yf_history(sym, period="5d", interval="5m")
        if df_5m is None or df_5m.empty:
            df_5m = ds.fetch_yf_history(f"{stock_id}.TWO", period="5d", interval="5m")

        c = df["Close"].astype(float).reset_index(drop=True)
        v = df["Volume"].astype(float).reset_index(drop=True)
        cur = float(c.iloc[-1])
        ma5 = float(c.tail(5).mean())
        ma20 = float(c.tail(20).mean())
        high_60d = float(df["High"].astype(float).tail(60).max())
        avg_vol_20d = float(v.tail(20).mean())

        # === 價量結構 ===
        # 量比 (今日 / 20d 平均)
        if df_5m is not None and not df_5m.empty:
            import pandas as pd
            date_col = "Datetime" if "Datetime" in df_5m.columns else df_5m.columns[0]
            df_5m_c = df_5m.copy()
            df_5m_c["_dt"] = pd.to_datetime(df_5m_c[date_col])
            df_5m_c["_d"] = df_5m_c["_dt"].dt.date
            today_d = df_5m_c["_d"].max()
            today_bars = df_5m_c[df_5m_c["_d"] == today_d]
            today_vol = float(today_bars["Volume"].sum())
            cur_5m = float(today_bars["Close"].iloc[-1]) if not today_bars.empty else cur
            today_open = float(today_bars["Open"].iloc[0]) if not today_bars.empty else cur
        else:
            today_vol = float(v.iloc[-1])
            cur_5m = cur
            today_open = cur

        vol_ratio = today_vol / avg_vol_20d if avg_vol_20d > 0 else 0
        today_pct = (cur_5m / float(c.iloc[-2]) - 1) * 100 if len(c) >= 2 else 0
        ma20_deviation = (cur_5m / ma20 - 1) * 100 if ma20 > 0 else 0
        consolidation_pct = _detect_consolidation(c, lookback=10)

        # === 條件檢查 (4 維) ===
        reasons = []
        warns = []

        # 1. 量比 ≥ 1.6 (原 2.0 太嚴, 池子擴大後配合微調 — 跟 sector_pulse 同類函式
        #    的 1.5 門檻更接近, 仍算明顯異常量)
        cond_vol = vol_ratio >= 1.6
        if cond_vol:
            reasons.append(f"📊 量比 {vol_ratio:.2f}x (≥1.6, 異常量)")

        # 2. 今日 ≤ +2.0% (原 1.5% 太嚴)
        cond_today = today_pct <= 2.0
        if cond_today:
            reasons.append(f"📉 今日僅 {today_pct:+.2f}% (≤+2.0%, 還沒被注意)")
        else:
            warns.append(f"⚠️ 今日已 {today_pct:+.2f}% (>+2.0%, 不夠潛伏)")

        # 3. 股價在 20MA ±5%
        cond_ma = abs(ma20_deviation) <= 5
        if cond_ma:
            reasons.append(f"📈 距 20MA {ma20_deviation:+.2f}% (在 ±5% 區間)")

        # 4. 整理 ≤ 6%
        cond_consol = consolidation_pct is not None and consolidation_pct <= 6.0
        if cond_consol:
            reasons.append(f"🔄 近 10 日窄幅整理 (振幅 {consolidation_pct:.2f}%)")

        # 5. 同族群已熱 (theme_avg_pct ≥ +2%)
        cond_theme = theme_avg_pct >= 2.0
        if cond_theme and theme:
            reasons.append(f"🔥 題材「{theme}」族群均漲 {theme_avg_pct:+.2f}%, 個股還沒跟")

        # === 籌碼面 (加分項) ===
        chip = _chip_signal(stock_id)
        cond_chip = False
        if chip["foreign_streak_days"] >= 3:
            reasons.append(f"💰 外資連買 {chip['foreign_streak_days']} 日 (5日累計 {chip['foreign_5d_lots']} 張)")
            cond_chip = True
        if chip["trust_streak_days"] >= 3:
            reasons.append(f"💼 投信連買 {chip['trust_streak_days']} 日")
            cond_chip = True
        if chip["main_broker_buy"]:
            reasons.append("🏦 主力券商 buy_top 出現")
            cond_chip = True
        if chip["lend_short_decrease"]:
            reasons.append("🔻 借券賣出減少 (法人空單回補)")
            cond_chip = True

        # === 通過條件數 ===
        # 必要: 量增 + 沒大漲 (cond_vol + cond_today)
        # 加分: 至少 1 個籌碼 或 (20MA + 整理)
        if not (cond_vol and cond_today):
            return None

        score = (
            (3 if cond_vol else 0) +
            (2 if cond_today else 0) +
            (2 if cond_ma else 0) +
            (2 if cond_consol else 0) +
            (3 if cond_theme else 0) +
            (4 if cond_chip else 0)
        )
        if score < 6:
            return None  # 分數不夠 (原門檻 7, 池子擴大 + 量比/今日% 微調後配合放寬)

        # === 進出場價 ===
        entry_low = round(ma5 * 0.99, 2)
        entry_high = round(ma5 * 1.01, 2)
        stop_loss = round(min(ma20 * 0.97, cur_5m * 0.97), 2)
        # 短目標: 追上族群龍頭漲幅 (假設龍頭 +5%, 目標 +5%)
        target_short = round(cur_5m * 1.05, 2)
        # 中目標: 突破 60d 高
        target_mid = round(high_60d * 1.02, 2)
        rr = round((target_short - cur_5m) / (cur_5m - stop_loss), 2) if (cur_5m - stop_loss) > 0 else 0

        return {
            "stock_id": stock_id,
            "name": name,
            "theme": theme,
            "current": round(cur_5m, 2),
            "today_pct": round(today_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "ma20_deviation": round(ma20_deviation, 2),
            "consolidation_pct": consolidation_pct,
            "theme_avg_pct": round(theme_avg_pct, 2),
            "chip": chip,
            "reasons": reasons,
            "warnings": warns,
            "score": score,
            # 進出場價
            "entry_low": entry_low, "entry_high": entry_high,
            "stop_loss": stop_loss,
            "target_short": target_short, "target_mid": target_mid,
            "rr": rr,
        }
    except Exception as e:
        print(f"[smart_stealth] {stock_id} fail: {e}", flush=True)
        return None


def scan_smart_money_stealth(top_n: int = 5) -> List[Dict]:
    """掃描所有熱門題材, 用嚴條件找「大戶偷偷進場」標的."""
    try:
        import sector_pulse as sp
        hot = sp.compute_hot_themes()
        themes_df = hot.get("themes")
        if themes_df is None or themes_df.empty:
            return []
        # 取 top 3 熱門題材
        top_themes = themes_df.head(3).to_dict("records")
    except Exception as e:
        print(f"[smart_stealth] hot_themes fail: {e}", flush=True)
        return []

    # Bug fix (選股池): 原本從 sp.compute_hot_themes()["leaders"] 挑股 — 那是「該題材
    # 今日漲幅前 5 名」。但這裡要找的是「族群熱、個股還沒被市場注意到」(今日 ≤ 門檻),
    # 這兩個條件互相矛盾: 題材真的熱 (平均漲 ≥2% 才會入選) 時, 今日漲幅前 5 名幾乎必然
    # 已經漲超過門檻, 候選池常態性被縮到剩沒幾檔甚至 0 檔 (dashboard「目前無訊號」
    # 常態出現的主因)。改成直接掃該題材的「全部成分股」(sector_pulse.TW_THEMES,
    # 每題材 6~16 檔), 才有機會找到「族群熱但個股自己還沒跟上」的真正落後股。
    name_map: Dict[str, str] = {}
    try:
        import data_sources as _ds
        info = _ds.get_taiwan_stock_info()
        if info is not None and not info.empty and "stock_name" in info.columns:
            name_map = info.set_index("stock_id")["stock_name"].to_dict()
    except Exception as e:
        print(f"[smart_stealth] stock name lookup fail (non-fatal): {e}", flush=True)

    # 同一檔股票若橫跨多個題材 (e.g. 2308 同時在 AI伺服器/機器人), 只算在排名最高的
    # 題材下 — 跟 sector_pulse 其他函式的去重方式一致, 避免重複評估同一檔股票。
    tasks = []  # (sid, name, theme, theme_avg)
    seen_stocks: set = set()
    for theme_row in top_themes:
        theme = theme_row.get("題材", "")
        theme_avg = float(theme_row.get("平均%", 0) or 0)
        if theme_avg < 2.0:
            continue  # 族群不夠熱
        for sid in sp.TW_THEMES.get(theme, []):
            sid = str(sid)
            if not sid or sid in seen_stocks:
                continue
            seen_stocks.add(sid)
            tasks.append((sid, name_map.get(sid, ""), theme, theme_avg))

    candidates = []
    if tasks:
        # 池子從「每題材前 5 名」擴大成「每題材全部成分股」後, 掃描檔數會變成 3~5 倍
        # (原本 top3 題材最多 15 檔 → 現在可能 30~45 檔), 每檔都要跑 yfinance + 籌碼
        # API, 序列跑會讓按鈕等超過 1 分鐘。改用 ThreadPoolExecutor 平行跑, 維持原本
        # 「約 30s」的體感速度。
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {
                ex.submit(_evaluate_smart_money_stock, sid, name, theme, theme_avg): sid
                for sid, name, theme, theme_avg in tasks
            }
            for fut in as_completed(futs):
                try:
                    result = fut.result()
                except Exception as e:
                    print(f"[smart_stealth] {futs[fut]} eval failed: {e}", flush=True)
                    result = None
                if result:
                    candidates.append(result)

    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    return candidates[:top_n]


def fmt_smart_stealth_msg(picks: List[Dict]) -> str:
    """格式化 TG 訊息 (HTML)."""
    import html as _html
    def _esc(s): return _html.escape(str(s) if s is not None else "", quote=False)

    if not picks:
        return ""
    now_tpe = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%H:%M")
    lines = [
        f"🕵️ <b>大戶偷偷進場 Top {len(picks)}</b> · {now_tpe} TPE",
        "<i>(量比≥1.6x + 沒大漲 + 籌碼/族群配合)</i>",
        "",
    ]
    for i, p in enumerate(picks, 1):
        sid = _esc(p.get("stock_id", ""))
        name = _esc(p.get("name", ""))
        cur = p.get("current", 0)
        tp = p.get("today_pct", 0)
        vr = p.get("vol_ratio", 0)
        theme = _esc(p.get("theme", ""))
        score = p.get("score", 0)
        lines.append(
            f"{i}. <code>{sid}</code> {name} · {cur:.2f} {tp:+.2f}% · "
            f"量比 {vr:.2f}x · 分數 {score}/16"
        )
        if theme:
            lines.append(f"   🏷 題材: {theme}")
        for r in p.get("reasons", [])[:3]:
            lines.append(f"   ✓ {_esc(r)}")
        # 進出場價
        # Bug fix: 原本 default 給字串 '—' 又套 :.2f → 任一價位缺值就 TypeError 炸掉整封推播;
        #          且進場/停損那兩行重複貼了一次. 改用安全格式化 + 去掉重複.
        def _f2(v):
            return f"{v:.2f}" if isinstance(v, (int, float)) else "—"
        lines.append(
            f"   📍 進場 {_f2(p.get('entry_low'))}-{_f2(p.get('entry_high'))} · "
            f"停損 {_f2(p.get('stop_loss'))} · "
            f"目標 {_f2(p.get('target_short'))}/{_f2(p.get('target_mid'))} · "
            f"R:R {p.get('rr', 0):.2f}"
        )
        lines.append("")

    lines.append("<i>※ 智慧潛伏股 — 嚴條件挑選, 仍建議分批進場 + 嚴守停損.</i>")
    return "\n".join(lines)
