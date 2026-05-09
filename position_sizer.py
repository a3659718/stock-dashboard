"""
position_sizer.py
依「停損距離 + 帳戶風險容忍度」計算建議部位.

核心公式:
    risk_per_share = entry_price - stop_price
    max_loss_dollars = account_capital * risk_per_trade_pct / 100
    suggested_shares = floor(max_loss_dollars / risk_per_share)

例:
    帳戶 100 萬, 單筆風險 1% (= 1 萬), 進場 1050, 停損 1000.
    risk_per_share = 50, suggested_shares = 10000 / 50 = 200 股 (台股: 0.2 張)

對台股建議用「張」(1 張 = 1000 股), 美股直接用股.

Streamlit 端透過 sidebar 收 user 輸入 (account_capital, risk_per_trade_pct),
存到 watchlist_store.load_monitor_state()["position_sizer_config"], 跨 session 持久化.
"""

from __future__ import annotations

import math
from typing import Dict, Optional


# 預設值 — 適合中等風險偏好
DEFAULT_ACCOUNT_CAPITAL = 1_000_000  # 100 萬 NTD
DEFAULT_RISK_PER_TRADE_PCT = 1.0      # 單筆最多虧 1% 帳戶價值
DEFAULT_MAX_POSITION_PCT = 20.0        # 單筆部位不超過 20% 帳戶


def compute_position_size(
    entry_price: float,
    stop_price: float,
    account_capital: float = DEFAULT_ACCOUNT_CAPITAL,
    risk_per_trade_pct: float = DEFAULT_RISK_PER_TRADE_PCT,
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
    market: str = "TW",
) -> Optional[Dict]:
    """計算建議部位.

    Returns:
        None — 輸入無效 / 停損 >= 進場
        Dict — {
            "shares": int,            # 股數 (TW 會 round 到 1000 股 = 1 張)
            "lots": float,            # 張數 (僅 TW 有意義)
            "position_value": float,  # 部位金額
            "risk_dollars": float,    # 觸發停損會虧多少
            "risk_pct": float,        # 占帳戶 %
            "limited_by": str,        # "risk" / "max_position" / "none"
        }
    """
    try:
        entry = float(entry_price)
        stop = float(stop_price)
        cap = float(account_capital)
        risk_pct = float(risk_per_trade_pct)
        max_pos_pct = float(max_position_pct)
    except (TypeError, ValueError):
        return None

    if entry <= 0 or stop <= 0 or cap <= 0 or risk_pct <= 0:
        return None
    if entry <= stop:
        # 停損沒在進場下方 → 沒意義
        return None

    risk_per_share = entry - stop
    max_loss = cap * risk_pct / 100.0
    shares_by_risk = max_loss / risk_per_share

    # 同時受 max_position_pct 限制
    max_position_value = cap * max_pos_pct / 100.0
    shares_by_position = max_position_value / entry

    if shares_by_position < shares_by_risk:
        shares = shares_by_position
        limited_by = "max_position"
    else:
        shares = shares_by_risk
        limited_by = "risk"

    # 台股: round down 到 1000 股 (張) 為單位; 不滿 1 張 (< 1000) 顯示零股
    if market == "TW":
        if shares >= 1000:
            shares = math.floor(shares / 1000) * 1000
            lots = shares / 1000
        else:
            shares = math.floor(shares)
            lots = round(shares / 1000, 2) if shares else 0
    else:  # US
        shares = math.floor(shares)
        lots = shares  # 美股 1 lot = 1 share

    if shares <= 0:
        return {
            "shares": 0,
            "lots": 0,
            "position_value": 0.0,
            "risk_dollars": 0.0,
            "risk_pct": 0.0,
            "limited_by": "too_small",
            "note": "停損距離過大或資金不足, 建議等更好進場點",
        }

    position_value = shares * entry
    actual_risk = shares * risk_per_share
    actual_risk_pct = actual_risk / cap * 100

    return {
        "shares": int(shares),
        "lots": lots,
        "position_value": round(position_value, 2),
        "risk_dollars": round(actual_risk, 2),
        "risk_pct": round(actual_risk_pct, 3),
        "limited_by": limited_by,
    }


def fmt_position_advice(sizing: Optional[Dict], market: str = "TW") -> str:
    """把部位建議格式化成一行 (給推播 / dashboard 用)."""
    if not sizing or sizing.get("shares", 0) <= 0:
        if sizing and sizing.get("note"):
            return f"建議部位: {sizing['note']}"
        return ""

    shares = sizing["shares"]
    lots = sizing.get("lots", 0)
    pv = sizing["position_value"]
    risk_d = sizing["risk_dollars"]
    risk_pct = sizing["risk_pct"]

    if market == "TW":
        if lots >= 1:
            unit = f"{int(lots)} 張" if lots == int(lots) else f"{lots:.1f} 張"
        else:
            unit = f"{shares} 股 (零股)"
    else:
        unit = f"{shares} 股"

    return (f"建議部位: {unit} (約 {pv:,.0f}) · "
            f"觸停損會虧 {risk_d:,.0f} ({risk_pct:.2f}% 帳戶)")


def load_user_config(default_capital: float = DEFAULT_ACCOUNT_CAPITAL,
                      default_risk_pct: float = DEFAULT_RISK_PER_TRADE_PCT) -> Dict:
    """從 watchlist_store 讀使用者設定. 沒設用 default."""
    try:
        import watchlist_store
        state = watchlist_store.load_monitor_state()
        cfg = state.get("position_sizer_config") or {}
        return {
            "account_capital": float(cfg.get("account_capital", default_capital)),
            "risk_per_trade_pct": float(cfg.get("risk_per_trade_pct", default_risk_pct)),
            "max_position_pct": float(cfg.get("max_position_pct", DEFAULT_MAX_POSITION_PCT)),
        }
    except Exception:
        return {
            "account_capital": default_capital,
            "risk_per_trade_pct": default_risk_pct,
            "max_position_pct": DEFAULT_MAX_POSITION_PCT,
        }


def save_user_config(account_capital: float, risk_per_trade_pct: float,
                      max_position_pct: float = DEFAULT_MAX_POSITION_PCT) -> bool:
    """寫使用者設定."""
    try:
        import watchlist_store
        state = watchlist_store.load_monitor_state()
        state["position_sizer_config"] = {
            "account_capital": float(account_capital),
            "risk_per_trade_pct": float(risk_per_trade_pct),
            "max_position_pct": float(max_position_pct),
        }
        watchlist_store.save_monitor_state(state)
        return True
    except Exception as e:
        print(f"[position_sizer] save failed: {e}", flush=True)
        return False
