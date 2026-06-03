"""
strong_sector_alert.py
盤中相對資金強勢族群偵測 — 證交所產業 + 熱門題材 雙軸.

觸發條件 (中度):
  - 族群均漲 ≥ +1.5%
  - 上漲家數占比 ≥ 60%
  - 族群成份股數 ≥ 3 (避免單檔噪音)

Throttle:
  - per-sector per-day cap: 1 則 (同一族群一天只推一次)
  - 全域 cooldown: 60 min between batches (避免短時間連推)
  - 一批推播最多 SECTOR_MAX_PER_BATCH = 3 族群

只在台股 session 內 (09:00-13:30 TPE = 01:00-05:30 UTC) 跑.
給 market_open_alert.py monitor flow 呼叫.

State: monitor_state["strong_sector_alert"] = {
   "date": "YYYY-MM-DD",
   "sectors_alerted": [<sector_name>, ...],  # 已推過的族群 (per-day cap 1)
   "last_batch_at": iso,                      # 上次推批的時間 (全域 cooldown)
}

API:
  - check_strong_sectors_intraday() -> List[Dict]
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List

import data_sources as ds  # noqa: F401  (sector_pulse 已用; 留著方便未來擴充)
import watchlist_store
import sector_pulse


SECTOR_AVG_THRESHOLD = 1.5        # 族群均漲 % (vs 昨收)
SECTOR_UP_RATIO_THRESHOLD = 0.6   # 上漲家數占比 (0-1)
SECTOR_MIN_STOCKS = 3             # 至少要 N 檔成份股 (避免單檔噪音)
SECTOR_COOLDOWN_MIN = 60          # 兩批推播間最少間隔 (分)
SECTOR_MAX_PER_BATCH = 3          # 一批最多 N 個族群

# 強度標籤門檻
STRONG_AVG_THRESHOLD = 2.5
STRONG_UP_RATIO_THRESHOLD = 0.75


def _is_tw_session() -> bool:
    """台股交易時段 09:00-13:30 TPE = 01:00-05:30 UTC, 平日."""
    now_utc = dt.datetime.utcnow()
    if now_utc.weekday() >= 5:
        return False
    cur = now_utc.hour + now_utc.minute / 60.0
    return 1.0 <= cur < 5.5


def _extract_leaders(sub_df) -> List[Dict]:
    """從 leaders df 取 top 3 龍頭股. 容錯欄位差異."""
    if sub_df is None or len(sub_df) == 0:
        return []
    try:
        out = []
        for _, r in sub_df.head(3).iterrows():
            sid = str(r.get("stock_id", ""))
            if not sid:
                continue
            try:
                tp = float(r.get("今日%", 0) or 0)
            except (TypeError, ValueError):
                tp = 0.0
            try:
                vr = float(r.get("量比", 0) or 0)
            except (TypeError, ValueError):
                vr = 0.0
            out.append({
                "stock_id": sid,
                "name": str(r.get("stock_name", "")),
                "today_pct": round(tp, 2),
                "vol_ratio": round(vr, 2),
            })
        return out
    except Exception:
        return []


def check_strong_sectors_intraday() -> List[Dict]:
    """偵測盤中相對強勢族群. 回傳 alert list (空 = 沒觸發).

    每個 alert dict 含:
      sector_type: "industry"/"theme"
      sector_name: e.g. "半導體業" / "AI 伺服器"
      avg_pct: 族群均漲 %
      up_ratio: 上漲家數占比 (0-1)
      n_stocks: 族群成份股數
      leaders: top 3 龍頭股 [{stock_id, name, today_pct, vol_ratio}]
      severity: "medium" / "strong"
    """
    if not _is_tw_session():
        return []

    # 假日 skip
    try:
        import holiday_check
        if holiday_check.is_market_closed_today("TW"):
            return []
    except Exception:
        pass

    state = watchlist_store.load_monitor_state()
    ssa_state = state.setdefault("strong_sector_alert", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    now_utc = dt.datetime.utcnow()

    # 跨日 reset
    if ssa_state.get("date") != today_str:
        ssa_state.clear()
        ssa_state.update({
            "date": today_str,
            "sectors_alerted": [],   # 已推過的族群名 (per-day cap 1)
            "last_batch_at": None,
        })

    # 全域 cooldown
    last_batch = ssa_state.get("last_batch_at")
    if last_batch:
        try:
            ts = dt.datetime.fromisoformat(last_batch)
            if (now_utc - ts).total_seconds() < SECTOR_COOLDOWN_MIN * 60:
                return []
        except Exception:
            pass

    candidates: List[Dict] = []

    # === 1) 證交所產業分類 ===
    # 欄位: industry_category, avg_change, up_count, n, up_ratio (0-1)
    try:
        sectors_data = sector_pulse.compute_strong_sectors(top_n=200)
        sectors_df = sectors_data.get("sectors")
        leaders_df = sectors_data.get("leaders")
        if sectors_df is not None and not sectors_df.empty:
            ind_col = "industry_category" if "industry_category" in sectors_df.columns \
                      else sectors_df.columns[0]
            for _, row in sectors_df.iterrows():
                try:
                    avg = float(row.get("avg_change", 0) or 0)
                    up_ratio = float(row.get("up_ratio", 0) or 0)
                    n = int(row.get("n", 0) or 0)
                except (TypeError, ValueError):
                    continue
                ind_name = str(row.get(ind_col, ""))
                if (avg >= SECTOR_AVG_THRESHOLD
                        and up_ratio >= SECTOR_UP_RATIO_THRESHOLD
                        and n >= SECTOR_MIN_STOCKS):
                    sub = (leaders_df[leaders_df[ind_col] == ind_name]
                           if leaders_df is not None and ind_col in leaders_df.columns
                           else None)
                    candidates.append({
                        "sector_type": "industry",
                        "sector_name": ind_name,
                        "avg_pct": round(avg, 2),
                        "up_ratio": round(up_ratio, 3),
                        "n_stocks": n,
                        "leaders": _extract_leaders(sub),
                    })
    except Exception as e:
        print(f"[strong_sector] 證交所產業掃描失敗 (non-fatal): {e}", flush=True)

    # === 2) 熱門題材 ===
    # 欄位: 題材, 平均%, 中位%, 上漲家數, 樣本數, 上漲比率% (0-100, 非 0-1!)
    try:
        themes_data = sector_pulse.compute_hot_themes()
        themes_df = themes_data.get("themes")
        theme_leaders = themes_data.get("leaders") or {}
        if themes_df is not None and not themes_df.empty:
            for _, row in themes_df.iterrows():
                theme_name = str(row.get("題材", ""))
                try:
                    avg = float(row.get("平均%", 0) or 0)
                    # 上漲比率% 是 0-100 scale, 要除 100 變 0-1
                    up_ratio_pct = float(row.get("上漲比率%", 0) or 0)
                    up_ratio = up_ratio_pct / 100.0
                    n = int(row.get("樣本數", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if (avg >= SECTOR_AVG_THRESHOLD
                        and up_ratio >= SECTOR_UP_RATIO_THRESHOLD
                        and n >= SECTOR_MIN_STOCKS):
                    sub = theme_leaders.get(theme_name)
                    candidates.append({
                        "sector_type": "theme",
                        "sector_name": theme_name,
                        "avg_pct": round(avg, 2),
                        "up_ratio": round(up_ratio, 3),
                        "n_stocks": n,
                        "leaders": _extract_leaders(sub),
                    })
    except Exception as e:
        print(f"[strong_sector] 題材掃描失敗 (non-fatal): {e}", flush=True)

    if not candidates:
        return []

    # === 3) 過濾已推過的 (per-day cap 1) ===
    # MED-3 fix: key 加 sector_type 前綴, 避免證交所 vs 題材未來同名衝突
    sectors_alerted = set(ssa_state.get("sectors_alerted") or [])
    fresh = [c for c in candidates if _dedup_key(c) not in sectors_alerted]

    if not fresh:
        return []

    # === 4) 排序取 top N (按 avg_pct 降冪) ===
    fresh.sort(key=lambda x: x["avg_pct"], reverse=True)
    selected = fresh[:SECTOR_MAX_PER_BATCH]

    # === 5) 標 severity ===
    for c in selected:
        if (c["avg_pct"] >= STRONG_AVG_THRESHOLD
                and c["up_ratio"] >= STRONG_UP_RATIO_THRESHOLD):
            c["severity"] = "strong"
        else:
            c["severity"] = "medium"

    # === 5.5) 給 leaders 加 entry_label (推播裡顯示「該檔現在能不能進」) ===
    try:
        import entry_label_helper as _el
        all_leader_syms = []
        for c in selected:
            for ld in c.get("leaders") or []:
                sid = str(ld.get("stock_id", ""))
                if sid:
                    all_leader_syms.append(sid)
        if all_leader_syms:
            pairs = [(s, "TW") for s in set(all_leader_syms)]
            eval_map = _el.batch_evaluate(pairs, max_workers=8)
            for c in selected:
                for ld in c.get("leaders") or []:
                    sid = str(ld.get("stock_id", ""))
                    ev = eval_map.get(sid) or {}
                    ld["entry_label"] = ev.get("entry_label", "—")
                    ld["entry_emoji"] = ev.get("entry_emoji", "")
                    ld["entry_score"] = ev.get("entry_score")
    except Exception as _e:
        print(f"[strong_sector] entry_label 計算失敗 (non-fatal): {_e}", flush=True)

    # HIGH fix: 不在這裡寫 state. 由 caller (market_open_alert) 在 send_message
    # 成功後呼叫 mark_sectors_sent() 才寫.
    return selected


def _dedup_key(c: Dict) -> str:
    """sectors_alerted dedup key — 結合 type + name, 避免證交所 vs 題材同名衝突."""
    return f"{c.get('sector_type', 'unknown')}:{c.get('sector_name', '')}"


def mark_sectors_sent(alerts: List[Dict]) -> None:
    """caller 在 send_message 成功後呼叫, 把已成功推送的族群登記進 state.

    這樣若 send 失敗, sectors_alerted 不會被污染, 下次 cron 仍會重試該族群.
    """
    if not alerts:
        return
    try:
        state = watchlist_store.load_monitor_state()
        ssa_state = state.setdefault("strong_sector_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        if ssa_state.get("date") != today_str:
            ssa_state.clear()
            ssa_state.update({"date": today_str, "sectors_alerted": [], "last_batch_at": None})
        sectors_alerted = set(ssa_state.get("sectors_alerted") or [])
        for a in alerts:
            sectors_alerted.add(_dedup_key(a))
        ssa_state["sectors_alerted"] = sorted(sectors_alerted)
        ssa_state["last_batch_at"] = dt.datetime.utcnow().isoformat()
        state["strong_sector_alert"] = ssa_state
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[strong_sector] mark_sectors_sent failed: {e}", flush=True)
