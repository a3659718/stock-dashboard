"""
smart_money_stealth.py
真正的「大戶偷偷進場」掃描 — 整合 4 維訊號:

1. 籌碼面 (最重要):
   - 外資/投信 連 3 日小幅買超 (累計 ≥ 500 張, 但單日沒爆量)
   - 主力券商 buy_top 出現 (新券商或前 5 大)
   - 借券賣出減少 (法人空單回補)

2. 價量結構:
   - 量比 ≥ 2.0x (異常量, 不只 1.5x)
   - 今日漲跌 ≤ +1.5% (沒被市場注意到)
   - 股價在 20MA ±5% 區間 (沒過熱)
   - 近 10 日窄幅整理 range ≤ 6%

3. 題材相關性:
   - 屬於熱門題材 top 3
   - 同族群已有 2-3 支大漲 (確認真熱)
   - 個股還沒進入領漲名單 (族群均漲 +3% 但個股 +0~+2%)

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
    """抓籌碼面: 外資/投信連續買超 + 主力券商 + 借券."""
    out = {
        "foreign_streak_days": 0,    # 外資連續買超天數
        "foreign_5d_lots": 0,        # 外資 5 日累計 (張)
        "trust_streak_days": 0,
        "main_broker_buy": False,    # 主力券商 (前 5 大) 是否買超
        "lend_short_decrease": False,  # 借券賣出減少 (空單回補)
    }
    try:
        import chip_analyzer as ca
        # 外資+投信 5 日累計
        ins = ca.get_institutional_summary(stock_id, days=5)
        if ins:
            f5 = ins.get("foreign_5d_net", 0) or 0
            t5 = ins.get("trust_5d_net", 0) or 0
            out["foreign_5d_lots"] = int(f5)
            f_streak = ins.get("foreign_consecutive_days", 0) or 0
            t_streak = ins.get("trust_consecutive_days", 0) or 0
            out["foreign_streak_days"] = int(f_streak)
            out["trust_streak_days"] = int(t_streak)
    except Exception:
        pass

    try:
        import chip_analyzer as ca
        # 主力券商買賣超
        brokers = ca.get_top_brokers(stock_id, top_n=5, days=5) or {}
        buy_top = brokers.get("buy_top") or []
        if buy_top and len(buy_top) >= 1:
            out["main_broker_buy"] = True
    except Exception:
        pass

    try:
        # 借券賣出趨勢 — 從 short_interest_alert 借用
        import institutional_positioning as _ip
        rows = _ip._safe_finmind_data("TaiwanStockSecuritiesLending", days=10)
        if rows:
            stock_rows = [r for r in rows if str(r.get("stock_id", "")) == str(stock_id)]
            if len(stock_rows) >= 3:
                sorted_rows = sorted(stock_rows, key=lambda x: x.get("date", ""))
                # 比較最近 2 日 vs 前 3 日, 若下降 = 回補
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

        # 1. 量比 ≥ 2.0
        cond_vol = vol_ratio >= 2.0
        if cond_vol:
            reasons.append(f"📊 量比 {vol_ratio:.2f}x (≥2.0, 異常量)")

        # 2. 今日 ≤ +1.5%
        cond_today = today_pct <= 1.5
        if cond_today:
            reasons.append(f"📉 今日僅 {today_pct:+.2f}% (≤+1.5%, 還沒被注意)")
        else:
            warns.append(f"⚠️ 今日已 {today_pct:+.2f}% (>+1.5%, 不夠潛伏)")

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
        if score < 7:
            return None  # 分數不夠

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
        leaders_map = hot.get("leaders") or {}
        if themes_df is None or themes_df.empty:
            return []
        # 取 top 3 熱門題材
        top_themes = themes_df.head(3).to_dict("records")
    except Exception as e:
        print(f"[smart_stealth] hot_themes fail: {e}", flush=True)
        return []

    candidates = []
    for theme_row in top_themes:
        theme = theme_row.get("題材", "")
        theme_avg = float(theme_row.get("平均%", 0) or 0)
        if theme_avg < 2.0:
            continue  # 族群不夠熱
        df = leaders_map.get(theme)
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            sid = str(r.get("stock_id", ""))
            name = str(r.get("stock_name", "") or r.get("name", ""))
            if not sid:
                continue
            result = _evaluate_smart_money_stock(sid, name, theme, theme_avg)
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
    now_tpe = (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%H:%M")
    lines = [
        f"🕵️ <b>大戶偷偷進場 Top {len(picks)}</b> · {now_tpe} TPE",
        "<i>(量比≥2x + 沒大漲 + 籌碼/族群配合)</i>",
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
        lines.append(
            f"   📍 進場 {p.get('entry_low', '—'):.2f}-{p.get('entry_high', '—'):.2f} · "
            f"停損 {p.get('stop_loss', '—'):.2f} · "
            f"目標 {p.get('target_short', '—'):.2f}/{p.get('target_mid', '—'):.2f} · "
            f"R:R {p.get('rr', 0):.2f}"
        )
        lines.append("")

    lines.append("<i>※ 智慧潛伏股 — 嚴條件挑選, 仍建議分批進場 + 嚴守停損.</i>")
    return "\n".join(lines)
