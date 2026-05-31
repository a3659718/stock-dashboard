"""
actionable_enhancer.py
為 actionable_picks 的 result 加上:
  A. 3 層目標價 (短/中/長線)
  B. chip_price_divergence 訊號 (進 reasons/warnings)
  C. 強勢族群 boost (+ score / + reason)
  D. 主力券商買賣超 (進 reasons)
  E. 屬於哪個強勢族群 (label, 給 app.py 顯示用)
  F. 主流板塊 boost (+0.5 分) — 科技/AI/能源/重電等 (新)

設計成 standalone module 讓 actionable_picks 一次呼叫:
  result = compute_actionable_picks(...)
  result = enhance_picks(result, market="TW")
"""
from __future__ import annotations

from typing import Dict, List, Optional

import data_sources as ds


# F: 主流板塊定義 — 涵蓋科技/半導體/AI/能源/電子製造/重電
MAINSTREAM_SECTORS = {
    # 證交所產業分類 (industry_category)
    "半導體業", "電腦及週邊設備業", "電子零組件業", "通信網路業",
    "光電業", "電子通路業", "資訊服務業", "其他電子業",
    "油電燃氣業", "電機機械",
    # 熱門題材 (sector_pulse TW_THEMES)
    "AI 伺服器", "AI 邊緣", "AI PC", "無人機", "低軌衛星",
    "重電族群", "散熱", "機器人", "儲能", "高頻高速",
    "ABF 載板", "矽光子", "CCL", "PCB", "被動元件", "面板",
    "電動車", "汽車零件",
}


def _is_mainstream_sector(sector_name: str) -> bool:
    """判斷一個族群名是否屬於主流板塊 (含模糊比對)."""
    if not sector_name:
        return False
    s = sector_name.strip()
    if s in MAINSTREAM_SECTORS:
        return True
    for mainstream in MAINSTREAM_SECTORS:
        if mainstream in s or s in mainstream:
            return True
    keywords = ["半導體", "電子", "電腦", "AI", "晶片", "ic", "IC",
                "能源", "電力", "重電", "光電", "機械"]
    return any(kw in s for kw in keywords)


# ---------------------------------------------------------------------------
# A: 3 層目標價
# ---------------------------------------------------------------------------
def _add_three_tier_targets(pick: Dict) -> None:
    """加 target_short / target_mid / target_long. 失敗時某層回 None 不 raise."""
    sid = str(pick.get("stock_id", ""))
    cur = pick.get("current")
    if not sid or cur is None:
        return
    # 短線 = 現有 target (從 actionable_picks 算的 ATR-based)
    pick["target_short"] = pick.get("target")

    # 中/長線: 抓 1y daily 算 fib + measured move
    try:
        import indicators as ind
        df = None
        for sfx in [".TW", ".TWO"]:
            tmp = ds.fetch_yf_history(f"{sid}{sfx}", period="1y", interval="1d")
            if tmp is not None and not tmp.empty and len(tmp) >= 60:
                df = tmp
                break
        if df is None or df.empty:
            return
        c = df["Close"].astype(float).reset_index(drop=True)

        # 中線: fib 1.27 extension (HIGH fix: 真實 key 是 fib_127 沒底線分隔)
        try:
            fib = ind.fibonacci_extension_targets(c, lookback=252, pivot_window=10) or {}
            t127 = fib.get("fib_127")
            if t127:
                pick["target_mid"] = round(float(t127), 2)
        except Exception:
            pass

        # 長線: measured move 或 fib 1.62 (HIGH fix: 真實 key 是 fib_162)
        try:
            mm = ind.measured_move_target(c, base_lookback=60, min_base_days=15) or {}
            t_mm = mm.get("target")
            if t_mm:
                pick["target_long"] = round(float(t_mm), 2)
            else:
                fib2 = ind.fibonacci_extension_targets(c, lookback=252, pivot_window=10) or {}
                t162 = fib2.get("fib_162")
                if t162:
                    pick["target_long"] = round(float(t162), 2)
        except Exception:
            pass
    except Exception as e:
        print(f"[enhancer] 3-tier targets {sid} 失敗: {e}", flush=True)


# ---------------------------------------------------------------------------
# B: chip_price_divergence
# ---------------------------------------------------------------------------
def _add_chip_divergence(pick: Dict) -> None:
    """跑 chip_price_divergence, 正面 pattern 加進 reasons, 負面進 warnings.

    HIGH fix: 真實 API analyze_stock(sid) 回 dict (不是 list), key 是:
        pattern, strength (1-5), strength_label, reason, recommendation
    """
    sid = str(pick.get("stock_id", ""))
    if not sid:
        return
    try:
        import chip_price_divergence as cpd
        result = cpd.analyze_stock(sid) or {}
        if not result or not result.get("pattern"):
            return
        pattern = str(result.get("pattern", ""))
        strength = result.get("strength")
        reason = str(result.get("reason", "") or result.get("recommendation", "") or pattern)
        emoji = str(result.get("emoji", "📊"))
        # 用 pattern 名分類正負面 (chip_price_divergence pattern 名常含 bullish/bearish)
        p_lower = pattern.lower()
        is_bearish = any(k in p_lower for k in ["bearish", "distribution", "warning", "exit",
                                                  "出貨", "警告", "看空"])
        is_bullish = any(k in p_lower for k in ["bullish", "accumulation", "bottom",
                                                  "進貨", "底部", "看多", "逢低"])
        if is_bearish:
            pick.setdefault("warnings", []).append(f"⚠️ {emoji} {reason}")
        elif is_bullish:
            pick.setdefault("reasons", []).append(f"{emoji} {reason}")
        else:
            # 強度 ≥ 3 視為值得提醒
            try:
                s = int(strength) if strength is not None else 0
            except (TypeError, ValueError):
                s = 0
            if s >= 3:
                pick.setdefault("reasons", []).append(f"{emoji} {reason}")
    except Exception as e:
        print(f"[enhancer B] chip_divergence {sid}: {e}", flush=True)


# ---------------------------------------------------------------------------
# C: 強勢族群 boost
# ---------------------------------------------------------------------------
def _get_current_strong_sectors() -> Dict[str, Dict]:
    """回 dict {stock_id: {sector_name, avg_pct}}.
    用 sector_pulse.compute_strong_sectors 找族群均漲 ≥ +1.5% 的族群龍頭股.
    """
    out = {}
    try:
        import sector_pulse as sp
        data = sp.compute_strong_sectors(top_n=200)
        sectors_df = data.get("sectors")
        leaders_df = data.get("leaders")
        if sectors_df is None or sectors_df.empty:
            return out
        # 篩出強勢族群 (avg ≥ +1.5%, up_ratio ≥ 0.6)
        strong = sectors_df[
            (sectors_df["avg_change"] >= 1.5) & (sectors_df["up_ratio"] >= 0.6)
        ]
        if strong.empty or leaders_df is None or leaders_df.empty:
            return out
        ind_col = "industry_category" if "industry_category" in leaders_df.columns \
                  else leaders_df.columns[0]
        for _, sec_row in strong.iterrows():
            sec_name = sec_row.get(ind_col, "")
            avg = float(sec_row.get("avg_change", 0) or 0)
            sub = leaders_df[leaders_df[ind_col] == sec_name]
            for _, ld in sub.iterrows():
                sid = str(ld.get("stock_id", ""))
                if sid:
                    out[sid] = {"sector_name": sec_name, "avg_pct": round(avg, 2)}
        # 加熱門題材
        try:
            themes_data = sp.compute_hot_themes()
            themes_df = themes_data.get("themes")
            theme_leaders = themes_data.get("leaders") or {}
            if themes_df is not None and not themes_df.empty:
                for _, t_row in themes_df.iterrows():
                    avg_t = float(t_row.get("平均%", 0) or 0)
                    up_ratio_pct = float(t_row.get("上漲比率%", 0) or 0)
                    if avg_t >= 1.5 and up_ratio_pct >= 60:
                        theme_name = str(t_row.get("題材", ""))
                        sub = theme_leaders.get(theme_name)
                        if sub is None or sub.empty:
                            continue
                        for _, ld in sub.iterrows():
                            sid = str(ld.get("stock_id", ""))
                            if sid and sid not in out:
                                out[sid] = {"sector_name": theme_name, "avg_pct": round(avg_t, 2)}
        except Exception:
            pass
    except Exception as e:
        print(f"[enhancer] get_current_strong_sectors 失敗: {e}", flush=True)
    return out


def _apply_strong_sector_boost(pick: Dict, strong_sectors_map: Dict) -> None:
    """若 pick 屬於強勢族群 → score +1.5 + 加 reason. 同時記 sector_label 供顯示."""
    sid = str(pick.get("stock_id", ""))
    info = strong_sectors_map.get(sid)
    if not info:
        return
    pick["score"] = round((pick.get("score", 0) or 0) + 1.5, 2)
    sec_name = info.get("sector_name", "")
    avg = info.get("avg_pct", 0)
    # MED fix: 用 append (不擠掉技術理由), 加在 reasons 末尾
    pick.setdefault("reasons", []).append(
        f"🚀 屬於強勢族群「{sec_name}」(均漲 +{avg:.2f}%) +1.5 分"
    )
    pick["sector_label"] = sec_name
    pick["sector_avg_pct"] = avg


def _apply_mainstream_boost(pick: Dict) -> None:
    """F: 若 pick 屬於主流板塊 (科技/AI/能源/重電) → score +0.5 + 加 reason + flag.

    判斷依據 (任一吻合即算): theme / sector_label / industry / sector.
    """
    candidates = [
        pick.get("theme"),
        pick.get("sector_label"),
        pick.get("industry"),
        pick.get("sector"),
    ]
    for c in candidates:
        if c and _is_mainstream_sector(str(c)):
            pick["score"] = round((pick.get("score", 0) or 0) + 0.5, 2)
            pick["is_mainstream"] = True
            pick.setdefault("reasons", []).append(
                f"🎯 主流板塊 (科技/AI/能源/重電) +0.5 分"
            )
            return
    pick["is_mainstream"] = False


# ---------------------------------------------------------------------------
# D: 主力券商買賣超
# ---------------------------------------------------------------------------
def _add_main_broker(pick: Dict) -> None:
    """若該股最近主力券商大量買進 → 加 reason.

    HIGH fix: 真實 API 是 chip_advanced.fetch_main_broker_flow, 回:
        {top_buy_brokers: dict, top_sell_brokers: dict,
         foreign_proxy_net: int, main_force_net: int}
    """
    sid = str(pick.get("stock_id", ""))
    if not sid:
        return
    try:
        import chip_advanced as ca
        data = ca.fetch_main_broker_flow(sid, days=5)
        if not data:
            return
        main_net = data.get("main_force_net") or 0
        fp_net = data.get("foreign_proxy_net") or 0
        top_buy = data.get("top_buy_brokers") or {}
        try:
            main_net = int(main_net)
            fp_net = int(fp_net)
        except (TypeError, ValueError):
            main_net = fp_net = 0
        # 取前 2 大買盤券商名 (top_buy 是 dict {broker_name: net})
        top_buy_names = list(top_buy.keys())[:2] if isinstance(top_buy, dict) else []
        # 主力 OR 外資代理淨買 ≥ 500 張
        net = max(main_net, fp_net)
        if net >= 500:
            brokers_str = "/".join(str(b) for b in top_buy_names)
            br_part = f" ({brokers_str})" if brokers_str else ""
            pick.setdefault("reasons", []).append(
                f"💼 主力券商淨買 +{net} 張{br_part}"
            )
        elif min(main_net, fp_net) <= -500:
            pick.setdefault("warnings", []).append(
                f"⚠️ 主力券商淨賣 {min(main_net, fp_net)} 張"
            )
    except Exception as e:
        print(f"[enhancer D] main_broker {sid}: {e}", flush=True)


def enhance_picks(picks: List[Dict], market: str = "TW",
                   mainstream_only: bool = False) -> List[Dict]:
    """對 actionable picks 一次性加 A+B+C+D+F enrich. 不 raise.

    呼叫順序: A 3 層目標 → B chip_divergence → C 強勢族群 boost →
              D 主力券商 → F 主流板塊 +0.5 → 重新排序 → (可選 mainstream filter)

    Args:
      mainstream_only: True 時最後過濾只回主流板塊 picks (科技/AI/能源).
                       預設 False (主流加分但不限制範圍, 仍看得到輪動).
    """
    if not picks or market != "TW":
        return picks

    strong_map = _get_current_strong_sectors()

    for p in picks:
        if not p.get("stock_id"):
            continue
        try:
            _add_three_tier_targets(p)
        except Exception as e:
            print(f"[enhancer A] {p.get('stock_id')}: {e}", flush=True)
        try:
            _add_chip_divergence(p)
        except Exception as e:
            print(f"[enhancer B] {p.get('stock_id')}: {e}", flush=True)
        try:
            _apply_strong_sector_boost(p, strong_map)
        except Exception as e:
            print(f"[enhancer C] {p.get('stock_id')}: {e}", flush=True)
        try:
            _add_main_broker(p)
        except Exception as e:
            print(f"[enhancer D] {p.get('stock_id')}: {e}", flush=True)
        try:
            _apply_mainstream_boost(p)
        except Exception as e:
            print(f"[enhancer F] {p.get('stock_id')}: {e}", flush=True)

    picks.sort(key=lambda x: x.get("score", 0) or 0, reverse=True)
    if mainstream_only:
        picks = [p for p in picks if p.get("is_mainstream")]
    return picks
