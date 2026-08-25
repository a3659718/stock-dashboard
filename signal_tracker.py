"""
signal_tracker.py
通用訊號 → 結果追蹤. 30 天滾動準確率.

設計原則:
  - 對所有訊號類型 (catalyst / strong_sector_leader / next_day_breakout 等) 用同一個 schema
  - 「預測 → 實際」用 stock_id 對應, N 個「交易日」後驗證
  - 紀錄存在 watchlist_store.monitor_state["signals"], 跨 cron persist

Schema (一筆記錄):
{
    "id": "uuid",
    "signal_type": "catalyst" | "strong_sector_leader" | "next_day_breakout" | ...,
    "stock_id": "2330",
    "name": "台積電",
    "predicted_at": "2026-05-09",      # 推播/預測日期 (TPE)
    "predicted_price": 1050,           # 推播當下的價格
    "expected_direction": "up" | "down",
    "evaluate_after_days": 1 | 3 | 5,  # 幾個「交易日」後驗證
    "evaluate_at": "2026-05-12",       # 只當粗篩用, 真正判斷看交易日 bar
    "actual_price": 1080,              # 驗證日收盤
    "actual_date": "2026-05-12",       # 驗證日 (實際那根日 K 的日期)
    "actual_pct": 2.86,
    "hit": true | false | null,        # null = 還沒驗證
    "extras": {
        "market": "TW" | "US",         # 沒給預設 TW
        "asof":   "2026-05-09",        # predicted_price 屬於哪一根日 K (見下)
        "source": "morning_tw_top5",   # 哪一封推播推的
        ...
    }
}

關於 extras["asof"] — 這是驗收正確與否的關鍵:
  推播價格不一定是「推播當天」的價格。盤前 08:00 晨報引用的是「前一交易日收盤」,
  盤中 09:32 選股引用的是「當天盤中價」, 盤後 15:03 引用的是「當天收盤」。
  所以「隔一天有沒有漲」必須以 *價格所屬的那根日 K* 為基準往後數, 而不是以推播日往後數。
  asof 沒給時退回 predicted_at。

用法:
  signal_tracker.record_signal(signal_type, stock_id, ...)
  signal_tracker.evaluate_pending()  # 每天盤後呼叫一次, 把到期的標記 hit
  signal_tracker.accuracy_summary(signal_type, lookback_days=30)
  signal_tracker.fmt_accuracy_block()  # 給推播末段用
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Dict, List, Optional, Tuple

import data_sources as ds


_STATE_KEY = "signals"
_MAX_RECORDS = 2000  # 防爆: 留最近 2000 筆

# 命中門檻 (%). 使用者定義:「隔天收盤價高於推薦當下價就算有漲」→ 0.0
# up   → actual_pct >  HIT_THRESHOLD_PCT
# down → actual_pct < -HIT_THRESHOLD_PCT
HIT_THRESHOLD_PCT = 0.0

# 一筆紀錄等多久還抓不到結果就放棄 (曆天). 避免下市/改代號的殭屍紀錄每天重抓。
_GIVE_UP_AFTER_DAYS = 20

# 單次 evaluate_pending 最多打幾個不同標的的行情 (控制 GitHub Actions 執行時間)
_MAX_SYMBOLS_PER_RUN = 80


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
            "actual_date": None,
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


# ===========================================================================
# 交易日 bar 工具
# ===========================================================================
# Bug fix (2026-08) — 這一整段取代了舊版的 `_fetch_eod_close_price()`:
#
#   舊版驗證邏輯是「evaluate_at (= predicted_at + N 個【曆天】) 到了, 就抓該檔
#   【最新】一根日 K 的收盤價來比」。兩個獨立的錯:
#
#   1. 曆天 ≠ 交易日。週四推、隔 1 天 → evaluate_at = 週五, 還算對; 但【週五推】
#      → evaluate_at = 週六, 而週六抓到的「最新收盤」就是週五自己的收盤 →
#      actual_pct 恆等於 0 → `0 > 0.5` 為 False → 每週五推的每一檔都被記成
#      「沒漲 / miss」。長期下來滾動勝率被系統性壓低, 而且完全看不出來。
#      國定假日 (春節那種連假) 同理, 錯得更誇張。
#
#   2. 「抓最新一根」而不是「抓該驗證日那一根」。evaluate_pending() 只在
#      heartbeat (14:32 TPE) 與台股盤後跑, 一旦某天 workflow 沒跑成功 (GitHub
#      Actions 排隊/失敗), 隔幾天才補驗證時, 拿到的是【那幾天後】的收盤價,
#      而不是「隔一天」的收盤價 — 驗的已經不是原本那個問題了。
#      舊 code 裡的 `trade_date` 參數其實從來沒有任何呼叫端傳過, 是死參數。
#
#   3. 美股從來沒被驗證過。舊 `_is_market_closed_now("US")` 要求 UTC ≥ 21:00,
#      但 evaluate_pending() 的兩個呼叫點 (heartbeat 06:32 UTC / 台股盤後
#      07:03 UTC) 都不滿足 → 美股標的一律回 None → 永遠 pending → 美股推薦
#      的命中率永遠是空的。
#
#   新版改成直接抓日 K 序列, 以 asof 那根 bar 為基準往後數 N 根「真的有交易的
#   bar」。週末 / 假日 / 補班日全部自動正確, 不需要維護行事曆; 補跑也不會失真。
# ---------------------------------------------------------------------------

# (stock_id, market) -> [(date_str, close, high, low)]  單次 process 內快取
_BARS_CACHE: Dict[Tuple[str, str], List[Tuple[str, float, float, float]]] = {}


def _us_eastern_now() -> dt.datetime:
    """美東當下時間 (粗略 DST 判斷, 只用來擋未收盤的 partial bar)."""
    now_utc = dt.datetime.utcnow()
    try:
        import index_alerts
        dst = index_alerts._is_us_in_dst(now_utc.date())
    except Exception:
        # 3 月中 ~ 11 月初 當 DST (誤差只影響切換那一週的 partial-bar 保護)
        dst = 3 <= now_utc.month <= 10
    return now_utc - dt.timedelta(hours=4 if dst else 5)


def _partial_bar_date(market: str) -> Optional[str]:
    """回傳「這根日 K 現在還沒定案」的日期字串 (沒有就 None).

    盤中抓 yfinance 日線, 最後一根是【今天的未完成 bar】(close = 現價). 拿它當
    「收盤價」會讓驗收結果隨盤中跳動, 收盤後又變一個數字。這裡把它辨識出來排除。
    """
    if str(market).upper() == "US":
        et = _us_eastern_now()
        # 16:00 ET 收盤, 給 30 分鐘讓 yfinance 定案
        if et.hour < 16 or (et.hour == 16 and et.minute < 30):
            return et.strftime("%Y-%m-%d")
        return None
    # TW: 13:30 收盤, 給 30 分鐘 buffer
    tpe = dt.datetime.utcnow() + dt.timedelta(hours=8)
    if tpe.hour < 14:
        return tpe.strftime("%Y-%m-%d")
    return None


def _extract_bars(df) -> List[Tuple[str, float, float, float]]:
    """把 fetch_yf_history 回的 DataFrame 轉成 [(date, close, high, low)]."""
    if df is None or getattr(df, "empty", True):
        return []
    cols = {str(c).lower(): c for c in df.columns}
    close_col = cols.get("close")
    if close_col is None:
        return []
    high_col = cols.get("high", close_col)
    low_col = cols.get("low", close_col)
    date_col = cols.get("date") or cols.get("datetime") or cols.get("index")
    out: List[Tuple[str, float, float, float]] = []
    for i in range(len(df)):
        try:
            if date_col is not None:
                d = df[date_col].iloc[i]
            else:
                d = df.index[i]
            date_str = str(d)[:10]
            if len(date_str) != 10 or date_str[4] != "-":
                continue
            c = float(df[close_col].iloc[i])
            h = float(df[high_col].iloc[i])
            lo = float(df[low_col].iloc[i])
            if c != c:  # NaN
                continue
            out.append((date_str, c, h, lo))
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    return out


def fetch_daily_bars(stock_id: str, market: str = "TW",
                       period: str = "3mo") -> List[Tuple[str, float, float, float]]:
    """抓日 K 序列 [(date, close, high, low)], 已排序且已排除盤中未定案的 bar.

    TW 會自動試 .TW / .TWO 兩種後綴; US 直接用 symbol。抓不到回 []。
    """
    market = str(market or "TW").upper()
    key = (str(stock_id), market)
    if key in _BARS_CACHE:
        return _BARS_CACHE[key]
    sid = str(stock_id)
    # 已經是完整代號的不要再加後綴: 指數 (^TWII / ^SOX) 或本來就帶 .TW/.TWO 的。
    # (漏了這個判斷會把 ^TWII 變成 ^TWII.TW → 永遠抓不到 → 盤前推播的 asof 拿不到。)
    if market == "US" or sid.startswith("^") or "." in sid:
        symbols = [sid]
    else:
        symbols = [f"{sid}.TW", f"{sid}.TWO"]
    bars: List[Tuple[str, float, float, float]] = []
    for sym in symbols:
        try:
            df = ds.fetch_yf_history(sym, period=period, interval="1d")
        except Exception as e:
            print(f"[signal_tracker] fetch {sym} failed: {e}", flush=True)
            continue
        bars = _extract_bars(df)
        if bars:
            break
    partial = _partial_bar_date(market)
    if partial and bars and bars[-1][0] >= partial:
        bars = [b for b in bars if b[0] < partial]
    _BARS_CACHE[key] = bars
    return bars


def clear_bars_cache() -> None:
    """清掉本 process 的日 K 快取 (長時間執行的 Streamlit 用)."""
    _BARS_CACHE.clear()


def nth_bar_after(bars: List[Tuple[str, float, float, float]], asof: str,
                    n: int = 1) -> Optional[Tuple[str, float, float, float]]:
    """asof 之後第 n 根交易日 bar. 還沒發生 (或資料還沒到) 回 None."""
    after = [b for b in bars if b[0] > asof]
    if len(after) < max(1, int(n)):
        return None
    return after[int(n) - 1]


def _record_asof(r: Dict) -> str:
    """這筆紀錄的 predicted_price 屬於哪一根日 K."""
    return str((r.get("extras") or {}).get("asof") or r.get("predicted_at") or "")


def _record_market(r: Dict) -> str:
    return "US" if str((r.get("extras") or {}).get("market", "")).upper() == "US" else "TW"


# 向後相容 — 舊呼叫端 (如果還有) 拿「最近一根完整日 K 收盤」
def _fetch_eod_close_price(stock_id: str, market: str = "TW",
                             trade_date: Optional[str] = None) -> Optional[float]:
    bars = fetch_daily_bars(stock_id, market)
    if not bars:
        return None
    if trade_date:
        for d, c, _h, _l in bars:
            if d == trade_date:
                return c
        return None
    return bars[-1][1]


_fetch_close_price = _fetch_eod_close_price


def evaluate_pending() -> int:
    """掃所有還沒結果的紀錄, 用「交易日 bar」補上 actual. 回驗證了幾筆.

    判定: 以 extras["asof"] (沒有就 predicted_at) 那根日 K 為基準, 取之後第
    evaluate_after_days 根交易日 bar 的收盤價, 跟 predicted_price 比。
    那根 bar 還沒出現 → 保持 pending, 下次再驗 (不會誤判成沒漲)。
    """
    today = _today_str()

    # Phase 1: 先算出每筆要用哪個價 (這段沒鎖, 慢操作不卡 lock)
    state_snap = _load_state()
    records_snap: List[Dict] = state_snap.get(_STATE_KEY, []) or []

    # 需要抓行情的 (stock_id, market) — 依 pending 記錄去重, 並限流
    wanted: List[Tuple[str, str]] = []
    seen_keys = set()
    for r in records_snap:
        if r.get("hit") is not None or (r.get("extras") or {}).get("_no_data"):
            continue
        if not r.get("predicted_price"):
            continue
        key = (str(r.get("stock_id", "")), _record_market(r))
        if not key[0] or key in seen_keys:
            continue
        seen_keys.add(key)
        wanted.append(key)
        if len(wanted) >= _MAX_SYMBOLS_PER_RUN:
            break

    resolved: Dict[str, Tuple[str, float]] = {}   # record id -> (date, close)
    no_data_ids: List[str] = []
    bars_by_key: Dict[Tuple[str, str], List] = {}
    for key in wanted:
        bars_by_key[key] = fetch_daily_bars(key[0], key[1])

    for r in records_snap:
        if r.get("hit") is not None or (r.get("extras") or {}).get("_no_data"):
            continue
        pred = r.get("predicted_price")
        if not pred:
            continue
        key = (str(r.get("stock_id", "")), _record_market(r))
        if key not in bars_by_key:
            continue  # 這輪沒輪到 (限流), 下次再說
        bars = bars_by_key[key]
        asof = _record_asof(r)
        n = int(r.get("evaluate_after_days") or 1)
        hit_bar = nth_bar_after(bars, asof, n) if (bars and asof) else None
        if hit_bar is not None:
            resolved[r.get("id")] = (hit_bar[0], hit_bar[1])
        else:
            # 太久還驗不出來 (下市 / 改代號 / 資料源長期抓不到) → 放棄, 不再重試
            try:
                age = (dt.datetime.strptime(today, "%Y-%m-%d").date()
                       - dt.datetime.strptime(r.get("predicted_at", today), "%Y-%m-%d").date()).days
            except Exception:
                age = 0
            if age > _GIVE_UP_AFTER_DAYS:
                no_data_ids.append(r.get("id"))

    if not resolved and not no_data_ids:
        return 0

    # Phase 2: 持鎖 mutate (快操作)
    def _mutate(records: List[Dict]) -> int:
        n = 0
        no_data_set = set(no_data_ids)
        for r in records:
            rid = r.get("id")
            if r.get("hit") is not None:
                continue
            if rid in no_data_set:
                r.setdefault("extras", {})["_no_data"] = True
                n += 1
                continue
            got = resolved.get(rid)
            if not got:
                continue
            pred = r.get("predicted_price")
            if not pred:
                continue
            actual_date, price = got
            actual_pct = (price / pred - 1) * 100
            direction = r.get("expected_direction", "up")
            if direction == "up":
                hit = actual_pct > HIT_THRESHOLD_PCT
            else:
                hit = actual_pct < -HIT_THRESHOLD_PCT
            r["actual_price"] = round(float(price), 2)
            r["actual_date"] = actual_date
            r["actual_pct"] = round(float(actual_pct), 2)
            r["hit"] = bool(hit)
            n += 1
        return n
    return _mutate_signals_atomically(_mutate)


def pending_count() -> int:
    """還沒驗證的筆數 (排除已放棄的)."""
    records = _load_state().get(_STATE_KEY, []) or []
    return sum(1 for r in records
               if r.get("hit") is None and not (r.get("extras") or {}).get("_no_data"))


def load_records() -> List[Dict]:
    """回目前所有訊號紀錄 (唯讀用)."""
    return list(_load_state().get(_STATE_KEY, []) or [])


def accuracy_summary(signal_type: Optional[str] = None,
                      lookback_days: int = 30,
                      market: Optional[str] = None) -> Dict:
    """指定 signal_type 的滾動準確率 (None = 全部). market 可再篩 TW / US."""
    records: List[Dict] = load_records()
    # Bug fix: 跟 _today_str() 同理, 改用 TPE 日期算 cutoff, 避免跟系統本地
    # (UTC, GitHub Actions) 日期不一致造成滾動視窗邊界悄悄偏移一天。
    today_tpe = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    cutoff = today_tpe - dt.timedelta(days=lookback_days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    want_market = str(market).upper() if market else None
    total = 0
    hit = 0
    pct_sum = 0.0
    for r in records:
        if r.get("hit") is None:
            continue
        if r.get("predicted_at", "0") < cutoff_str:
            continue
        if signal_type and r.get("signal_type") != signal_type:
            continue
        if want_market and _record_market(r) != want_market:
            continue
        total += 1
        if r.get("hit"):
            hit += 1
        try:
            pct_sum += float(r.get("actual_pct") or 0)
        except (TypeError, ValueError):
            pass
    pct = round(hit / total * 100, 1) if total else None
    return {
        "signal_type": signal_type or "ALL",
        "market": want_market or "ALL",
        "n": total, "hit": hit, "pct": pct,
        "avg_pct": round(pct_sum / total, 2) if total else None,
        "lookback_days": lookback_days,
    }


def fmt_accuracy_block(signal_types: Optional[List[str]] = None,
                        lookback_days: int = 30) -> str:
    """格式化「本月各訊號準確率」一段給推播末用."""
    # 注意: limit_up_precursor 「刻意」不放進這個預設清單。這個 fmt_accuracy_block()
    # 會被 tw_post_market_summary.py / market_open_picks.py 直接組進真的會送出的
    # Telegram 推播裡 — 使用者明確要求 limit_up_precursor 現階段只留在 dashboard,
    # 不要任何形式自動出現在推播裡, 即使只是「命中率 60%」這種不點名個股的彙總數字
    # 也算。要在 dashboard 上手動看它的準確率, 呼叫
    # fmt_accuracy_block(signal_types=["limit_up_precursor"]) 或直接呼叫
    # accuracy_summary("limit_up_precursor") 即可 (label_map 已經備好)。
    types_to_show = signal_types or [
        "catalyst", "strong_sector_leader", "next_day_breakout",
        "avoid_pick", "potential_pick",
        "morning_tw_top5", "pre_market_buy", "us_buy_picks",
    ]
    lines = []
    for st in types_to_show:
        s = accuracy_summary(st, lookback_days=lookback_days)
        if s["n"] >= 5:
            label = SIGNAL_LABELS.get(st, st)
            mark = "🟢" if s["pct"] and s["pct"] >= 60 else ("🟡" if s["pct"] and s["pct"] >= 40 else "🔴")
            lines.append(f"  {mark} {label}: {s['hit']}/{s['n']} ({s['pct']}%)")
    if not lines:
        return ""
    return "\n".join([f"<b>近 {lookback_days} 天訊號表現</b>"] + lines)


# signal_type → 中文說明 (推播 / dashboard 共用)
SIGNAL_LABELS: Dict[str, str] = {
    "catalyst": "催化劑利多→3日漲",
    "strong_sector_leader": "強勢族群龍頭→隔日漲",
    "next_day_breakout": "隔日上漲 Top 3→隔日漲",
    "avoid_pick": "避開訊號→3日跌",
    "potential_pick": "潛力股→5日漲",
    "limit_up_precursor": "漲停前兆→5日漲",
    "morning_tw_top5": "晨報台股 Top 5",
    "pre_market_buy": "盤前台股可買 Top 5",
    "us_buy_picks": "美股盤前 BUY Top 5",
    "intraday_strong_stock": "盤中強勢股",
    "intraday_weak_short": "盤中弱勢放空",
}


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
                  expected_direction: str = "up",
                  market: str = "TW",
                  source: str = "",
                  asof: Optional[str] = None) -> int:
    """批次 record_signal — 給推播一次推 N 個個股時用.

    market / source / asof 會寫進 extras, 隔日驗收 (pick_review) 靠這三個欄位分類。
    asof = predicted_price 屬於哪一根日 K (見模組 docstring); 不給就用推播日。
    """
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
        if price_f is None or price_f <= 0:
            # 沒有推薦當下價就無法驗收 — 記了也只會變殭屍紀錄
            continue
        extras = {"market": str(market).upper(), "source": source or signal_type}
        if asof:
            extras["asof"] = asof
        rid = record_signal(
            signal_type=signal_type,
            stock_id=sid,
            name=name,
            predicted_price=price_f,
            expected_direction=expected_direction,
            evaluate_after_days=evaluate_after_days,
            extras=extras,
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
