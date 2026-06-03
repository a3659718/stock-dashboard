"""
morning_action_alert.py
早盤情境推播 — 整合 1 封 TG, 涵蓋:
  - 大盤開盤實況 (TWII / SPY)
  - 美股隔夜 / JP-KR 亞股先行 (TW 用) 或 隔夜亞股 (US 用)
  - 強勢族群 Top 3
  - 推薦進場 BUY Top 3

對外 API:
  build_morning_action_msg(market="TW") -> str (TG HTML)
  build_morning_action_msg(market="US") -> str
"""
from __future__ import annotations

from typing import Dict, List


def _fetch_index_snap(symbol: str) -> Dict:
    """抓單一指數 snap: open, current, pct."""
    try:
        import data_sources as ds
        df = ds.fetch_yf_history(symbol, period="2d", interval="1d")
        if df is None or df.empty:
            return {}
        c = df["Close"].astype(float)
        o = df["Open"].astype(float)
        cur = float(c.iloc[-1])
        op = float(o.iloc[-1])
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


def _get_tw_strong_sectors(top_n: int = 3) -> List[Dict]:
    try:
        import sector_pulse as sp
        data = sp.compute_strong_sectors(top_n=80)
        sec_df = data.get("sectors")
        if sec_df is None or sec_df.empty:
            return []
        rows = sec_df.head(top_n).to_dict("records")
        ind_col = "industry_category" if "industry_category" in sec_df.columns else None
        return [
            {
                "sector": r.get(ind_col, "—") if ind_col else "—",
                "avg": round(float(r.get("avg_change", 0) or 0), 2),
                "up_ratio": round(float(r.get("up_ratio", 0) or 0) * 100, 0),
                "n": int(r.get("n", 0) or 0),
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[morning] tw strong sectors fail: {e}", flush=True)
        return []


def _get_tw_buy_picks(top_n: int = 3) -> List[Dict]:
    try:
        import actionable_picks as ap
        picks = ap.compute_actionable_picks(top_n=10) or []
        buy = [p for p in picks if p.get("entry_label") == "BUY"]
        return buy[:top_n]
    except Exception as e:
        print(f"[morning] tw buy picks fail: {e}", flush=True)
        return []


def _get_us_buy_picks(top_n: int = 3) -> List[Dict]:
    try:
        import us_actionable as ua
        picks = ua.compute_us_actionable_picks(top_n=10) or []
        buy = [p for p in picks if p.get("entry_label") == "BUY"]
        return buy[:top_n]
    except Exception as e:
        print(f"[morning] us buy picks fail: {e}", flush=True)
        return []


def _get_us_strong_sectors(top_n: int = 3) -> List[Dict]:
    try:
        import sector_rotation as sr
        d = sr.compute_sector_rotation("US")
        leading = d.get("quadrants", {}).get("leading", [])
        improving = d.get("quadrants", {}).get("improving", [])
        merged = (leading + improving)[:top_n]
        return [
            {
                "sector": x.get("name", "—"),
                "etf": x.get("etf", ""),
                "this_w": x.get("this_week_pct", 0),
                "last_w": x.get("last_week_pct", 0),
            }
            for x in merged
        ]
    except Exception as e:
        print(f"[morning] us sectors fail: {e}", flush=True)
        return []


def build_morning_action_msg(market: str = "TW") -> str:
    """建立早盤情境 TG 訊息 (HTML). 失敗回空字串."""
    try:
        from notifier import _esc
    except Exception:
        def _esc(x): return str(x)

    if market == "TW":
        # 台股早盤情境 (09:35 TPE = 01:35 UTC)
        twii = _fetch_index_snap("^TWII")
        n225 = _fetch_index_snap("^N225")
        ks11 = _fetch_index_snap("^KS11")
        spy = _fetch_index_snap("SPY")
        sox = _fetch_index_snap("^SOX")
        sectors = _get_tw_strong_sectors(3)
        buys = _get_tw_buy_picks(3)

        lines = ["🌅 <b>台股早盤情境 (09:35)</b>", "━━━━━━━━━━━━━━━"]
        if twii.get("current"):
            tag = "⚡強開" if twii.get("pct_vs_prev", 0) >= 1.0 \
                else ("📉弱開" if twii.get("pct_vs_prev", 0) <= -1.0 else "")
            lines.append(
                f"📊 加權: {twii['current']:,.2f} "
                f"<b>{twii.get('pct_vs_prev', 0):+.2f}%</b> {tag}"
            )
        ovn_parts = []
        if spy.get("current"):
            ovn_parts.append(f"SPY {spy.get('pct_vs_prev', 0):+.2f}%")
        if sox.get("current"):
            ovn_parts.append(f"SOX {sox.get('pct_vs_prev', 0):+.2f}%")
        if ovn_parts:
            lines.append(f"  美股隔夜: {' · '.join(ovn_parts)}")
        lead_parts = []
        if n225.get("current"):
            lead_parts.append(f"日經 {n225.get('pct_vs_prev', 0):+.2f}%")
        if ks11.get("current"):
            lead_parts.append(f"KOSPI {ks11.get('pct_vs_prev', 0):+.2f}%")
        if lead_parts:
            lines.append(f"🌏 亞股先行: {' · '.join(lead_parts)}")
        if sectors:
            lines.append("")
            lines.append("🚀 <b>強勢族群 Top 3</b>")
            for s in sectors:
                lines.append(
                    f"  {_esc(s['sector'])} <b>+{s['avg']:.2f}%</b> "
                    f"(上漲 {s['up_ratio']:.0f}%, {s['n']} 檔)"
                )
        if buys:
            lines.append("")
            lines.append("📊 <b>今日推薦進場 (BUY)</b>")
            for p in buys:
                sid = _esc(p.get("stock_id") or p.get("symbol", ""))
                name = _esc(p.get("name") or p.get("stock_name", ""))
                tp = p.get("today_pct") or p.get("今日%") or 0
                sc = p.get("score") or p.get("entry_score", "—")
                try: tp = float(tp)
                except (TypeError, ValueError): tp = 0
                lines.append(f"  <code>{sid}</code> {name} <b>{tp:+.2f}%</b> 🟢 (score {sc})")
        # 操作建議
        if twii.get("pct_vs_prev", 0) >= 1.0:
            lines.append("")
            lines.append("💡 強開無回測 → 順勢, 避免追高; 拉回 0.5% 內找買點")
        elif twii.get("pct_vs_prev", 0) <= -1.0:
            lines.append("")
            lines.append("💡 弱開未止穩 → 觀望, 持倉減碼; 留意強勢族群作多")
        return "\n".join(lines)

    elif market == "US":
        # 美股早盤情境 (10:05 NYC = 14:05 UTC EDT)
        spy = _fetch_index_snap("SPY")
        qqq = _fetch_index_snap("QQQ")
        sox = _fetch_index_snap("^SOX")
        twii = _fetch_index_snap("^TWII")
        sectors = _get_us_strong_sectors(3)
        buys = _get_us_buy_picks(3)

        lines = ["🌅 <b>美股早盤情境 (10:05 NYC)</b>", "━━━━━━━━━━━━━━━"]
        for sym, name in [("SPY", "SPY"), ("QQQ", "QQQ"), ("^SOX", "SOX")]:
            snap = _fetch_index_snap(sym)
            if snap.get("current"):
                tag = "⚡強開" if snap.get("pct_vs_prev", 0) >= 1.0 \
                    else ("📉弱開" if snap.get("pct_vs_prev", 0) <= -1.0 else "")
                lines.append(
                    f"📊 {name}: {snap['current']:,.2f} "
                    f"<b>{snap.get('pct_vs_prev', 0):+.2f}%</b> {tag}"
                )
        if twii.get("current"):
            lines.append(f"🇹🇼 台股今日收盤: {twii.get('pct_vs_prev', 0):+.2f}%")
        if sectors:
            lines.append("")
            lines.append("🚀 <b>強勢族群 (rotation Top 3)</b>")
            for s in sectors:
                lines.append(
                    f"  {_esc(s['sector'])} ({_esc(s['etf'])}) "
                    f"本週 <b>{s['this_w']:+.2f}%</b> · 上週 {s['last_w']:+.2f}%"
                )
        if buys:
            lines.append("")
            lines.append("📊 <b>今日推薦進場 (BUY)</b>")
            for p in buys:
                sym_ = _esc(p.get("symbol", ""))
                name = _esc(p.get("name", ""))
                tp = p.get("today_pct") or 0
                sc = p.get("entry_score", "—")
                try: tp = float(tp)
                except (TypeError, ValueError): tp = 0
                lines.append(f"  <code>{sym_}</code> {name} <b>{tp:+.2f}%</b> 🟢 (score {sc})")
        if spy.get("pct_vs_prev", 0) >= 1.0:
            lines.append("")
            lines.append("💡 大盤強開 → 板塊輪動找龍頭, 留意科技/AI")
        elif spy.get("pct_vs_prev", 0) <= -1.0:
            lines.append("")
            lines.append("💡 大盤弱開 → 觀望或佈局防禦類 (XLP/XLU)")
        msg = "\n".join(lines)
        try:
            from notifier import _truncate_tg_msg
            return _truncate_tg_msg(msg)
        except Exception:
            return msg
    return ""
