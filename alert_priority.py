"""
alert_priority.py — 訊號優先級 + 跨類去重

問題:
  - 推播太多種 (反轉 / 強股 / 強族 / 重大消息 / 持倉 / 川普 / 台指), 用戶不知道哪個重要
  - 同一檔股票可能 30 分鐘內出現在 2-3 個推播 (強股 + 反轉 + companion)
  - 用戶該注意「最重要的 1-2 個」, 不是全看

解法:
  1. Tier 分級 — 每個 alert 標 Tier 0 (致命) ~ Tier 3 (參考)
     Tier 0: 持倉風險 / 重大事件 / 川普政策 → 響鈴
     Tier 1: 反轉/開盤即弱/強 / 升貼水異常 → 高優先
     Tier 2: 盤中強勢股 / 短空候選 / 法人轉向 → 中優先
     Tier 3: 強勢族群 / RSI 背離 / 散戶反指標 → 參考

  2. 跨類去重 — 同一 stock_id 在 30min 內同方向訊號 → 合併或第 2 則 silent skip

  3. 推播前綴 tag — 訊息開頭加 [致命] / [重要] 等

API:
  classify_alert(alert_type) -> int  # tier
  prefix_tier_tag(tier) -> str  # "[致命] " 等
  dedup_recent_pushes(stock_id, alert_type, direction) -> bool  # True 表示要 skip
  mark_pushed(stock_id, alert_type, direction)
"""
from __future__ import annotations

import datetime as dt
from datetime import timezone
from typing import Dict, Optional


# Tier 對照表 (alert_type → tier)
TIER_MAP = {
    # ── Tier 0: 致命 / 立即動作 ──
    "holdings_intraday_critical": 0,  # 持倉急殺
    "trump_policy_critical": 0,        # 川普政策
    "news_event_critical": 0,          # 重大消息 (8-K, 重大訊息)
    "circuit_breaker": 0,               # 熔斷

    # ── Tier 1: 高優先 (操作訊號) ──
    "intraday_reversal": 1,
    "weak_open": 1,
    "strong_open": 1,
    "tx_basis_anomaly": 1,             # 升貼水異常
    "tx_inst_flip": 1,                  # 法人轉向
    "actionable_pick_buy": 1,           # 可進場精選 BUY

    # ── Tier 2: 中優先 (參考訊號) ──
    "intraday_strong_stock": 2,
    "intraday_short_candidate": 2,
    "smart_money_stealth": 2,
    "volume_breakout": 2,
    "chip_anomaly": 2,
    "morning_action": 2,
    "pre_market": 2,
    "post_market": 2,

    # ── Tier 3: 背景參考 (盤勢) ──
    "strong_sector": 3,
    "rsi_divergence": 3,
    "retail_extreme": 3,
    "sector_rotation": 3,
    "fear_greed": 3,
}


def classify_alert(alert_type: str) -> int:
    """回 tier 0-3. 未知預設 2."""
    return TIER_MAP.get(alert_type, 2)


def prefix_tier_tag(tier: int) -> str:
    """產生 prefix tag for TG 訊息開頭."""
    return {
        0: "🚨[致命] ",
        1: "🔴[高優先] ",
        2: "🟡",      # 不加文字 tag, 只 emoji
        3: "⚪",
    }.get(tier, "")


def should_ring(tier: int) -> bool:
    """是否該響鈴 (disable_notification=False)."""
    return tier <= 1


# ── 跨類去重 ──
DEDUP_WINDOW_MIN = 30  # 30 min 內同股同向不重複


def _state_key() -> str:
    return "alert_dedup"


def _get_state() -> Dict:
    try:
        import watchlist_store
        return watchlist_store.load_monitor_state() or {}
    except Exception:
        return {}


def _save_state(state: Dict) -> None:
    try:
        import watchlist_store
        watchlist_store.save_monitor_state(state)
    except Exception:
        pass


def dedup_recent_pushes(stock_id: str, alert_type: str, direction: str = "up") -> bool:
    """檢查同股同向訊號是否最近推過. True = 要 skip.

    direction: "up" / "down" / "neutral"
    """
    if not stock_id or not alert_type:
        return False
    state = _get_state()
    dedup = state.get(_state_key(), {})
    key = f"{stock_id}|{direction}"
    rec = dedup.get(key)
    if not rec:
        return False
    try:
        last_ts = dt.datetime.fromisoformat(rec.get("ts", "").replace("Z", "+00:00"))
        mins_since = (dt.datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        if mins_since < DEDUP_WINDOW_MIN:
            return True
    except Exception:
        pass
    return False


def mark_pushed(stock_id: str, alert_type: str, direction: str = "up") -> None:
    """記錄已推. 一週後過期 (避免 state file 爆)."""
    if not stock_id or not alert_type:
        return
    state = _get_state()
    dedup = state.setdefault(_state_key(), {})
    key = f"{stock_id}|{direction}"
    dedup[key] = {
        "ts": dt.datetime.now(timezone.utc).isoformat(),
        "alert_type": alert_type,
    }
    # 清過期 (>7 日)
    cutoff = dt.datetime.now(timezone.utc) - dt.timedelta(days=7)
    to_del = []
    for k, v in dedup.items():
        try:
            ts = dt.datetime.fromisoformat(v.get("ts", "").replace("Z", "+00:00"))
            if ts < cutoff:
                to_del.append(k)
        except Exception:
            to_del.append(k)
    for k in to_del:
        dedup.pop(k, None)
    _save_state(state)


def filter_dedup_picks(picks: list, alert_type: str, direction: str = "up") -> list:
    """從 picks list 過濾掉最近推過的 stock_id.

    用法: 推播前 picks = alert_priority.filter_dedup_picks(picks, "intraday_strong_stock", "up")
    """
    if not picks:
        return picks
    out = []
    for p in picks:
        sid = str(p.get("stock_id") or p.get("symbol", ""))
        if not sid:
            out.append(p)
            continue
        if not dedup_recent_pushes(sid, alert_type, direction):
            out.append(p)
    return out


def mark_picks_pushed(picks: list, alert_type: str, direction: str = "up") -> None:
    """把 picks list 全部標記已推 (一次寫一次 state)."""
    if not picks:
        return
    state = _get_state()
    dedup = state.setdefault(_state_key(), {})
    now_iso = dt.datetime.now(timezone.utc).isoformat()
    for p in picks:
        sid = str(p.get("stock_id") or p.get("symbol", ""))
        if not sid:
            continue
        key = f"{sid}|{direction}"
        dedup[key] = {"ts": now_iso, "alert_type": alert_type}
    # 清過期
    cutoff = dt.datetime.now(timezone.utc) - dt.timedelta(days=7)
    to_del = []
    for k, v in dedup.items():
        try:
            ts = dt.datetime.fromisoformat(v.get("ts", "").replace("Z", "+00:00"))
            if ts < cutoff:
                to_del.append(k)
        except Exception:
            to_del.append(k)
    for k in to_del:
        dedup.pop(k, None)
    _save_state(state)
