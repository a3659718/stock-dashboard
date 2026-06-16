"""
index_alerts.py
大盤點數增量警報 + 加密貨幣劇烈波動警報。

【亞股】當日漲跌每超過閾值就跳通知 (亞股交易時段內監控):
  日經 225 (^N225)    每 ±150 點
  韓國 KOSPI (^KS11)  每 ±50 點
  台股加權 (^TWII)    每 ±100 點

【美股】當日漲跌每超過閾值就跳通知 (美股交易時段內監控):
  費城半導體 (^SOX)   每 ±100 點 (~1.7%, 台股 leading indicator)
  那斯達克 (^IXIC)    每 ±200 點 (~1%)

【加密貨幣】當日 ±2.5% 就跳通知 (24/7):
  BTC-USD
  ETH-USD

連續同方向跳 N 次 → 視為強勢趨勢，加上「持續 X 連跌/連漲」警示文字。

State: monitor_state["index_alerts"], monitor_state["crypto_alerts"]
       每天的開盤價當 base, 收盤後重置.

安全機制 (per-market 交易時段判斷):
  - TW: 09:00-13:30 TPE  = 01:00-05:30 UTC
  - JP: 09:00-15:00 JST  = 00:00-06:00 UTC
  - KR: 09:00-15:30 KST  = 00:00-06:30 UTC
  - US: 09:30-16:00 ET   = 13:30-20:00 UTC (EDT) / 14:30-21:00 UTC (EST)
  非交易時段 → 跳過, 避免抓收盤後 stale data 觸發假警報。
  亞股 + 美股完全不重疊 (06:30-13:30 UTC 為空窗, 此時段只有加密貨幣監控)。
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import data_sources as ds
import watchlist_store


# 配置
# 欄位:
#   threshold:           bucket alert 基礎點數門檻
#   disable_atr_boost:   True 時不套 ATR 動態 boost (避免高波動日門檻反而升高)
#   reversal_pct:        per-symbol reversal 門檻 (default REVERSAL_THRESHOLD_PCT = 1.0)
# 為什麼 TWII 用 200 + disable_atr_boost: 使用者反映高波動日 ATR boost 拉到 240-300 點,
#                                          200-400 點波動全被吞掉; 改固定 200 + 反轉 0.5%
INDEX_CONFIG = {
    # 亞股
    # M3 fix: 把各指數 reversal_pct 都顯式設定, 避免 default 1.0% 漏掉「漲幅萎縮」
    # P1 砍噪音: reversal 門檻全面提高 (專業推播標準, 0.5% 太敏感一天 3-5 次)
    "^N225":  {"name": "日經 225",   "threshold": 150.0, "country": "JP",
               "reversal_pct": 1.2},  # 中等: 0.8→1.2 (N225)
    "^KS11":  {"name": "韓國 KOSPI", "threshold": 50.0,  "country": "KR",
               "reversal_pct": 0.8},  # 中等: 0.6→0.8 (KS11)
    "^TWII":  {"name": "台灣加權",   "threshold": 200.0, "country": "TW",
               "disable_atr_boost": True, "reversal_pct": 1.0},  # 中等: 0.7→1.0 TWII
    # 美股
    "^SOX":   {"name": "費城半導體", "threshold": 100.0, "country": "US",
               "reversal_pct": 0.8},  # 中等: 0.6→0.8 SOX
    "^IXIC":  {"name": "那斯達克",   "threshold": 200.0, "country": "US",
               "reversal_pct": 1.2},  # 中等: 0.8→1.2 IXIC
}


# ---------------------------------------------------------------------------
# 各市場交易時段判斷 (避免閉市時抓 stale data 觸發假警報)
# ---------------------------------------------------------------------------
def _is_us_in_dst(today: Optional[dt.date] = None) -> bool:
    """美國 DST: 3 月第 2 週日 ~ 11 月第 1 週日."""
    today = today or dt.date.today()
    year = today.year
    # 3 月第 2 週日
    mar1 = dt.date(year, 3, 1)
    days_to_first_sun = (6 - mar1.weekday()) % 7  # weekday: Mon=0..Sun=6
    dst_start = mar1 + dt.timedelta(days=days_to_first_sun + 7)
    # 11 月第 1 週日
    nov1 = dt.date(year, 11, 1)
    days_to_first_sun = (6 - nov1.weekday()) % 7
    dst_end = nov1 + dt.timedelta(days=days_to_first_sun)
    return dst_start <= today < dst_end


def _is_market_in_session(country: str) -> bool:
    """各市場是否在自己交易時段內.
    TW: 09:00-13:30 TPE  = 01:00-05:30 UTC
    JP: 09:00-15:00 JST  = 00:00-06:00 UTC
    KR: 09:00-15:30 KST  = 00:00-06:30 UTC
    US: 09:30-16:00 ET   = 13:30-20:00 UTC (EDT) / 14:30-21:00 UTC (EST)
    週末一律不在 session.
    """
    now_utc = dt.datetime.now(dt.timezone.utc)
    cur = now_utc.hour + now_utc.minute / 60.0
    c = country.upper()
    if c == "US":
        # 美股 weekend 邊界要用「美東日期」, 不能用 UTC 日期 —
        # 例: UTC Sat 02:00 = ET Fri 22:00 (理論上盤後), 美股還是「週五」, 不算週末.
        # 但實務上美股 16:00 ET (= 20:00/21:00 UTC) 就收盤, 之後不在 session, 所以
        # 加上 weekday 判斷 (對 ET 日期判) 就足夠安全.
        # DST 切換邊界: 11/3、3 月第二週日, 在 UTC 跨 ET-day 那 4-5 小時可能誤判 1 hr,
        # 用 ET-date 算 DST 才完全乾淨.
        # 估算 ET 偏移 (DST 期 -4h, 否則 -5h). 先用 UTC 日期粗估 DST, 再以該偏移
        # 反推 ET 日期, 用 ET 日期重算 DST (二次校正 — 邊界處才會生效).
        is_dst = _is_us_in_dst(now_utc.date())
        et_offset_hr = -4 if is_dst else -5
        et_now = now_utc + dt.timedelta(hours=et_offset_hr)
        if et_now.weekday() >= 5:  # 美東週末
            return False
        is_dst = _is_us_in_dst(et_now.date())
        # 切換完 DST 後, et_offset 也要重算 (極少數 case 跨日)
        et_offset_hr = -4 if is_dst else -5
        et_now = now_utc + dt.timedelta(hours=et_offset_hr)
        # 09:30 ~ 16:00 ET
        et_cur = et_now.hour + et_now.minute / 60.0
        return 9.5 <= et_cur < 16.0
    # 亞股 — UTC weekend 一律不算 (TW/JP/KR 時區只比 UTC 早 8-9 hr, 週末邊界不會誤判)
    if now_utc.weekday() >= 5:
        return False
    if c == "TW":
        return 1.0 <= cur < 5.5
    elif c == "JP":
        return 0.0 <= cur < 6.0
    elif c == "KR":
        return 0.0 <= cur < 6.5
    return False


def _is_us_trading_hours() -> bool:
    """美股是否在交易時段 — 保留 backward-compat alias."""
    return _is_market_in_session("US")


def get_active_session() -> str:
    """回傳目前 active session: 'asia' / 'us' / 'gap' (空窗期, 只剩加密貨幣)."""
    if any(_is_market_in_session(c) for c in ["TW", "JP", "KR"]):
        return "asia"
    if _is_market_in_session("US"):
        return "us"
    return "gap"


CRYPTO_CONFIG = {
    "BTC-USD": {"name": "BTC",  "threshold_pct": 2.5},
    "ETH-USD": {"name": "ETH",  "threshold_pct": 2.5},
}


# ---------------------------------------------------------------------------
# 盤中反轉警報配置 (從高點回吐 / 從低點反彈)
# ---------------------------------------------------------------------------
# 為什麼: 原本 check_index_alerts 用 ATR 動態門檻 + bucket 邏輯, 高波動日門檻反而拉高,
#         「漲幅減少→反轉」過程的中段沒推. 這個新邏輯獨立追蹤 intraday_high / low,
#         抓出「從高點掉了 X%」「從低點彈了 X%」, 跟 bucket 邏輯並行不取代.
# 監控標的: 跟 INDEX_CONFIG 一樣 (^TWII / ^N225 / ^KS11 / ^SOX / ^IXIC)
# Threshold: 從高/低點累計 1.0% 觸發, 之後每多 0.5pp 才再觸發 (ratchet)
# Throttle:  cooldown 60 min/sym, max 3 alerts/day/sym
REVERSAL_THRESHOLD_PCT = 1.0       # 初始觸發門檻 (累計回吐/反彈 ≥ 1.0%)
REVERSAL_INCREMENT_PCT = 0.5       # 後續每增加 0.5pp 才再觸發
REVERSAL_COOLDOWN_MIN = 60         # 同 symbol 兩警報間最少間隔 (分)
REVERSAL_MAX_PER_DAY = 3           # 每 symbol 每日最多警報數
# Bug #R1 fix: rebound 必須有「真實的下探」才算反彈, 不然連續鎖漲日的 today_low 就是 open,
# 會把正常漲勢誤判為反彈. 要求 today_low 至少比 today_open 低 0.5% 才考慮 rebound.
REVERSAL_MIN_DIP_FOR_REBOUND_PCT = 0.5


def _fetch_intraday_anchor_data(symbol: str) -> Optional[Dict]:
    """抓盤中反轉判斷需要的數據: today_open / current / 今日 high / 今日 low.

    跟 _fetch_index_today_open_and_current 類似, 但額外回 high/low.
    Bug #R3 fix: NaN / Inf 偵測 → 回 None, 避免污染 state.
    """
    import math
    import pandas as pd

    df = ds.fetch_yf_history(symbol, period="2d", interval="5m")
    if df is None or df.empty:
        return None
    try:
        df = df.copy()
        date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
        # M2 fix: 統一 utc=True 避免 tz-aware/naive 混用 → 後面 datetime 減法不會 raise
        df["_dt"] = pd.to_datetime(df[date_col], utc=True)
        df["_d"] = df["_dt"].dt.date
        today = df["_d"].max()
        sys_today = dt.date.today()
        # stale check: yfinance 最新日期距 server today >= 1 天 → 跳
        if (sys_today - today).days >= 1:
            return None
        today_bars = df[df["_d"] == today].sort_values("_dt")
        if today_bars.empty:
            return None
        bar_count = len(today_bars)  # H1: 給 weak_open 判斷「資料是否已分化」
        today_open = float(today_bars.iloc[0]["Open"])
        current = float(today_bars.iloc[-1]["Close"])
        # 用全部今日 bar 算 high/low (而非只用 state 累積 — yfinance 已給完整 5m bars)
        today_high = float(today_bars["High"].astype(float).max())
        today_low = float(today_bars["Low"].astype(float).min())

        # M2: 抓 today_high / today_low 發生的時間 (用於急殺 vs 緩跌判斷)
        # 取「最後一次」出現 (高低點重覆時, 近期的更貼近現實)
        # M2 fix: _dt 已統一 utc=True, 減法不再 raise; 若仍失敗印 log 而非靜默
        mins_since_high = None
        mins_since_low = None
        try:
            tb = today_bars.copy()
            tb["_high_f"] = tb["High"].astype(float)
            tb["_low_f"] = tb["Low"].astype(float)
            high_rows = tb[tb["_high_f"] >= today_high - 1e-9]
            low_rows = tb[tb["_low_f"] <= today_low + 1e-9]
            last_dt = pd.to_datetime(tb["_dt"].iloc[-1], utc=True)
            if not high_rows.empty:
                high_dt = pd.to_datetime(high_rows["_dt"].iloc[-1], utc=True)
                mins_since_high = max(0.0, (last_dt - high_dt).total_seconds() / 60.0)
            if not low_rows.empty:
                low_dt = pd.to_datetime(low_rows["_dt"].iloc[-1], utc=True)
                mins_since_low = max(0.0, (last_dt - low_dt).total_seconds() / 60.0)
        except Exception as _e:
            print(f"[reversal] {symbol} mins_since calc failed: {_e}", flush=True)

        # 距開盤分鐘數 (L1: 給 vol_ratio 防呆用)
        mins_since_open = None
        try:
            open_dt = pd.to_datetime(today_bars["_dt"].iloc[0], utc=True)
            last_dt2 = pd.to_datetime(today_bars["_dt"].iloc[-1], utc=True)
            mins_since_open = max(0.0, (last_dt2 - open_dt).total_seconds() / 60.0)
        except Exception:
            pass

        # M2: 量比 (今日累計 / 過去 5 日同期累計平均) — 判斷量增殺出 vs 量縮回吐
        # L1 fix: 開盤頭 30min 分母 (過去同期累計) 太小, vol_ratio 易爆極端值 → 留 None
        vol_ratio = None
        try:
            if mins_since_open is None or mins_since_open >= 30:
                today_vol_cum = float(today_bars["Volume"].astype(float).sum())
                prev = df[df["_d"] < today].copy()
                if not prev.empty:
                    last_hm = pd.to_datetime(
                        today_bars["_dt"].iloc[-1], utc=True
                    ).strftime("%H:%M")
                    prev["_hm"] = prev["_dt"].dt.strftime("%H:%M")
                    cum = prev[prev["_hm"] <= last_hm].groupby("_d")["Volume"].sum().tail(5)
                    avg_prev_cum = float(cum.mean()) if not cum.empty else 0.0
                    if avg_prev_cum > 0:
                        vol_ratio = today_vol_cum / avg_prev_cum
        except Exception:
            pass

        # Bug #R3 fix: NaN/Inf 偵測 — yfinance 偶爾回 NaN, json.dumps(nan) 會 raise,
        # state 寫入失敗 → ratchet/cooldown 完全失效. 直接 return None 跳掉這次.
        for name, v in [("today_open", today_open), ("current", current),
                        ("today_high", today_high), ("today_low", today_low)]:
            if math.isnan(v) or math.isinf(v) or v <= 0:
                print(
                    f"[reversal] {symbol} got invalid {name}={v}, skip this tick",
                    flush=True,
                )
                return None

        # 新增 prior_close — 給 alert 顯示「vs 昨收」用
        # 從 df 拿昨日最後一根 5m bar 的 Close. 若昨日無資料 (週一 / 假日後) 取最近一個 trading day.
        prior_close = None
        try:
            prev_df = df[df["_d"] < today].sort_values("_dt")
            if not prev_df.empty:
                prior_close = float(prev_df.iloc[-1]["Close"])
        except Exception:
            pass

        return {
            "today_open": today_open,
            "current": current,
            "today_high": today_high,
            "today_low": today_low,
            "prior_close": prior_close,
            "mins_since_high": mins_since_high,
            "mins_since_low": mins_since_low,
            "mins_since_open": mins_since_open,
            "bar_count": bar_count,
            "vol_ratio": vol_ratio,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Enrichment helpers (M2: severity / market_state / speed)
# ---------------------------------------------------------------------------
def _classify_severity(abs_pct: float) -> str:
    """abs_pct = 回吐/反彈絕對幅度. 回傳 mild / medium / severe."""
    a = abs(abs_pct)
    if a >= 2.5:
        return "severe"
    if a >= 1.5:
        return "medium"
    return "mild"


def _classify_market_state_drawdown(pct_vs_open: float) -> str:
    """drawdown 時的盤勢狀態 (用 pct_vs_open 判斷):
       still_green: 仍在紅 K (高開回吐)
       turned_black: 已翻黑 (溫和壓回平盤下)
       accelerating_down: 加速殺低 (vs 開盤 ≤ -1%)
    """
    if pct_vs_open > 0:
        return "still_green"
    if pct_vs_open > -1.0:
        return "turned_black"
    return "accelerating_down"


def _classify_market_state_rebound(pct_vs_open: float) -> str:
    """rebound 時的盤勢狀態:
       recovered: 已收復 (vs 開盤 ≥ 0)
       near_recover: 接近收復 (-1% < vs 開盤 < 0)
       still_red: 仍在黑 K (vs 開盤 ≤ -1%)
    """
    if pct_vs_open >= 0:
        return "recovered"
    if pct_vs_open > -1.0:
        return "near_recover"
    return "still_red"


def _classify_speed(abs_pct: float, mins_since_extreme: Optional[float]) -> str:
    """急殺 vs 緩跌. 用「pct / 分鐘」速率判斷.
       fast: 30 分內幅度 ≥ 1.5%, 或 60 分內 ≥ 2.0%
       slow: 其他
       unknown: 沒時間資料
    """
    if mins_since_extreme is None or mins_since_extreme < 1:
        return "unknown"
    a = abs(abs_pct)
    if mins_since_extreme <= 30 and a >= 1.5:
        return "fast"
    if mins_since_extreme <= 60 and a >= 2.0:
        return "fast"
    return "slow"


def _classify_volume(vol_ratio: Optional[float]) -> str:
    """量能狀態. vol_ratio = 今日累計 / 過去 5 日同期累計平均.
       heavy: ≥ 1.3 (量增放出)
       normal: 0.8 ~ 1.3
       light: < 0.8 (量縮觀望)
       unknown: 無資料
    """
    if vol_ratio is None:
        return "unknown"
    if vol_ratio >= 1.3:
        return "heavy"
    if vol_ratio < 0.8:
        return "light"
    return "normal"


# ---------------------------------------------------------------------------
# 同向轉弱個股掃描 (TWII drawdown 觸發時用)
# ---------------------------------------------------------------------------
def _single_stock_weak_metrics(stock_id: str) -> Optional[Dict]:
    """單檔: 今日%, 距高點%, 量比.
    用 5m intraday + 過去 5 日 daily 累計做.
    """
    try:
        import pandas as pd
        df = None
        for suffix in [".TW", ".TWO"]:
            tmp = ds.fetch_yf_history(f"{stock_id}{suffix}", period="5d", interval="5m")
            if tmp is not None and not tmp.empty and len(tmp) >= 5:
                df = tmp
                break
        if df is None:
            return None
        date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
        df = df.copy()
        df["_dt"] = pd.to_datetime(df[date_col])
        df["_d"] = df["_dt"].dt.date
        today = df["_d"].max()
        today_bars = df[df["_d"] == today].sort_values("_dt")
        prev_bars = df[df["_d"] < today]
        if today_bars.empty:
            return None
        today_open = float(today_bars["Open"].iloc[0])
        current = float(today_bars["Close"].iloc[-1])
        today_high = float(today_bars["High"].astype(float).max())
        prev_close = float(prev_bars["Close"].iloc[-1]) if not prev_bars.empty else today_open
        if prev_close <= 0 or today_open <= 0 or today_high <= 0:
            return None
        today_pct = (current / prev_close - 1) * 100
        dd_from_high = (current / today_high - 1) * 100
        # 量比 (cumulative today vs 過去 5 日當日總量平均)
        today_vol = float(today_bars["Volume"].astype(float).sum())
        if not prev_bars.empty:
            prev_daily = prev_bars.groupby("_d")["Volume"].sum()
            avg_dv = float(prev_daily.tail(5).mean()) if len(prev_daily) >= 1 else 0
        else:
            avg_dv = 0
        vol_ratio = today_vol / avg_dv if avg_dv > 0 else 0
        return {
            "stock_id": stock_id,
            "current": round(current, 2),
            "today_pct": round(today_pct, 2),
            "drawdown_from_high_pct": round(dd_from_high, 2),
            "vol_ratio": round(vol_ratio, 2),
        }
    except Exception:
        return None


def _scan_companion_weak_stocks_tw(top_n: int = 5, max_workers: int = 8) -> List[Dict]:
    """指數反轉時, 掃台股權值 universe 找同向轉弱的個股.

    篩選條件:
      - 今日跌幅 ≤ -1.0% (或距高點回吐 ≥ 1.5%)
      - 排除「漲反而很多」的 (today_pct > 0)
    取跌幅最深的前 top_n 檔. 用 ThreadPoolExecutor 並行.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # 跟 strong_stock_alert 同 universe (大型權值 + AI/半導體 + 重電 + 航運 等)
    universe = [
        "2330", "2317", "2454", "2412", "2308", "2382", "2891", "2882", "2881",
        "3231", "2376", "6669", "3017", "3661", "2379", "3711", "8046",
        "1513", "1519", "1503", "1504", "1514",
        "2603", "2609", "2618", "2207", "1536",
        "3491", "6285", "3178", "4585",
        "3037",
        "8069", "6531", "1216", "2912",
    ]
    # 加 user watchlist
    try:
        wl = watchlist_store.load_watchlist() or []
        universe = list(dict.fromkeys(universe + wl))
    except Exception:
        pass

    results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_single_stock_weak_metrics, sid): sid for sid in universe}
        for fut in as_completed(futures):
            m = fut.result()
            if m is None:
                continue
            tp = m.get("today_pct", 0)
            dd = m.get("drawdown_from_high_pct", 0)
            # 同向轉弱條件: 今日已跌 ≥ 1% 或 從高點回吐 ≥ 1.5%
            if tp <= -1.0 or dd <= -1.5:
                # 弱勢分數 (越負越弱): today_pct + 0.5 × drawdown_from_high_pct
                # 補強訊號: 量大殺出加重 (vol_ratio 越高越像真出貨)
                vr = m.get("vol_ratio", 0)
                vol_factor = 1.0 + max(0.0, vr - 1.0) * 0.3
                weakness = (tp + 0.5 * dd) * vol_factor
                m["weakness"] = round(weakness, 2)
                results.append(m)

    # weakness 越負越弱 → 升冪排序取前 top_n
    results.sort(key=lambda x: x.get("weakness", 0))
    # 補上股名
    try:
        info = ds.get_taiwan_stock_info()
        if info is not None and not info.empty:
            name_map = info.set_index("stock_id")["stock_name"].to_dict()
            for r in results:
                r["name"] = name_map.get(str(r.get("stock_id", "")), "")
    except Exception:
        pass
    top_results = results[:top_n]
    # 給 top results 加 entry_label (推播裡顯示「哪檔該減碼/出場」)
    try:
        import entry_label_helper as _el
        pairs = [(str(r.get("stock_id", "")), "TW") for r in top_results
                 if r.get("stock_id")]
        if pairs:
            eval_map = _el.batch_evaluate(pairs, max_workers=8)
            for r in top_results:
                sid = str(r.get("stock_id", ""))
                ev = eval_map.get(sid) or {}
                r["entry_label"] = ev.get("entry_label", "—")
                r["entry_emoji"] = ev.get("entry_emoji", "")
                r["entry_score"] = ev.get("entry_score")
    except Exception as _e:
        print(f"[companion] entry_label 失敗 (non-fatal): {_e}", flush=True)
    return top_results


# M3: companion scan 結果 cache (避免同 tick / 短時間多次 drawdown 重複掃 40+ 檔)
COMPANION_CACHE_TTL_SEC = 600  # 10 分鐘


def _scan_companion_weak_stocks_tw_cached(top_n: int = 5, state: Optional[Dict] = None) -> List[Dict]:
    """帶 monitor_state cache 的 companion 掃描.
    10 分鐘內重複呼叫直接回快取, 省掉 40+ 檔 yfinance round-trip (~10-15s).

    HIGH fix: 若 caller 傳入 state (e.g. check_intraday_reversal 已 load 的 state),
    就把 cache 寫進「同一個」state dict, 由 caller 結尾的 save 持久化.
    否則 (standalone) 自己 load/save. 這樣避免 caller 結尾 save 把 companion
    cache 覆蓋掉 (caller load 在前, companion 寫在後, caller save 又用舊 snapshot).
    """
    now = dt.datetime.now(dt.timezone.utc)
    own_state = state is None
    try:
        if state is None:
            state = watchlist_store.load_monitor_state()
        cache = state.get("_companion_cache") or {}
        ts_str = cache.get("ts")
        if ts_str:
            ts = dt.datetime.fromisoformat(ts_str)
            if (now - ts).total_seconds() < COMPANION_CACHE_TTL_SEC:
                cached = cache.get("data") or []
                print(f"[companion] cache hit ({len(cached)} 檔)", flush=True)
                return cached[:top_n]
    except Exception:
        if state is None:
            state = {}

    # cache miss → 重新掃
    result = _scan_companion_weak_stocks_tw(top_n=top_n)
    try:
        state["_companion_cache"] = {"ts": now.isoformat(), "data": result}
        # 只有 standalone (沒外層 state) 才自己 save;
        # 若是 caller 傳入的 state, 由 caller 結尾的 save 一起持久化, 避免被覆蓋.
        if own_state:
            watchlist_store.save_monitor_state(state)
    except Exception:
        pass
    return result



# ---------------------------------------------------------------------------
# 同步轉強權值股 (鏡像 weak 版) — recover/rebound 時用
# ---------------------------------------------------------------------------
def _scan_companion_strong_stocks_tw(top_n: int = 5, max_workers: int = 8) -> List[Dict]:
    """指數反彈時, 掃台股權值找同向轉強的個股."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    universe = [
        "2330", "2317", "2454", "2412", "2308", "2382", "2891", "2882", "2881",
        "3231", "2376", "6669", "3017", "3661", "2379", "3711", "8046",
        "1513", "1519", "1503", "1504", "1514",
        "2603", "2609", "2618", "2207", "1536",
        "3491", "6285", "3178", "4585",
        "3037",
        "8069", "6531", "1216", "2912",
    ]
    try:
        wl = watchlist_store.load_watchlist() or []
        universe = list(dict.fromkeys(universe + wl))
    except Exception:
        pass

    results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_single_stock_weak_metrics, sid): sid for sid in universe}
        for fut in as_completed(futures):
            m = fut.result()
            if m is None:
                continue
            tp = m.get("today_pct", 0)
            dd = m.get("drawdown_from_high_pct", 0)
            # 強勢條件: 今日漲 ≥ +1% (反向)
            if tp >= 1.0:
                vr = m.get("vol_ratio", 0) or 0
                vol_factor = 1.0 + max(0.0, vr - 1.0) * 0.3
                strength = tp * vol_factor
                m["strength"] = round(strength, 2)
                results.append(m)

    results.sort(key=lambda x: x.get("strength", 0), reverse=True)
    try:
        info = ds.get_taiwan_stock_info()
        if info is not None and not info.empty:
            name_map = info.set_index("stock_id")["stock_name"].to_dict()
            for r in results:
                r["name"] = name_map.get(str(r.get("stock_id", "")), "")
    except Exception:
        pass
    top_results = results[:top_n]
    # entry_label
    try:
        import entry_label_helper as _el
        pairs = [(str(r.get("stock_id", "")), "TW") for r in top_results
                 if r.get("stock_id")]
        if pairs:
            eval_map = _el.batch_evaluate(pairs, max_workers=8)
            for r in top_results:
                sid = str(r.get("stock_id", ""))
                ev = eval_map.get(sid) or {}
                r["entry_label"] = ev.get("entry_label", "—")
                r["entry_emoji"] = ev.get("entry_emoji", "")
                r["entry_score"] = ev.get("entry_score")
    except Exception:
        pass
    return top_results


def _scan_companion_strong_stocks_tw_cached(top_n: int = 5, state: Optional[Dict] = None) -> List[Dict]:
    """cache 版 (與 weak 版同 TTL, 不同 key)."""
    now = dt.datetime.now(dt.timezone.utc)
    own_state = state is None
    try:
        if state is None:
            state = watchlist_store.load_monitor_state()
        cache = state.get("_companion_strong_cache") or {}
        ts_str = cache.get("ts")
        if ts_str:
            ts = dt.datetime.fromisoformat(ts_str)
            if (now - ts).total_seconds() < COMPANION_CACHE_TTL_SEC:
                cached = cache.get("data") or []
                print(f"[companion-strong] cache hit ({len(cached)} 檔)", flush=True)
                return cached[:top_n]
    except Exception:
        if state is None:
            state = {}
    result = _scan_companion_strong_stocks_tw(top_n=top_n)
    try:
        state["_companion_strong_cache"] = {"ts": now.isoformat(), "data": result}
        if own_state:
            watchlist_store.save_monitor_state(state)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# 強勢族群 top N (用 sector_pulse.compute_strong_sectors)
# ---------------------------------------------------------------------------
_SECTOR_LEADERS_CACHE_TTL_SEC = 600  # 10 min


def _get_top_strong_sectors_tw(top_n: int = 3, state: Optional[Dict] = None) -> List[Dict]:
    """回今日台股強勢族群 top N. 失敗 graceful 回 [].

    HIGH fix: compute_strong_sectors 跑 60+ 檔 FinMind + Gemini 可能 15-30s.
    在 monitor cron 內 inline 跑會 block 其他 alert. 加 monitor_state cache (10min TTL)
    + universe 縮到 80 檔 (從 200) 降 latency.
    """
    own_state = state is None
    try:
        if state is None:
            state = watchlist_store.load_monitor_state()
        cache = state.get("_sector_leaders_cache") or {}
        ts_str = cache.get("ts")
        if ts_str:
            ts = dt.datetime.fromisoformat(ts_str)
            if (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() < _SECTOR_LEADERS_CACHE_TTL_SEC:
                cached = cache.get("data") or []
                return cached[:top_n]
    except Exception:
        if state is None:
            state = {}

    try:
        import sector_pulse as sp
        data = sp.compute_strong_sectors(top_n=80)  # HIGH fix: 200 → 80
        sectors_df = data.get("sectors")
        if sectors_df is None or sectors_df.empty:
            return []
        rows = sectors_df.head(top_n).to_dict("records")
        ind_col = "industry_category" if "industry_category" in sectors_df.columns else None
        out = []
        for r in rows:
            out.append({
                "sector": r.get(ind_col, "—") if ind_col else "—",
                "avg_change": round(float(r.get("avg_change", 0) or 0), 2),
                "up_ratio": round(float(r.get("up_ratio", 0) or 0) * 100, 0),
                "n": int(r.get("n", 0) or 0),
            })
    except Exception as e:
        print(f"[sector-leaders] failed: {e}", flush=True)
        return []

    # cache 寫回 (跟 caller state 共用避免覆蓋)
    try:
        state["_sector_leaders_cache"] = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "data": out,
        }
        if own_state:
            watchlist_store.save_monitor_state(state)
    except Exception:
        pass
    return out



def check_intraday_reversal() -> List[Dict]:
    """檢測「從高點回吐」「從低點反彈」並回傳 alert list.

    Logic:
      drawdown_pct = (current / today_high - 1) * 100        ≤ 0
      rebound_pct  = (current / today_low  - 1) * 100        ≥ 0

      觸發:
        drawdown: drawdown_pct ≤ -1.0% AND (第一次推 OR 比上次推差 ≥ 0.5pp)
        rebound:  rebound_pct  ≥  1.0% AND (第一次推 OR 比上次推好 ≥ 0.5pp)
      Cooldown: 同 symbol 同方向 60 分鐘
      Daily cap: 每 sym 每天最多 3 則

    State: monitor_state["intraday_reversal"][sym] = {
       "date": "YYYY-MM-DD",
       "last_drawdown_pct": float | None,     # 上次推 drawdown 時的 pct (負值)
       "last_drawdown_at": iso | None,
       "drawdown_alerts_today": int,
       "last_rebound_pct": float | None,
       "last_rebound_at": iso | None,
       "rebound_alerts_today": int,
    }
    """
    state = watchlist_store.load_monitor_state()
    rev_state = state.setdefault("intraday_reversal", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    now_utc = dt.datetime.now(dt.timezone.utc)

    # 假日檢查
    closed_markets: set = set()
    try:
        import holiday_check
        for mk in ["TW", "JP", "KR", "US"]:
            if holiday_check.is_market_closed_today(mk):
                closed_markets.add(mk)
    except Exception:
        pass

    # M1 fix: 之前 I2 dedup (crash 已推則 reversal 跳) 是在 alerts 各自獨立推 TG 時設計的.
    # 現在 crash + reversal 已合併到同一封 TG (fmt_combined_intraday_alerts), reversal 段
    # 是 crash 段的 *互補資訊* (告訴用戶從高點掉多少 vs crash 只告訴大跌總幅度).
    # 拿掉 dedup 後, 合併訊息會同時顯示兩段, 資訊更完整.
    # 註: reversal 自身的 ratchet / cooldown / daily-cap 仍保留 (避免單一 reversal 連推).

    alerts: List[Dict] = []

    # M2: 預抓所有指數 snapshot, 建立 cross_market 對照表
    # 同時跑也讓「同向轉弱個股」掃描有依據 (TWII 觸發 drawdown 才掃)
    all_snaps: Dict[str, Dict] = {}
    for sym, cfg in INDEX_CONFIG.items():
        country = cfg.get("country")
        if country in closed_markets:
            continue
        if not _is_market_in_session(country):
            continue
        snap = _fetch_intraday_anchor_data(sym)
        if not snap:
            continue
        try:
            pct_vs_open_ = (snap["current"] / snap["today_open"] - 1) * 100 \
                           if snap["today_open"] > 0 else 0.0
        except Exception:
            pct_vs_open_ = 0.0
        snap["_pct_vs_open"] = pct_vs_open_
        snap["_country"] = country
        snap["_name"] = cfg.get("name", sym)
        all_snaps[sym] = snap

    for sym, cfg in INDEX_CONFIG.items():
        if sym not in all_snaps:
            continue
        country = cfg.get("country")
        snap = all_snaps[sym]
        today_open = snap["today_open"]
        current = snap["current"]
        today_high = snap["today_high"]
        today_low = snap["today_low"]
        mins_since_high = snap.get("mins_since_high")
        mins_since_low = snap.get("mins_since_low")
        vol_ratio = snap.get("vol_ratio")
        pct_vs_open = snap["_pct_vs_open"]

        # 算 drawdown / rebound (相對 intraday high/low)
        try:
            drawdown_pct = (current / today_high - 1) * 100 if today_high > 0 else 0.0
        except Exception:
            drawdown_pct = 0.0
        try:
            rebound_pct = (current / today_low - 1) * 100 if today_low > 0 else 0.0
        except Exception:
            rebound_pct = 0.0

        # 跨市場對照 (排除自己) — 給訊息用判斷「系統性 vs 獨自走弱」
        cross_market = []
        for other_sym, other_snap in all_snaps.items():
            if other_sym == sym:
                continue
            cross_market.append({
                "symbol": other_sym,
                "name": other_snap["_name"],
                "country": other_snap["_country"],
                "pct_vs_open": round(other_snap["_pct_vs_open"], 2),
            })

        # 跨日 reset
        sym_state = rev_state.setdefault(sym, {})
        if sym_state.get("date") != today_str:
            sym_state.clear()
            sym_state.update({
                "date": today_str,
                "last_drawdown_pct": None,
                "last_drawdown_at": None,
                "drawdown_alerts_today": 0,
                "last_rebound_pct": None,
                "last_rebound_at": None,
                "rebound_alerts_today": 0,
                "max_pct_vs_open": None,   # 今日盤中最高漲幅 (相對開盤)
                "min_pct_vs_open": None,   # 今日盤中最低跌幅 (相對開盤)
                "shrink_alerted_today": False,
                "recover_alerted_today": False,
            })

        # 更新 max/min pct_vs_open
        max_pct_vs_open = sym_state.get("max_pct_vs_open")
        if max_pct_vs_open is None or pct_vs_open > max_pct_vs_open:
            sym_state["max_pct_vs_open"] = pct_vs_open
            max_pct_vs_open = pct_vs_open
        min_pct_vs_open = sym_state.get("min_pct_vs_open")
        if min_pct_vs_open is None or pct_vs_open < min_pct_vs_open:
            sym_state["min_pct_vs_open"] = pct_vs_open
            min_pct_vs_open = pct_vs_open

        # C: 漲幅萎縮 (跌轉漲都用 0.5pp 動作)
        MIN_PEAK_PCT = 0.5
        sym_shrink_threshold = float(cfg.get("shrink_pct",
                                              cfg.get("reversal_pct", REVERSAL_THRESHOLD_PCT)))
        # P1 砍噪音: shrink 跟 drawdown 數學接近 → 禁用 emit (False 不會 alert)
        # 仍記錄 state, 後續可用於 confluence 判斷
        ENABLE_SHRINK_EMIT = False
        if (ENABLE_SHRINK_EMIT
                and max_pct_vs_open >= MIN_PEAK_PCT
                and (max_pct_vs_open - pct_vs_open) >= sym_shrink_threshold
                and not sym_state.get("shrink_alerted_today")):
            companion_stocks_s: List[Dict] = []
            if sym == "^TWII":
                try:
                    companion_stocks_s = _scan_companion_weak_stocks_tw_cached(
                        top_n=5, state=state,
                    )
                except Exception as e:
                    print(f"[reversal-shrink] companion scan failed: {e}", flush=True)
            shrink_pp = max_pct_vs_open - pct_vs_open
            alerts.append({
                "symbol": sym,
                "name": cfg["name"],
                "country": country,
                "type": "shrink",  # 新類別
                "current": round(current, 2),
                "today_open": round(today_open, 2),
                "today_high": round(today_high, 2),
                "today_low": round(today_low, 2),
                "max_pct_vs_open": round(max_pct_vs_open, 2),
                "pct_vs_open": round(pct_vs_open, 2),
                "shrink_pp": round(shrink_pp, 2),
                "severity": _classify_severity(-shrink_pp),  # 用負值 borrow severity logic
                "market_state": "high_to_low",
                "cross_market": cross_market,
                "companion_stocks": companion_stocks_s,
            })
            sym_state["shrink_alerted_today"] = True

        # #1 recover trigger: 跌過 -MIN_PEAK_PCT 後反彈 ≥ recovery_pp (鏡像 shrink)
        # P1 砍噪音: recover 跟 rebound 重複, 禁用 emit
        ENABLE_RECOVER_EMIT = False
        if (ENABLE_RECOVER_EMIT
                and min_pct_vs_open <= -MIN_PEAK_PCT
                and (pct_vs_open - min_pct_vs_open) >= sym_shrink_threshold
                and not sym_state.get("recover_alerted_today")):
            companion_stocks_r: List[Dict] = []
            sector_leaders_r: List[Dict] = []
            if sym == "^TWII":
                try:
                    companion_stocks_r = _scan_companion_strong_stocks_tw_cached(
                        top_n=5, state=state,
                    )
                except Exception as e:
                    print(f"[reversal-recover] companion-up scan failed: {e}", flush=True)
                try:
                    sector_leaders_r = _get_top_strong_sectors_tw(top_n=3, state=state)
                except Exception as e:
                    print(f"[reversal-recover] sector leaders failed: {e}", flush=True)
            recovery_pp = pct_vs_open - min_pct_vs_open
            alerts.append({
                "symbol": sym,
                "name": cfg["name"],
                "country": country,
                "type": "recover",
                "current": round(current, 2),
                "today_open": round(today_open, 2),
                "today_high": round(today_high, 2),
                "today_low": round(today_low, 2),
                "min_pct_vs_open": round(min_pct_vs_open, 2),
                "pct_vs_open": round(pct_vs_open, 2),
                "recovery_pp": round(recovery_pp, 2),
                "severity": _classify_severity(recovery_pp),
                "market_state": "low_to_high",
                "cross_market": cross_market,
                "companion_stocks_up": companion_stocks_r,
                "sector_leaders": sector_leaders_r,
            })
            sym_state["recover_alerted_today"] = True
            # B4 fix: recover 觸發後 mute 同 tick 的 rebound (避免重複訊息)
            # 設個 in-memory 旗標, 下面 rebound check 看這個
            sym_state["_skip_rebound_this_tick"] = True

        # per-symbol reversal threshold (TWII 用 0.5%, 其他用 default 1.0%)
        sym_reversal_threshold = float(cfg.get("reversal_pct", REVERSAL_THRESHOLD_PCT))

        # === Drawdown 判斷 ===
        if drawdown_pct <= -sym_reversal_threshold:
            should_fire = False
            last_pct = sym_state.get("last_drawdown_pct")
            last_at = sym_state.get("last_drawdown_at")
            alerts_today = sym_state.get("drawdown_alerts_today", 0)

            if alerts_today >= REVERSAL_MAX_PER_DAY:
                pass  # 已達每日上限
            elif last_pct is None:
                should_fire = True  # 第一次
            else:
                # 比上次推差 ≥ 增量門檻
                if drawdown_pct <= last_pct - REVERSAL_INCREMENT_PCT:
                    # 還要過 cooldown
                    if last_at:
                        try:
                            ts = dt.datetime.fromisoformat(last_at)
                            if (now_utc - ts).total_seconds() >= REVERSAL_COOLDOWN_MIN * 60:
                                should_fire = True
                        except Exception:
                            should_fire = True
                    else:
                        should_fire = True

            if should_fire:
                # M2: 同向轉弱個股 (只在 TWII drawdown 觸發, US 暫不掃)
                companion_stocks: List[Dict] = []
                if sym == "^TWII":
                    try:
                        # 傳入同一個 state, cache 由本函式結尾的 save 持久化
                        companion_stocks = _scan_companion_weak_stocks_tw_cached(
                            top_n=5, state=state,
                        )
                    except Exception as e:
                        print(f"[reversal] companion scan failed: {e}", flush=True)
                # 新增: vs 昨收 — 讓用戶看到「今日總漲跌」而非只是「from 高點回吐」
                _prior = snap.get("prior_close")
                _pct_vs_prior = None
                if _prior and _prior > 0:
                    _pct_vs_prior = round((current / _prior - 1) * 100, 2)
                alerts.append({
                    "symbol": sym,
                    "name": cfg["name"],
                    "country": country,
                    "type": "drawdown",
                    "current": round(current, 2),
                    "today_open": round(today_open, 2),
                    "today_high": round(today_high, 2),
                    "today_low": round(today_low, 2),
                    "prior_close": round(_prior, 2) if _prior else None,
                    "pct_vs_prior": _pct_vs_prior,   # ← 新增
                    "drawdown_pct": round(drawdown_pct, 2),
                    "rebound_pct": round(rebound_pct, 2),
                    "pct_vs_open": round(pct_vs_open, 2),
                    "alerts_today": alerts_today + 1,
                    # M2 新增豐富欄位
                    "severity": _classify_severity(drawdown_pct),
                    "market_state": _classify_market_state_drawdown(pct_vs_open),
                    "speed": _classify_speed(drawdown_pct, mins_since_high),
                    "mins_since_extreme": round(mins_since_high, 1) if mins_since_high else None,
                    "volume_state": _classify_volume(vol_ratio),
                    "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
                    "cross_market": cross_market,
                    "companion_stocks": companion_stocks,
                })
                # Bug #R2 fix: state 存全精度, 顯示才 round, 避免 ratchet 浮點邊界 bug
                sym_state["last_drawdown_pct"] = drawdown_pct
                sym_state["last_drawdown_at"] = now_utc.isoformat()
                sym_state["drawdown_alerts_today"] = alerts_today + 1

        # === Rebound 判斷 ===
        # Bug #R1 fix: 必須有真實的「下探」才算反彈, 否則連續鎖漲日的 today_low == today_open
        # 會把正常漲勢誤判為反彈. 要求 today_low 至少比 today_open 低 X%.
        dip_pct_from_open = (today_low / today_open - 1) * 100 if today_open > 0 else 0.0
        has_real_dip = dip_pct_from_open <= -REVERSAL_MIN_DIP_FOR_REBOUND_PCT
        # rebound 也用 per-symbol threshold (跟 drawdown 對稱)
        # B4 fix: recover 觸發後 mute 同 tick 的 rebound
        if sym_state.pop("_skip_rebound_this_tick", False):
            pass  # 跳過 rebound, 已被 recover 覆蓋
        elif rebound_pct >= sym_reversal_threshold and has_real_dip:
            should_fire = False
            last_pct = sym_state.get("last_rebound_pct")
            last_at = sym_state.get("last_rebound_at")
            alerts_today = sym_state.get("rebound_alerts_today", 0)

            if alerts_today >= REVERSAL_MAX_PER_DAY:
                pass
            elif last_pct is None:
                should_fire = True
            else:
                if rebound_pct >= last_pct + REVERSAL_INCREMENT_PCT:
                    if last_at:
                        try:
                            ts = dt.datetime.fromisoformat(last_at)
                            if (now_utc - ts).total_seconds() >= REVERSAL_COOLDOWN_MIN * 60:
                                should_fire = True
                        except Exception:
                            should_fire = True
                    else:
                        should_fire = True

            if should_fire:
                alerts.append({
                    "symbol": sym,
                    "name": cfg["name"],
                    "country": country,
                    "type": "rebound",
                    "current": round(current, 2),
                    "today_open": round(today_open, 2),
                    "today_high": round(today_high, 2),
                    "today_low": round(today_low, 2),
                    "drawdown_pct": round(drawdown_pct, 2),
                    "rebound_pct": round(rebound_pct, 2),
                    "pct_vs_open": round(pct_vs_open, 2),
                    "alerts_today": alerts_today + 1,
                    # M2 新增豐富欄位
                    "severity": _classify_severity(rebound_pct),
                    "market_state": _classify_market_state_rebound(pct_vs_open),
                    "speed": _classify_speed(rebound_pct, mins_since_low),
                    "mins_since_extreme": round(mins_since_low, 1) if mins_since_low else None,
                    "volume_state": _classify_volume(vol_ratio),
                    "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
                    "cross_market": cross_market,
                    "companion_stocks": [],  # rebound 暫不掃 (未來可補同向轉強股)
                })
                # Bug #R2 fix: state 存全精度
                sym_state["last_rebound_pct"] = rebound_pct
                sym_state["last_rebound_at"] = now_utc.isoformat()
                sym_state["rebound_alerts_today"] = alerts_today + 1

    state["intraday_reversal"] = rev_state
    watchlist_store.save_monitor_state(state)
    return alerts


# ---------------------------------------------------------------------------
# 開盤即弱 / 開盤即強警報 (M3)
# ---------------------------------------------------------------------------
# 為什麼: reversal 邏輯是「從高/低點回吐」, 對「開盤就跌, 一路弱」這種沒給過機會的
#        場景沒辦法觸發 (today_high == today_open, drawdown_pct ≈ 0).
#        新警報專抓「開盤直接跌/直接漲」, 對 SOX/IXIC (台股 leading indicator) 特別重要.
# 觸發條件 (任一指數開盤後判斷):
#   弱開: pct_vs_open ≤ -WEAK_OPEN_PCT(sym) AND today_high 距 today_open < 0.3% (沒反彈過)
#   強開: pct_vs_open ≥ +WEAK_OPEN_PCT(sym) AND today_low  距 today_open > -0.3% (沒回測過)
# Throttle: 每 sym 每方向每日最多 1 則 (型態警報, 不需 ratchet)
# State: monitor_state["weak_open"][sym] = {date, weak_alerted, strong_alerted}
WEAK_OPEN_DEFAULT_PCT = 1.0   # 預設閾值
# 放寬: 0.3 → 0.5 (台股開盤後常震盪 -0.3%, 0.3 太嚴會把真正強/弱開擋掉)
WEAK_OPEN_NO_REBOUND_PCT = 0.5  # 高/低點離開盤 < 0.5% = 沒回頭機會
# Per-symbol 客製 (波動較大的指數用較高 threshold, 避免雜訊)
# 弱開門檻 (跌)
WEAK_OPEN_PCT_OVERRIDE = {
    "^SOX": 1.5,    # 中等: 1.0→1.5
    "^IXIC": 1.2,   # 中等: 0.8→1.2
    "^N225": 1.0,   # 中等: 0.8→1.0
    "^KS11": 1.0,   # 中等: 0.7→1.0
    "^TWII": 1.0,   # 中等: 0.7→1.0
}
# 強開門檻 (漲) — 略低於弱開 (用戶想抓開盤大漲)
STRONG_OPEN_PCT_OVERRIDE = {
    "^SOX": 1.2,    # 中等: 0.8→1.2
    "^IXIC": 1.0,   # 中等: 0.7→1.0
    "^N225": 0.8,   # 中等: 0.6→0.8
    "^KS11": 0.8,   # 中等: 0.6→0.8
    "^TWII": 0.8,   # 中等: 0.5→0.8 (TWII)
}


def check_weak_open_alerts() -> List[Dict]:
    """偵測「開盤即弱」/「開盤即強」場景.

    每 sym 每方向每日最多推 1 則 (型態確認, 不會反覆推).
    回傳的 alert dict 結構:
      symbol, name, country, type ("weak_open"/"strong_open"),
      current, today_open, today_high, today_low,
      pct_vs_open, high_from_open_pct, low_from_open_pct,
      severity, vol_ratio, cross_market.
    """
    state = watchlist_store.load_monitor_state()
    wo_state = state.setdefault("weak_open", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")

    closed_markets: set = set()
    try:
        import holiday_check
        for mk in ["TW", "JP", "KR", "US"]:
            if holiday_check.is_market_closed_today(mk):
                closed_markets.add(mk)
    except Exception:
        pass

    # 預抓所有 snap (給 cross_market 用)
    all_snaps: Dict[str, Dict] = {}
    for sym, cfg in INDEX_CONFIG.items():
        country = cfg.get("country")
        if country in closed_markets:
            continue
        if not _is_market_in_session(country):
            continue
        snap = _fetch_intraday_anchor_data(sym)
        if not snap:
            continue
        try:
            pct_vs_open_ = (snap["current"] / snap["today_open"] - 1) * 100 \
                           if snap["today_open"] > 0 else 0.0
        except Exception:
            pct_vs_open_ = 0.0
        snap["_pct_vs_open"] = pct_vs_open_
        snap["_country"] = country
        snap["_name"] = cfg.get("name", sym)
        all_snaps[sym] = snap

    alerts: List[Dict] = []

    for sym, cfg in INDEX_CONFIG.items():
        if sym not in all_snaps:
            continue
        country = cfg.get("country")
        snap = all_snaps[sym]
        today_open = snap["today_open"]
        today_high = snap["today_high"]
        today_low = snap["today_low"]
        current = snap["current"]
        pct_vs_open = snap["_pct_vs_open"]
        vol_ratio = snap.get("vol_ratio")
        bar_count = snap.get("bar_count", 0)
        mins_since_open = snap.get("mins_since_open")

        if today_open <= 0:
            continue

        # H1/H3 fix: 開盤頭 15min 資料還沒分化 (today_high ≈ today_open 是正常未分化結果,
        #            而非「沒給過反彈機會」). 要求至少 4 根 5m bar (≥20min) 或開盤 ≥20min
        #            才允許 weak/strong_open 觸發, 避免開盤即弱誤判.
        # 從 4 → 3 (≥15min), 配合 09:15 monitor cron 可抓到開盤+15min
        enough_data = bar_count >= 3 or (mins_since_open is not None and mins_since_open >= 15)
        if not enough_data:
            continue

        # 高/低點距開盤的幅度 (high_from_open 一定 ≥ 0, low_from_open 一定 ≤ 0)
        high_from_open = (today_high / today_open - 1) * 100
        low_from_open = (today_low / today_open - 1) * 100

        # 跨日 reset
        sym_state = wo_state.setdefault(sym, {})
        if sym_state.get("date") != today_str:
            sym_state.clear()
            sym_state.update({
                "date": today_str,
                "weak_alerted": False,
                "strong_alerted": False,
            })

        # 跨市場對照 snapshot (排除自己)
        cross_market = []
        for other_sym, other_snap in all_snaps.items():
            if other_sym == sym:
                continue
            cross_market.append({
                "symbol": other_sym,
                "name": other_snap["_name"],
                "country": other_snap["_country"],
                "pct_vs_open": round(other_snap["_pct_vs_open"], 2),
            })

        sym_threshold_weak = WEAK_OPEN_PCT_OVERRIDE.get(sym, WEAK_OPEN_DEFAULT_PCT)
        sym_threshold_strong = STRONG_OPEN_PCT_OVERRIDE.get(sym, WEAK_OPEN_DEFAULT_PCT)
        # alias 給弱開原本 reference
        sym_threshold = sym_threshold_weak

        # === 弱開判斷 ===
        # pct_vs_open ≤ -threshold AND 高點離開盤很近 (沒反彈過)
        if pct_vs_open <= -sym_threshold and high_from_open < WEAK_OPEN_NO_REBOUND_PCT:
            if not sym_state.get("weak_alerted"):
                alerts.append({
                    "symbol": sym,
                    "name": cfg["name"],
                    "country": country,
                    "type": "weak_open",
                    "current": round(current, 2),
                    "today_open": round(today_open, 2),
                    "today_high": round(today_high, 2),
                    "today_low": round(today_low, 2),
                    "pct_vs_open": round(pct_vs_open, 2),
                    "high_from_open_pct": round(high_from_open, 2),
                    "low_from_open_pct": round(low_from_open, 2),
                    "severity": _classify_severity(pct_vs_open),
                    "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
                    "volume_state": _classify_volume(vol_ratio),
                    "cross_market": cross_market,
                })
                sym_state["weak_alerted"] = True

        # === 強開判斷 ===
        # pct_vs_open ≥ +strong_threshold AND 低點離開盤很近 (沒回測過)
        if pct_vs_open >= sym_threshold_strong and low_from_open > -WEAK_OPEN_NO_REBOUND_PCT:
            if not sym_state.get("strong_alerted"):
                alerts.append({
                    "symbol": sym,
                    "name": cfg["name"],
                    "country": country,
                    "type": "strong_open",
                    "current": round(current, 2),
                    "today_open": round(today_open, 2),
                    "today_high": round(today_high, 2),
                    "today_low": round(today_low, 2),
                    "pct_vs_open": round(pct_vs_open, 2),
                    "high_from_open_pct": round(high_from_open, 2),
                    "low_from_open_pct": round(low_from_open, 2),
                    "severity": _classify_severity(pct_vs_open),
                    "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
                    "volume_state": _classify_volume(vol_ratio),
                    "cross_market": cross_market,
                })
                sym_state["strong_alerted"] = True

    state["weak_open"] = wo_state
    watchlist_store.save_monitor_state(state)
    return alerts


# ---------------------------------------------------------------------------
# 系統性大跌警報配置 (繞過 ATR / cooldown / daily-cap throttle)
# ---------------------------------------------------------------------------
# 觸發條件 (任一):
#   A) 盤中跌幅 vs 今日開盤 <= INTRADAY_PCT  (預設 -3%)
#   B) 連續 2 日累計跌幅 (今日 close vs 前日 close 再串接) <= TWO_DAY_CUM_PCT (預設 -4%)
# 監控標的: 台股加權, 費城半導體 (台股先行指標), S&P500, 台積電 ADR
#
# Throttle (避免一天狂推):
#   - 同 symbol 兩次警報至少間隔 SYSTEMIC_COOLDOWN_MIN 分鐘 (預設 60)
#   - 全 symbol 一天最多 SYSTEMIC_MAX_PER_DAY 則 (預設 2)
#
# State: monitor_state["systemic_crash_alerts"][symbol] = {
#   "date": "YYYY-MM-DD", "last_alert_at": iso, "alerts_today": int
# }
SYSTEMIC_CRASH_CONFIG = {
    # 為什麼 SOX 用 -2.0%: 費半波動較大 + 對台股傳導性強, 使用者要求 30-60 min 內 -2% 就推
    "^TWII": {"name": "台灣加權", "country": "TW", "intraday_threshold": -3.0},
    "^SOX":  {"name": "費城半導體", "country": "US", "intraday_threshold": -2.0},
    "^IXIC": {"name": "那斯達克", "country": "US", "intraday_threshold": -3.0},
    "TSM":   {"name": "台積電 ADR", "country": "US", "intraday_threshold": -3.0},
}
SYSTEMIC_INTRADAY_PCT = -3.0      # 預設觸發門檻 (若 config 沒設 intraday_threshold)
SYSTEMIC_TWO_DAY_CUM_PCT = -4.0   # 觸發門檻: 連續 2 日累計 -4%
SYSTEMIC_COOLDOWN_MIN = 60        # 同 symbol 兩警報間最少間隔 (分)
SYSTEMIC_MAX_PER_DAY = 2          # 全 symbol 每日最多警報數
# 是否把「vs 昨收」也納入 intraday 跌幅判斷.
# True (預設): 取 min(pct_vs_open, pct_vs_prior) — 能抓 gap down 場景, 但對 user 字面意思
#              「盤中 -3%」較寬鬆
# False: 只看 pct_vs_open — 嚴格符合「盤中跌幅」字面
SYSTEMIC_INCLUDE_VS_PRIOR = True


def _fetch_systemic_snapshot(symbol: str) -> Optional[Dict]:
    """抓系統性大跌判斷需要的數據:
       今日開盤 / 即時價 / 前日收盤 / 前前日收盤.

    優先用 5m intraday 抓 today_open + current, 然後用 daily 抓 prior closes.
    任一失敗 → 回 None.

    NOTE [Bug #6 已知限制 — 時區邊界]:
    本函式用 `dt.date.today()` (= server 本地日期, 在 GH Actions = UTC) 跟 yfinance
    回傳的 latest_d 比對「是否為今天」. 對 TW (UTC+8) 跟 US (UTC-4/-5) 而言, UTC 跨日
    的 1-2 小時內可能誤判. 實務上 monitor cron `*/15` 一天 96 次, 即使誤判跨日邊界
    幾個 tick, 也會在 session 真正開始後正確判斷, 不會造成 silent 失敗.
    """
    import pandas as pd

    # 1) Daily 抓 prior close 跟前前日 close (近 5 個交易日)
    df_d = ds.fetch_yf_history(symbol, period="10d", interval="1d")
    if df_d is None or df_d.empty or len(df_d) < 2:
        return None
    try:
        df_d = df_d.copy()
        date_col_d = "Date" if "Date" in df_d.columns else df_d.columns[0]
        df_d["_d"] = pd.to_datetime(df_d[date_col_d]).dt.date
        df_d = df_d.sort_values("_d")
        latest_d = df_d["_d"].iloc[-1]
        sys_today = dt.date.today()
        # daily 已過期 → 跳 (盤前 / 假日 / yfinance 延遲)
        if (sys_today - latest_d).days >= 2:
            return None
        # 視 latest 是否為「今天」
        latest_is_today = (latest_d == sys_today)
        closes = df_d["Close"].astype(float).tolist()
    except Exception:
        return None

    # 2) 5m intraday 抓 today_open + current (若 latest_is_today 為 False, 表示盤前/休市)
    today_open = None
    current = None
    df_i = ds.fetch_yf_history(symbol, period="2d", interval="5m")
    if df_i is not None and not df_i.empty:
        try:
            df_i = df_i.copy()
            date_col_i = "Datetime" if "Datetime" in df_i.columns else df_i.columns[0]
            df_i["_dt"] = pd.to_datetime(df_i[date_col_i])
            df_i["_d"] = df_i["_dt"].dt.date
            today_in_intraday = df_i["_d"].max()
            sys_today = dt.date.today()
            if (sys_today - today_in_intraday).days < 1:
                today_bars = df_i[df_i["_d"] == today_in_intraday].sort_values("_dt")
                if not today_bars.empty:
                    today_open = float(today_bars.iloc[0]["Open"])
                    current = float(today_bars.iloc[-1]["Close"])
        except Exception:
            pass

    # 3) 若 intraday 拿不到 → fallback 用 daily (latest)
    #    但若 daily 的最新 row 不是「今天」(latest_is_today=False),
    #    用 daily latest 當 current 會跟 prior_close 落在同一天 → 無意義.
    #    這種情況直接 return None (代表 yfinance 還沒拿到今天的 data).
    if today_open is None or current is None:
        if not latest_is_today:
            return None  # 沒有今日真實數據, 不冒風險用昨日 daily 當「今日」
        try:
            today_open = float(df_d["Open"].iloc[-1])
            current = float(df_d["Close"].iloc[-1])
        except Exception:
            return None

    # 4) prior close: 若 latest_is_today, 用倒數第 2;否則用倒數第 1
    try:
        if latest_is_today and len(closes) >= 2:
            prior_close = float(closes[-2])
        else:
            prior_close = float(closes[-1])
    except Exception:
        return None

    # 5) 前前日 close (給「連續 2 日累計」用)
    try:
        if latest_is_today and len(closes) >= 3:
            prior_prior_close = float(closes[-3])
        elif (not latest_is_today) and len(closes) >= 2:
            prior_prior_close = float(closes[-2])
        else:
            prior_prior_close = None
    except Exception:
        prior_prior_close = None

    return {
        "today_open": today_open,
        "current": current,
        "prior_close": prior_close,
        "prior_prior_close": prior_prior_close,
    }


def check_systemic_crash() -> Optional[Dict]:
    """檢測系統性大跌. 任一監控標的觸發即回傳整體 context.

    回傳 None (沒觸發) 或 dict:
    {
      "triggers": [{symbol, name, country, trigger_type, value, ...}, ...],
      "context": {
        "vix": float | None,
        "all_snapshots": [...全部標的的當前狀態給 Gemini 看全局],
        "macro_news": str | None,  # 國際新聞背景
        "checked_at": ISO,
      },
      "alert_index": int  # 今天第 N 次 (1 開始, 用於文案)
    }
    """
    # 載 state — 共用 monitor_state.json
    state = watchlist_store.load_monitor_state()
    crash_state = state.setdefault("systemic_crash_alerts", {})
    today_str = dt.date.today().strftime("%Y-%m-%d")
    now_utc = dt.datetime.now(dt.timezone.utc)

    # 跨日重置: 全部 symbol 都把舊日的 state 清掉
    for sym in list(crash_state.keys()):
        if crash_state[sym].get("date") != today_str:
            crash_state[sym] = {"date": today_str, "last_alert_at": None, "alerts_today": 0}

    # 今日已發過的總數 (跨 symbol)
    total_today = sum(s.get("alerts_today", 0) for s in crash_state.values())
    if total_today >= SYSTEMIC_MAX_PER_DAY:
        return None

    triggers: List[Dict] = []
    all_snapshots: List[Dict] = []

    for sym, cfg in SYSTEMIC_CRASH_CONFIG.items():
        # === Fix #1: 同一 tick 內也要尊重全局每日上限 ===
        if (total_today + len(triggers)) >= SYSTEMIC_MAX_PER_DAY:
            break

        country = cfg.get("country", "")
        # 該市場必須在 session 內 (避免抓 stale close 觸發假警報)
        if not _is_market_in_session(country):
            continue
        snap = _fetch_systemic_snapshot(sym)
        if not snap:
            continue
        today_open = snap["today_open"]
        current = snap["current"]
        prior_close = snap["prior_close"]
        prior_prior = snap.get("prior_prior_close")

        # 算盤中跌幅 vs 開盤 / vs 昨收
        try:
            pct_vs_open = (current / today_open - 1) * 100 if today_open else 0.0
        except Exception:
            pct_vs_open = 0.0
        try:
            pct_vs_prior = (current / prior_close - 1) * 100 if prior_close else 0.0
        except Exception:
            pct_vs_prior = 0.0
        # Fix #3: vs prior_close 包含 gap down. 由 SYSTEMIC_INCLUDE_VS_PRIOR 開關控制.
        if SYSTEMIC_INCLUDE_VS_PRIOR:
            intraday_pct = min(pct_vs_open, pct_vs_prior)
        else:
            intraday_pct = pct_vs_open

        # 算 2 日累計 (前日 close → 今日 close, 並把昨日 close vs 前前日 close 加上)
        two_day_pct = None
        try:
            if prior_prior and prior_close and prior_prior > 0:
                # 兩日複合報酬: (1+d1)*(1+d2)-1
                d1 = prior_close / prior_prior - 1
                d2 = current / prior_close - 1 if prior_close else 0
                two_day_pct = ((1 + d1) * (1 + d2) - 1) * 100
        except Exception:
            two_day_pct = None

        snap_full = {
            "symbol": sym,
            "name": cfg["name"],
            "country": country,
            "today_open": round(today_open, 4),
            "current": round(current, 4),
            "prior_close": round(prior_close, 4),
            "pct_vs_open": round(pct_vs_open, 2),
            "pct_vs_prior": round(pct_vs_prior, 2),
            "intraday_pct": round(intraday_pct, 2),
            "two_day_pct": round(two_day_pct, 2) if two_day_pct is not None else None,
        }
        all_snapshots.append(snap_full)

        # === 判斷是否觸發 ===
        sym_state = crash_state.setdefault(sym, {"date": today_str, "last_alert_at": None, "alerts_today": 0})

        # Cooldown: 同 symbol 至少 60 分鐘
        last_at = sym_state.get("last_alert_at")
        if last_at:
            try:
                last_dt = dt.datetime.fromisoformat(last_at)
                if (now_utc - last_dt).total_seconds() < SYSTEMIC_COOLDOWN_MIN * 60:
                    continue
            except Exception:
                pass

        # SOX fix: per-symbol intraday threshold (default -3%, SOX 用 -2%)
        sym_intraday_threshold = cfg.get("intraday_threshold", SYSTEMIC_INTRADAY_PCT)

        trigger_type = None
        trigger_value = None
        if intraday_pct <= sym_intraday_threshold:
            trigger_type = "intraday"
            trigger_value = intraday_pct
        elif two_day_pct is not None and two_day_pct <= SYSTEMIC_TWO_DAY_CUM_PCT:
            trigger_type = "two_day"
            trigger_value = two_day_pct

        if trigger_type:
            triggers.append({
                **snap_full,
                "trigger_type": trigger_type,
                "trigger_value": round(trigger_value, 2),
                "threshold_used": sym_intraday_threshold,
            })
            sym_state["last_alert_at"] = now_utc.isoformat()
            sym_state["alerts_today"] = sym_state.get("alerts_today", 0) + 1

    if not triggers:
        state["systemic_crash_alerts"] = crash_state
        watchlist_store.save_monitor_state(state)
        return None

    # === 蒐集上下文 (VIX + 國際新聞) 給 Gemini 用 ===
    vix_now = None
    try:
        vix_df = ds.fetch_yf_history("^VIX", period="5d", interval="1d")
        if vix_df is not None and not vix_df.empty:
            vix_now = float(vix_df["Close"].astype(float).iloc[-1])
    except Exception:
        pass

    macro_news = None
    try:
        import news_sources
        macro_news = news_sources.build_news_context(include_trump=True)
    except Exception:
        macro_news = None

    new_total = sum(s.get("alerts_today", 0) for s in crash_state.values())

    state["systemic_crash_alerts"] = crash_state
    watchlist_store.save_monitor_state(state)

    return {
        "triggers": triggers,
        "context": {
            "vix": round(vix_now, 2) if vix_now is not None else None,
            "all_snapshots": all_snapshots,
            "macro_news": macro_news,
            "checked_at": now_utc.isoformat(),
        },
        "alert_index": new_total,
        "max_per_day": SYSTEMIC_MAX_PER_DAY,
    }


# ---------------------------------------------------------------------------
# ATR helper (給動態門檻用)
# ---------------------------------------------------------------------------
def _compute_atr(symbol: str, period_days: str = "2mo", n: int = 14) -> Optional[float]:
    """抓 daily K 線算 ATR(14). 失敗回 None."""
    try:
        import pandas as pd
        df = ds.fetch_yf_history(symbol, period=period_days, interval="1d")
        if df is None or df.empty or len(df) < n + 1:
            return None
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        return float(tr.tail(n).mean())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 大盤點數警報
# ---------------------------------------------------------------------------
def _fetch_index_today_open_and_current(symbol: str) -> Optional[Dict]:
    """抓今天開盤價跟即時價 (用 5m 線推今日開盤)."""
    df = ds.fetch_yf_history(symbol, period="2d", interval="5m")
    if df.empty:
        df_d = ds.fetch_yf_history(symbol, period="2d", interval="1d")
        if df_d.empty or len(df_d) < 1:
            return None
        try:
            import pandas as pd
            df_d = df_d.copy()
            date_col_d = "Date" if "Date" in df_d.columns else df_d.columns[0]
            latest_d = pd.to_datetime(df_d[date_col_d].iloc[-1]).date()
            sys_today = dt.date.today()
            if (sys_today - latest_d).days >= 1:
                return None
            today_open = float(df_d["Open"].iloc[-1])
            today_close = float(df_d["Close"].iloc[-1])
            return {"open": today_open, "current": today_close}
        except Exception:
            return None

    import pandas as pd
    df = df.copy()
    date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
    df["_dt"] = pd.to_datetime(df[date_col])
    df["_d"] = df["_dt"].dt.date
    today = df["_d"].max()

    sys_today = dt.date.today()
    if (sys_today - today).days >= 1:
        return None

    today_bars = df[df["_d"] == today].sort_values("_dt")
    if today_bars.empty:
        return None
    try:
        today_open = float(today_bars.iloc[0]["Open"])
        current = float(today_bars.iloc[-1]["Close"])
        return {"open": today_open, "current": current}
    except Exception:
        return None


def check_index_alerts() -> List[Dict]:
    """檢查大盤點數是否觸發新門檻. 該市場休市則跳過."""
    state = watchlist_store.load_monitor_state()
    idx_state = state.setdefault("index_alerts", {})

    closed_markets: set = set()
    try:
        import holiday_check
        for mk in ["TW", "JP", "KR", "US"]:
            if holiday_check.is_market_closed_today(mk):
                closed_markets.add(mk)
    except Exception:
        pass

    today_str = dt.date.today().strftime("%Y-%m-%d")
    alerts: List[Dict] = []

    for sym, cfg in INDEX_CONFIG.items():
        country = cfg.get("country")
        if country in closed_markets:
            continue
        if not _is_market_in_session(country):
            continue
        info = _fetch_index_today_open_and_current(sym)
        if not info:
            continue
        today_open = info["open"]
        current = info["current"]
        diff = current - today_open

        threshold = cfg["threshold"]
        if not cfg.get("disable_atr_boost", False):
            try:
                atr14 = _compute_atr(sym, period_days="2mo", n=14)
                if atr14 is not None:
                    dyn = atr14 * 0.8
                    upper_cap = threshold * 3
                    if dyn > threshold:
                        threshold = round(min(dyn, upper_cap), 1)
            except Exception:
                pass

        if diff >= 0:
            bucket = int(diff // threshold) * threshold
        else:
            bucket = -(int(-diff // threshold) * threshold)

        sym_state = idx_state.setdefault(sym, {})
        if sym_state.get("date") != today_str:
            sym_state.clear()
            sym_state["date"] = today_str
            sym_state["last_bucket"] = 0
            sym_state["last_alert_diff"] = 0.0
            sym_state["last_alert_price"] = round(today_open, 2)
            sym_state["consecutive_count"] = 0
            sym_state["last_direction"] = "none"
            sym_state["last_alert_at"] = None
            sym_state["alerts_today_count"] = 0

        last_bucket = sym_state.get("last_bucket", 0)
        if bucket != last_bucket and abs(bucket) >= threshold:
            direction = "漲" if diff > 0 else "跌"
            last_direction = sym_state.get("last_direction", "none")
            if direction != last_direction:
                sym_state["consecutive_count"] = 0
                sym_state["last_direction"] = direction

            last_at = sym_state.get("last_alert_at")
            if last_at:
                try:
                    last_dt = dt.datetime.fromisoformat(last_at)
                    elapsed = (dt.datetime.now(dt.timezone.utc) - last_dt).total_seconds()
                    if elapsed < 30 * 60:
                        continue
                except Exception:
                    pass

            alerts_count = sym_state.get("alerts_today_count", 0)
            MAX_ALERTS_PER_DAY = 4
            if alerts_count >= MAX_ALERTS_PER_DAY:
                continue

            sym_state["consecutive_count"] = sym_state.get("consecutive_count", 0) + 1
            consecutive = sym_state["consecutive_count"]

            last_alert_diff = float(sym_state.get("last_alert_diff", 0))
            last_alert_price = float(sym_state.get("last_alert_price", today_open))
            leg_pts = round(diff - last_alert_diff, 2)

            alerts.append({
                "symbol": sym,
                "name": cfg["name"],
                "country": cfg["country"],
                "today_open": round(today_open, 2),
                "current": round(current, 2),
                "diff": round(diff, 2),
                "last_alert_price": round(last_alert_price, 2),
                "last_alert_diff": round(last_alert_diff, 2),
                "leg_pts": leg_pts,
                "direction": direction,
                "threshold_bucket": bucket,
                "threshold_used": threshold,
                "consecutive": consecutive,
                "warning": consecutive >= 2,
                "alerts_today": alerts_count + 1,
            })
            sym_state["last_bucket"] = bucket
            sym_state["last_alert_diff"] = round(diff, 2)
            sym_state["last_alert_price"] = round(current, 2)
            sym_state["last_alert_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            sym_state["alerts_today_count"] = alerts_count + 1

    state["index_alerts"] = idx_state
    watchlist_store.save_monitor_state(state)
    return alerts


# ---------------------------------------------------------------------------
# 加密貨幣警報 (排程制)
# ---------------------------------------------------------------------------
CRYPTO_SCHEDULE_UTC_HOURS = {
    15: "night",  # 台北 23:00
}


def check_crypto_alerts() -> List[Dict]:
    """加密貨幣排程警報. 每 slot 最多 2 次 (first push 固定發, second push 變動 >= 2.5% 才發)."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    cur_hour = now_utc.hour
    cur_minute = now_utc.minute

    if cur_hour not in CRYPTO_SCHEDULE_UTC_HOURS:
        return []

    is_first_tick = cur_minute < 30

    slot_label = CRYPTO_SCHEDULE_UTC_HOURS[cur_hour]
    today_str = now_utc.strftime("%Y-%m-%d")
    slot_key = f"{today_str}_{slot_label}"

    state = watchlist_store.load_monitor_state()
    crypto_state = state.setdefault("crypto_alerts", {})

    print(
        f"[crypto] slot={slot_key} tick={'first' if is_first_tick else 'second'} "
        f"min={cur_minute}, state has {len(crypto_state)} symbols",
        flush=True,
    )

    alerts: List[Dict] = []
    now_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    for sym, cfg in CRYPTO_CONFIG.items():
        df = ds.fetch_yf_history(sym, period="2d", interval="1h")
        if df.empty:
            print(f"[crypto] {sym} no data, skip", flush=True)
            continue
        try:
            current = float(df["Close"].iloc[-1])
        except Exception:
            continue
    state["crypto_alerts"] = ca_state
    watchlist_store.save_monitor_state(state)
    return alerts
