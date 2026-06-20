"""
hot_theme_v2.py
熱門題材強化版 — 不再找「第一根」, 改找 3 種型態:

1. 整理 1 月以上突破 (真突破, 不是假突破)
   - 近 20 日窄幅整理 (range ≤ 8%)
   - 今日突破 20 日高 + 量增 ≥ 1.8x
   - 收盤站上整理高位 → 真突破

2. Leader 帶動 Follower (連動策略)
   - 族群龍頭今日 ≥ +3%, 個股還在 0~+2%
   - 個股 RS vs 龍頭歷史相關性高 (簡化: 同族群)
   - 適合短線 follow-up

3. 量增但股價未動 (吸籌型態)
   - 量比 ≥ 2.0x
   - 今日 ≤ +1% (沒拉抬, 純吸籌)
   - 股價在 20MA ±3% (穩穩接)
   - 跟「smart_money_stealth」略有重疊但更聚焦量價

API:
  find_breakout_consolidation(themes_hot) -> List[Dict]  # 型態 1
  find_leader_follower(themes_hot) -> List[Dict]         # 型態 2
  find_volume_absorb(themes_hot) -> List[Dict]           # 型態 3
  find_all_patterns(top_themes=3) -> Dict[type, picks]   # 全部
"""
from __future__ import annotations

from typing import Dict, List, Optional
import data_sources as ds


def _stock_metrics(stock_id: str) -> Optional[Dict]:
    """取單檔關鍵指標."""
    try:
        for suf in [".TW", ".TWO"]:
            sym = f"{stock_id}{suf}"
            df = ds.fetch_yf_history(sym, period="60d", interval="1d")
            if df is not None and not df.empty and len(df) >= 20:
                break
        else:
            return None
        c = df["Close"].astype(float).reset_index(drop=True)
        h = df["High"].astype(float).reset_index(drop=True)
        v = df["Volume"].astype(float).reset_index(drop=True)

        cur = float(c.iloc[-1])
        prev = float(c.iloc[-2]) if len(c) >= 2 else cur
        today_pct = (cur / prev - 1) * 100 if prev > 0 else 0
        ma5 = float(c.tail(5).mean())
        ma20 = float(c.tail(20).mean())
        # Bug fix: 原本 h.tail(20) 含「今日」高點 → cur >= high_20d*0.99 幾乎必過, 變成假突破.
        #          改用「今日之前」的 20 日高 (排除今日) 才是真正突破前高.
        high_20d = float(h.iloc[-21:-1].max()) if len(h) >= 21 else (
            float(h.iloc[:-1].max()) if len(h) >= 2 else cur)
        avg_vol_20d = float(v.tail(20).mean())
        today_vol = float(v.iloc[-1])
        vol_ratio = today_vol / avg_vol_20d if avg_vol_20d > 0 else 0

        # 近 20 日 range
        c_20 = c.tail(20)
        range_pct_20 = (float(c_20.max()) - float(c_20.min())) / float(c_20.median()) * 100

        ma20_dev = (cur / ma20 - 1) * 100 if ma20 > 0 else 0
        return {
            "stock_id": stock_id,
            "current": round(cur, 2),
            "today_pct": round(today_pct, 2),
            "ma5": round(ma5, 2),
            "ma20": round(ma20, 2),
            "high_20d": round(high_20d, 2),
            "vol_ratio": round(vol_ratio, 2),
            "range_pct_20": round(range_pct_20, 2),
            "ma20_dev": round(ma20_dev, 2),
        }
    except Exception:
        return None


def find_breakout_consolidation(theme_picks: Dict[str, List]) -> List[Dict]:
    """型態 1: 整理 1 月後突破.

    條件:
      - 近 20 日 range ≤ 8% (整理)
      - 今日突破 20 日高 (cur ≥ high_20d * 0.98)
      - 量比 ≥ 1.8x
    """
    out = []
    for theme, stocks in theme_picks.items():
        for sid_data in stocks:
            sid = sid_data.get("stock_id")
            name = sid_data.get("stock_name", "")
            if not sid:
                continue
            m = _stock_metrics(sid)
            if not m:
                continue
            cond_consol = m["range_pct_20"] <= 8.0
            cond_breakout = m["current"] >= m["high_20d"] * 0.99
            cond_vol = m["vol_ratio"] >= 1.8
            if cond_consol and cond_breakout and cond_vol:
                m["name"] = name
                m["theme"] = theme
                m["pattern"] = "📈 整理突破"
                m["reasoning"] = (
                    f"近 20 日窄幅 {m['range_pct_20']:.2f}% + "
                    f"突破 20d 高 {m['high_20d']:.2f} + "
                    f"量比 {m['vol_ratio']:.2f}x"
                )
                # 進出場
                m["entry"] = round(m["current"] * 0.99, 2)
                m["stop_loss"] = round(m["ma20"] * 0.97, 2)
                m["target_short"] = round(m["current"] * 1.05, 2)
                m["target_mid"] = round(m["current"] * 1.10, 2)
                out.append(m)
    return sorted(out, key=lambda x: x["vol_ratio"], reverse=True)[:5]


def find_leader_follower(theme_picks: Dict[str, List],
                          leader_min_pct: float = 3.0) -> List[Dict]:
    """型態 2: Leader 帶 Follower.

    條件:
      - 同題材中 龍頭今日 ≥ +3%
      - 個股本身今日 0 ~ +2%
      - 量比 ≥ 1.3x (有量但沒大量)
    """
    out = []
    for theme, stocks in theme_picks.items():
        if not stocks or len(stocks) < 2:
            continue
        # 找 leader (今日%最高的)
        all_metrics = []
        for s in stocks:
            sid = s.get("stock_id")
            if not sid:
                continue
            m = _stock_metrics(sid)
            if m:
                m["_stock_name"] = s.get("stock_name", "")
                all_metrics.append(m)
        if not all_metrics:
            continue
        all_metrics.sort(key=lambda x: x["today_pct"], reverse=True)
        leader = all_metrics[0]
        if leader["today_pct"] < leader_min_pct:
            continue
        # 找 follower
        for m in all_metrics[1:]:
            if 0 <= m["today_pct"] <= 2 and m["vol_ratio"] >= 1.3:
                m["name"] = m.get("_stock_name", "")
                m["theme"] = theme
                m["pattern"] = "🚀 龍頭帶 follower"
                m["leader_pct"] = leader["today_pct"]
                m["leader_id"] = leader["stock_id"]
                m["reasoning"] = (
                    f"族群龍頭 {leader['stock_id']} 今日 {leader['today_pct']:+.2f}%, "
                    f"本身僅 {m['today_pct']:+.2f}% + 量比 {m['vol_ratio']:.2f}x → 有機會跟漲"
                )
                m["entry"] = round(m["current"] * 0.995, 2)
                m["stop_loss"] = round(m["ma20"] * 0.97, 2)
                m["target_short"] = round(m["current"] * (1 + leader["today_pct"] / 100 * 0.7), 2)
                m["target_mid"] = round(m["high_20d"] * 1.02, 2)
                out.append(m)
                break  # 一族群一檔
    return sorted(out, key=lambda x: x.get("leader_pct", 0), reverse=True)[:5]


def find_volume_absorb(theme_picks: Dict[str, List]) -> List[Dict]:
    """型態 3: 量增但股價未動 (吸籌).

    條件:
      - 量比 ≥ 2.0x
      - 今日 -0.5% ~ +1%
      - 股價在 20MA ±3%
    """
    out = []
    for theme, stocks in theme_picks.items():
        for s in stocks:
            sid = s.get("stock_id")
            if not sid:
                continue
            m = _stock_metrics(sid)
            if not m:
                continue
            cond_vol = m["vol_ratio"] >= 2.0
            cond_static = -0.5 <= m["today_pct"] <= 1.0
            cond_ma = abs(m["ma20_dev"]) <= 3
            if cond_vol and cond_static and cond_ma:
                m["name"] = s.get("stock_name", "")
                m["theme"] = theme
                m["pattern"] = "🕵️ 吸籌量增"
                m["reasoning"] = (
                    f"量比 {m['vol_ratio']:.2f}x 但今日僅 {m['today_pct']:+.2f}% + "
                    f"距 20MA {m['ma20_dev']:+.2f}% → 大戶悄悄接"
                )
                m["entry"] = round(m["current"] * 0.99, 2)
                m["stop_loss"] = round(m["ma20"] * 0.97, 2)
                m["target_short"] = round(m["current"] * 1.05, 2)
                m["target_mid"] = round(m["high_20d"] * 1.02, 2)
                out.append(m)
    return sorted(out, key=lambda x: x["vol_ratio"], reverse=True)[:5]


def find_all_patterns(top_themes: int = 3) -> Dict[str, List]:
    """整合 3 型態, 回 {型態: picks}."""
    try:
        import sector_pulse as sp
        hot = sp.compute_hot_themes()
        themes_df = hot.get("themes")
        leaders_map = hot.get("leaders") or {}
        if themes_df is None or themes_df.empty:
            return {}
        top_names = themes_df["題材"].head(top_themes).tolist()
        theme_picks = {}
        for tn in top_names:
            df = leaders_map.get(tn)
            if df is None or df.empty:
                continue
            theme_picks[tn] = df.to_dict("records")
        if not theme_picks:
            return {}

        return {
            "breakout": find_breakout_consolidation(theme_picks),
            "leader_follower": find_leader_follower(theme_picks),
            "volume_absorb": find_volume_absorb(theme_picks),
        }
    except Exception as e:
        print(f"[hot_theme_v2] fail: {e}", flush=True)
        return {}
