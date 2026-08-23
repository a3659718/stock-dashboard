"""
signal_tracker.py
通用訊號 → 結果追蹤. 30 天滾動準確率.

設計原則:
  - 對所有訊號類型 (catalyst / strong_sector_leader / next_day_breakout 等) 用同一個 schema
  - 「預測 → 實際」用 stock_id 對應, N 天後驗證 (預測時設好 evaluate_after_days)
  - 紀錄存在 watchlist_store.monitor_state["signals"], 跨 cron persist

Schema (一筆記錄):
{
    "id": "uuid",
    "signal_type": "catalyst" | "strong_sector_leader" | "next_day_breakout" | ...,
    "stock_id": "2330",
    "name": "台積電",
    "predicted_at": "2026-05-09",      # 預測日期
    "predicted_price": 1050,           # 預測時的收盤價
    "expected_direction": "up" | "down",
    "evaluate_after_days": 1 | 3 | 5,  # 幾天後驗證
    "evaluate_at": "2026-05-12",
    "actual_price": 1080,              # 驗證日收盤
    "actual_pct": 2.86,
    "hit": true | false | null,        # null = 還沒驗證
    "extras": {...}                    # 額外 metadata (e.g. 機率 / 信心 / R:R)
}

用法:
  signal_tracker.record_signal(signal_type, stock_id, ...)
  signal_tracker.evaluate_pending()  # 在每天盤後呼叫一次, 把到期的標記 hit
  signal_tracker.accuracy_summary(signal_type, lookback_days=30)
  signal_tracker.fmt_accuracy_block()  # 給推播末段用
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Dict, List, Optional

import data_sources as ds


_STATE_KEY = "signals"
_MAX_RECORDS = 2000  # 防爆: 留最近 2000 筆


def _load_state() -> Dict:
    try:
        import watchlist_store
        return watchlist_store.load_monitor_state()
    except Exception:
        return {}


def _save_state(state: Dict) -> None:
    try:
        import watchlist_store
        watchlist_store.save_monitor_state(state)
    except Exception as e:
        print(f"[signal_tracker] save failed: {e}", flush=True)


def _mutate_signals_atomically(mutate_fn) -> int:
    """以 file-lock 保護的 read-modify-write, 防 cron + Streamlit 同時 lost update.

    `mutate_fn(records: list) -> int` 接 mutable list, return 變動的筆數. 函式內可
    直接修改 list (append / 改值) — 在持有 lock 期間做完 reload 就不會被覆蓋.

    Lock 用 fcntl (Unix) 或 portalocker (Windows fallback). 失敗就 fall back 到
    無 lock 模式 (不致 raise, 但有 race 風險).
    """
    import os, time
    n_changed = 0
    try:
        import watchlist_store
        lock_path = str(watchlist_store.MONITOR_STATE_FILE) + ".lock"
    except Exception:
        # 連 watchlist_store 都掛 — fall back 無鎖模式
        state = _load_state()
        records = state.setdefault(_STATE_KEY, [])
        n_changed = mutate_fn(records)
        if n_changed:
            state[_STATE_KEY] = records
            _save_state(state)
        return n_changed

    # 嘗試開 lock file 並排他鎖定 (跨 process)
    fd = None
    locked = False
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            import fcntl
            for _ in range(20):  # 最多等 1 秒 (20 × 50ms)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    time.sleep(0.05)
        except ImportError:
            # Windows: 用 portalocker (若沒裝就 skip lock, fall back race-prone)
            try:
                import portalocker
                portalocker.lock(fd, portalocker.LOCK_EX)
                locked = True
            except Exception:
                pass

        # 持有 lock 後再 read → mutate → save (atomic 區段)
        state = _load_state()
        records = state.setdefault(_STATE_KEY, [])
        n_changed = mutate_fn(records)
        if n_changed:
            state[_STATE_KEY] = records
            _save_state(state)
    except Exception as e:
        print(f"[signal_tracker] _mutate_signals_atomically failed: {e}", flush=True)
        # fall back to non-locked
        state = _load_state()
        records = state.setdefault(_STATE_KEY, [])
        n_changed = mutate_fn(records)
        if n_changed:
            state[_STATE_KEY] = records
            _save_state(state)
    finally:
        if fd is not None:
            try:
                if locked:
                    try:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except ImportError:
                        try:
                            import portalocker
                            portalocker.unlock(fd)
                        except Exception:
                            pass
                os.close(fd)
            except Exception:
                pass
    return n_changed


def _today_str() -> str:
    # Bug fix: 原本用 dt.date.today() (執行機器的系統本地日期). 本專案實際跑在
    # GitHub Actions (UTC), 台北 00:00-07:59 這段時間 UTC 還是「前一天」, 會讓
    # predicted_at 記到比台北實際交易日晚一天的日期 — 同一個台北交易日可能因為
    # 橫跨 UTC 日界被切成兩天, 造成 evaluate_at 的驗證窗口提前/延後一天觸發,
    # accuracy_summary() 算出的滾動勝率也悄悄跟著錯. 改成跟 push_cap._today_tpe()
    # 一致, 統一用 TPE (UTC+8) 日期。
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def _add_days(d: str, n: int) -> str:
    base = dt.datetime.strptime(d, "%Y-%m-%d").date()
    return (base + dt.timedelta(days=n)).strftime("%Y-%m-%d")


def record_signal(signal_type: str, stock_id: str, name: str = "",
                   predicted_price: Optional[float] = None,
                   expected_direction: str = "up",
                   evaluate_after_days: int = 3,
                   extras: Optional[Dict] = None) -> Optional[str]:
    """記一筆預測, 回 record id (失敗回 None).

    用 _mutate_signals_atomically 保證 cron + Streamlit 同時 record 不會 lost update.
    """
    if not stock_id or not signal_type:
        return None
    today = _today_str()
    rec_id_holder = [None]  # closure 用

    def _mutate(records: List[Dict]) -> int:
        # Dedup: 同一天 同一 stock_id + signal_type 只記一筆
        for r in records:
            if (r.get("predicted_at") == today
                    and r.get("signal_type") == signal_type
                    and str(r.get("stock_id", "")) == str(stock_id)):
                rec_id_holder[0] = r.get("id")
                return 0  # 已存在 → 不變動

        rec_id_holder[0] = str(uuid.uuid4())[:8]
        rec = {
            "id": rec_id_holder[0],
            "signal_type": signal_type,
            "stock_id": str(stock_id),
            "name": str(name or ""),
            "predicted_at": today,
            "predicted_price": float(predicted_price) if predicted_price is not None else None,
            "expected_direction": expected_direction,
            "evaluate_after_days": int(evaluate_after_days),
            "evaluate_at": _add_days(today, int(evaluate_after_days)),
            "actual_price": None,
            "actual_pct": None,
            "hit": None,
            "extras": dict(extras or {}),
        }
        records.append(rec)
        # Cap — 保留所有 pending (hit=None) + 最近的 validated
        if len(records) > _MAX_RECORDS:
            pending = [r for r in records if r.get("hit") is None]
            validated = [r for r in records if r.get("hit") is not None]
            keep_validated = max(0, _MAX_RECORDS - len(pending))
            records[:] = pending + validated[-keep_validated:]
        return 1

    _mutate_signals_atomically(_mutate)
    return rec_id_holder[0]


def _is_market_closed_now(market: str = "TW") -> bool:
    """判斷該市場是否已收盤 (用來決定要不要評估 today 的訊號).

    TW: UTC ≥ 06:00 (= 14:00 TPE 之後, 13:30 收盤後加 30 min buffer)
    US: UTC ≥ 21:00 EDT 期 / ≥ 22:00 EST (16:00 ET 收盤後加 1 hr buffer)
    """
    now_utc = dt.datetime.now(dt.timezone.utc)
    if market == "US":
        # 用 index_alerts 的 DST 偵測
        try:
            import index_alerts
            dst = index_alerts._is_us_in_dst(now_utc.date())
        except Exception:
            dst = True  # 假設 DST (容錯)
        return now_utc.hour >= (21 if dst else 22)
    return now_utc.hour >= 6  # TW


def _fetch_eod_close_price(stock_id: str, market: str = "TW",
                             trade_date: Optional[str] = None) -> Optional[float]:
    """抓「指定日期或最近一個完整交易日」的收盤價.

    重要: 不能直接 iloc[-1] — 盤中跑會拿到 partial-bar (今日 high/low/close 還沒定案).
    解法:
      1. 如果該市場 not closed yet → 不評估 (回 None)
      2. 已收盤但 yfinance 最後一根還是「今日 partial」(罕見, 邊界 case) → 用 iloc[-2]
    """
    if not _is_market_closed_now(market):
        return None
    try:
        if market == "US":
            df = ds.fetch_yf_history(stock_id, period="5d", interval="1d")
            if df is None or df.empty:
                return None
            return float(df["Close"].astype(float).iloc[-1])
        for suffix in [".TW", ".TWO"]:
            df = ds.fetch_yf_history(f"{stock_id}{suffix}", period="5d", interval="1d")
            if df is None or df.empty:
                continue
            return float(df["Close"].astype(float).iloc[-1])
        return None
    except Exception:
        return None


# 向後相容 (萬一其他地方有 import)
_fetch_close_price = _fetch_eod_close_price


def evaluate_pending() -> int:
    """掃所有 evaluate_at <= 今日 的待驗證紀錄, 抓現價算 hit. 回驗證了幾筆.

    用 _mutate_signals_atomically 保證跟 record_signal 不會 lost update.
    """
    today = _today_str()

    # Phase 1: pre-fetch 所有需要 evaluate 的價格 (這段沒鎖, 慢操作不卡 lock)
    state_snap = _load_state()
    records_snap: List[Dict] = state_snap.get(_STATE_KEY, []) or []
    price_map: Dict[str, Optional[float]] = {}
    for r in records_snap:
        if r.get("hit") is not None:
            continue
        if r.get("evaluate_at", "9999-99-99") > today:
            continue
        sid = r.get("stock_id", "")
        if not sid or sid in price_map:
            continue
        market = "US" if r.get("extras", {}).get("market") == "US" else "TW"
        price_map[sid] = _fetch_eod_close_price(sid, market)

    # Phase 2: 持鎖 mutate (快操作, 用 phase1 抓好的 price_map)
    def _mutate(records: List[Dict]) -> int:
        n = 0
        for r in records:
            if r.get("hit") is not None:
                continue
            if r.get("evaluate_at", "9999-99-99") > today:
                continue
            sid = r.get("stock_id", "")
            price = price_map.get(sid)
            if price is None:
                continue
            pred = r.get("predicted_price")
            if not pred:
                continue
            actual_pct = (price / pred - 1) * 100
            direction = r.get("expected_direction", "up")
            if direction == "up":
                hit = actual_pct > 0.5
            else:
                hit = actual_pct < -0.5
            r["actual_price"] = round(float(price), 2)
            r["actual_pct"] = round(float(actual_pct), 2)
            r["hit"] = bool(hit)
            n += 1
        return n
    return _mutate_signals_atomically(_mutate)


def accuracy_summary(signal_type: Optional[str] = None,
                      lookback_days: int = 30) -> Dict:
    """指定 signal_type 的滾動準確率 (None = 全部)."""
    state = _load_state()
    records: List[Dict] = state.get(_STATE_KEY, []) or []
    # Bug fix: 跟 _today_str() 同理, 改用 TPE 日期算 cutoff, 避免跟系統本地
    # (UTC, GitHub Actions) 日期不一致造成滾動視窗邊界悄悄偏移一天。
    today_tpe = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    cutoff = today_tpe - dt.timedelta(days=lookback_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    total = 0
    hit = 0
    for r in records:
        if r.get("hit") is None:
            continue
        if r.get("predicted_at", "0") < cutoff_str:
            continue
        if signal_type and r.get("signal_type") != signal_type:
            continue
        total += 1
        if r.get("hit"):
            hit += 1
    pct = round(hit / total * 100, 1) if total else None
    return {
        "signal_type": signal_type or "ALL",
        "n": total, "hit": hit, "pct": pct,
        "lookback_days": lookback_days,
    }


def fmt_accuracy_block(signal_types: Optional[List[str]] = None,
                        lookback_days: int = 30) -> str:
    """格式化「本月各訊號準確率」一段給推播末用."""
    types_to_show = signal_types or [
        "catalyst", "strong_sector_leader", "next_day_breakout",
        "avoid_pick", "potential_pick",
    ]
    lines = []
    for st in types_to_show:
        s = accuracy_summary(st, lookback_days=lookback_days)
        if s["n"] >= 5:
            label_map = {
                "catalyst": "催化劑利多→3日漲",
                "strong_sector_leader": "強勢族群龍頭→隔日漲",
                "next_day_breakout": "隔日上漲 Top 3→隔日漲",
                "avoid_pick": "避開訊號→3日跌",
                "potential_pick": "潛力股→5日漲",
            }
            label = label_map.get(st, st)
            mark = "🟢" if s["pct"] and s["pct"] >= 60 else ("🟡" if s["pct"] and s["pct"] >= 40 else "🔴")
            lines.append(f"  {mark} {label}: {s['hit']}/{s['n']} ({s['pct']}%)")
    if not lines:
        return ""
    return "\n".join([f"<b>近 {lookback_days} 天訊號表現</b>"] + lines)



def fmt_compact_perf(signal_type: str, lookback_days: int = 30,
                       min_n: int = 5, **_kwargs) -> str:
    """單一 signal_type 的精簡績效一行 — 給推播末段塞.

    格式: "📊 歷史 30d 勝率 65% (n=23)"
    """
    s = accuracy_summary(signal_type, lookback_days=lookback_days)
    n = s.get("n") or 0
    pct = s.get("pct")
    if n == 0:
        return ""
    if n < min_n or pct is None:
        return f"📊 歷史表現: 樣本累積中 ({n} 筆)"
    mark = "🟢" if pct >= 60 else ("🟡" if pct >= 40 else "🔴")
    return f"📊 {mark} 歷史 {lookback_days}d 勝率 {pct:.0f}% (n={n})"


def record_batch(signal_type: str, items: List[Dict],
                  evaluate_after_days: int = 5,
                  expected_direction: str = "up") -> int:
    """批次 record_signal — 給推播一次推 N 個個股時用."""
    if not items:
        return 0
    added = 0
    for it in items:
        sid = str(it.get("stock_id") or it.get("symbol", "")).strip()
        if not sid:
            continue
        name = it.get("name", "")
        price = it.get("current") or it.get("predicted_price") or it.get("price")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        rid = record_signal(
            signal_type=signal_type,
            stock_id=sid,
            name=name,
            predicted_price=price_f,
            expected_direction=expected_direction,
            evaluate_after_days=evaluate_after_days,
        )
        if rid:
            added += 1
    return added


def reset_all() -> int:
    """清空所有訊號紀錄. 回原本筆數."""
    def _mutate(records):
        n = len(records)
        records.clear()
        return n
    return _mutate_signals_atomically(_mutate)
