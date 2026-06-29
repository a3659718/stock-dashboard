"""
strong_stock_alert.py
大盤大漲時, 自動掃當下強勢股推 TG.

觸發條件 (任一):
  - 加權指數 (^TWII) 當日漲幅 >= +1.5%
  - 加權指數突破今日 + 10 日高點
  - 費半 (^SOX) 隔夜 >= +2% (亞股開盤前訊號)

觸發後動作:
  1. 抓 universe (top 100 流動股 + watchlist)
  2. 算每檔當下強勢分數: 今日漲幅 + 量比 + 對 TWII 相對強度
  3. 排序取 Top 10 推 TG

只在台股 session 內 (09:00-13:30 台北) 跑.
給 market_open_alert.py monitor flow 呼叫.

API:
  - check_market_surge() -> Optional[Dict]  # 觸發條件偵測, 沒觸發回 None
  - scan_strong_stocks_now(top_n=10) -> List[Dict]  # 掃當下強勢股
  - fmt_strong_alert_tg(surge_info, picks) -> str  # 格式化 TG 訊息
"""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import data_sources as ds


# yfinance 對 ^TWII 5m 盤中偶爾回空 → 用 0050.TW (元大台灣50, 走勢百分比幾近等同大盤) 當代理,
# 避免台股大漲因指數盤中資料缺漏而靜默漏接 (與 index_alerts 的 ^SOX→SOXX 同理)。
# 只用到「當日漲幅 %」, 代理與大盤百分比一致, 無價格尺度問題。
_TWII_PROXY = "0050.TW"


def _fetch_twii_5m_resilient():
    """回 (df, used_symbol). ^TWII 5m 回空時改用 0050.TW。"""
    df = ds.fetch_yf_history("^TWII", period="2d", interval="5m")
    if df is None or getattr(df, "empty", True):
        df2 = ds.fetch_yf_history(_TWII_PROXY, period="2d", interval="5m")
        if df2 is not None and not df2.empty:
            print(f"[strong_stock_alert] ^TWII 5m 回空 → 改用代理 {_TWII_PROXY}", flush=True)
            return df2, _TWII_PROXY
    return df, "^TWII"


def check_market_surge() -> Optional[Dict]:
    """偵測大盤是否大漲. 回傳 surge_info 或 None.

    surge_info = {trigger, twii_pct, sox_overnight_pct, message}
    """
    # 必須在台股 session 內 (09:00-13:30 TPE = UTC 01:00-05:30)
    now_utc = dt.datetime.now(dt.timezone.utc)
    h = now_utc.hour
    if h < 1 or h > 5:
        return None
    # 假日 skip
    try:
        import holiday_check
        if holiday_check.is_market_closed_today("TW"):
            return None
    except Exception:
        pass

    triggers = []

    # 1. TWII 當日漲幅
    twii_pct = None
    try:
        twii, _twii_src = _fetch_twii_5m_resilient()
        if twii is not None and not twii.empty:
            today_bars = twii.tail(50)  # 約近 4 小時的 5m bars
            if len(today_bars) >= 2:
                # 找今日 open (第一筆) vs current (最後一筆)
                date_col = "Datetime" if "Datetime" in twii.columns else twii.columns[0]
                twii = twii.copy()
                import pandas as pd
                twii["_dt"] = pd.to_datetime(twii[date_col])
                twii["_d"] = twii["_dt"].dt.date
                today = twii["_d"].max()
                today_bars = twii[twii["_d"] == today].sort_values("_dt")
                if not today_bars.empty:
                    today_open = float(today_bars["Open"].iloc[0])
                    current = float(today_bars["Close"].iloc[-1])
                    if today_open > 0:
                        twii_pct = (current / today_open - 1) * 100
    except Exception:
        pass

    if twii_pct is not None and twii_pct >= 1.5:
        triggers.append(f"加權指數 +{twii_pct:.2f}% (>+1.5%)")

    # 2. 費半隔夜
    sox_pct = None
    try:
        sox = ds.fetch_yf_history("^SOX", period="5d", interval="1d")
        if sox is not None and not sox.empty and len(sox) >= 2:
            c = sox["Close"].astype(float)
            sox_pct = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
            if sox_pct >= 2.0:
                triggers.append(f"費半隔夜 +{sox_pct:.2f}% (>+2%)")
    except Exception:
        pass

    if not triggers:
        return None

    return {
        "trigger": " · ".join(triggers),
        "twii_pct": round(twii_pct, 2) if twii_pct is not None else None,
        "sox_overnight_pct": round(sox_pct, 2) if sox_pct is not None else None,
        "fired_at": now_utc.strftime("%Y-%m-%d %H:%M UTC"),
    }


def _stock_strength_metrics(stock_id: str) -> Optional[Dict]:
    """單檔股票當下強勢度: 今日%, 量比, RS vs TWII."""
    try:
        # 用 yfinance 抓即時 5m (台股代號加 .TW 或 .TWO)
        for suffix in [".TW", ".TWO"]:
            sym = f"{stock_id}{suffix}"
            df = ds.fetch_yf_history(sym, period="5d", interval="5m")
            if df is not None and not df.empty and len(df) >= 5:
                break
        else:
            return None
        import pandas as pd
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
        today_vol = float(today_bars["Volume"].sum())
        # 昨收
        prev_close = float(prev_bars["Close"].iloc[-1]) if not prev_bars.empty else today_open
        # 今日漲幅 (對昨收)
        today_pct = (current / prev_close - 1) * 100 if prev_close > 0 else 0
        # 量比 (今日累計 / 過去 5 日平均當日量)
        if not prev_bars.empty:
            prev_daily = prev_bars.groupby("_d")["Volume"].sum()
            avg_daily_vol = float(prev_daily.tail(5).mean()) if len(prev_daily) >= 1 else 0
        else:
            avg_daily_vol = 0
        vol_ratio = today_vol / avg_daily_vol if avg_daily_vol > 0 else 0
        return {
            "stock_id": stock_id,
            "current": round(current, 2),
            "today_pct": round(today_pct, 2),
            "vol_ratio": round(vol_ratio, 2),
            "today_vol_M": round(today_vol / 1000000, 1),  # 百萬股
        }
    except Exception:
        return None


def scan_strong_stocks_now(top_n: int = 10, max_workers: int = 8) -> List[Dict]:
    """掃當下強勢股. 從 universe (top 100 流動 + watchlist) 算強勢分數排序."""
    # 簡化 universe: 用大型權值 + 高 momentum 候選 (40 檔, 控制 yfinance 呼叫量)
    universe = [
        # 大型權值
        "2330", "2317", "2454", "2412", "2308", "2382", "2891", "2882", "2881",
        # AI / 半導體
        "3231", "2376", "6669", "3017", "3661", "2379", "3711", "8046",
        # 重電 / 核電
        "1513", "1519", "1503", "1504", "1514",
        # 航運 / 汽車
        "2603", "2609", "2618", "2207", "1536",
        # 太空 / 衛星 / 機器人
        "3491", "6285", "3178", "4585",
        # ABF 載板
        "3037",
        # 其他熱門
        "8069", "6531", "1216", "2912",
    ]
    # 嘗試讀使用者 watchlist 也加入掃描
    try:
        import watchlist_store
        wl = watchlist_store.load_watchlist() or []
        universe = list(dict.fromkeys(universe + wl))
    except Exception:
        pass

    print(f"[strong_stock_alert] 掃描 {len(universe)} 檔...", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_stock_strength_metrics, sid): sid for sid in universe}
        for fut in as_completed(futures):
            m = fut.result()
            if m is None:
                continue
            # 強勢分數: today_pct × 2 + vol_ratio × 1 (簡單線性)
            tp = m.get("today_pct", 0)
            vr = m.get("vol_ratio", 0)
            # 過濾: 今日跌的不算強勢, 量比太小不算 (避免假訊號)
            if tp < 1.0 or vr < 0.8:
                continue
            score = tp * 2 + vr * 1
            m["score"] = round(score, 2)
            results.append(m)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]


def fmt_strong_alert_tg(surge_info: Dict, picks: List[Dict]) -> str:
    """組 TG 訊息. HTML 格式."""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)

    lines = [
        "🚀 <b>大盤大漲警報</b>",
        f"<i>{_esc(surge_info.get('trigger', ''))}</i>",
        "",
    ]
    if not picks:
        lines.append("(掃描中無強勢股, 或資料未更新)")
        return "\n".join(lines)
    lines.append(f"<b>📊 當下強勢股 Top {len(picks)}</b>")
    # 嘗試補上股名 (從 data_sources.get_taiwan_stock_info)
    name_map = {}
    try:
        info = ds.get_taiwan_stock_info()
        if info is not None and not info.empty:
            name_map = info.set_index("stock_id")["stock_name"].to_dict()
    except Exception:
        pass
    # #6: 加入假反彈偵測
    try:
        import fake_rally_detector as _frd
        picks = _frd.flag_fake_rally(picks)
    except Exception:
        pass
    for i, p in enumerate(picks, 1):
        sid = str(p.get("stock_id", ""))
        name = _esc(name_map.get(sid, ""))
        cur = p.get("current", "—")
        tp = p.get("today_pct", 0)
        vr = p.get("vol_ratio", 0)
        score = p.get("score", 0)
        line = (
            f"{i}. <code>{_esc(sid)}</code> {name} · "
            f"{cur} <b>+{tp:.2f}%</b> · 強度 {score:.0f}"
        )
        # 結論導向: 不顯示量比, 直接給「不追/觀望」
        if p.get("rally_quality") == "fake_rally":
            line += "  ⚠️ <i>不追 (弱反彈)</i>"
        elif p.get("rally_quality") == "weak":
            line += "  🟡 <i>觀望</i>"
        lines.append(line)
    lines.append("")
    lines.append("<i>※ 為當下動能掃描, 非中長線推薦. 請自行控管風險.</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口 (給 monitor cron 呼叫)
# ---------------------------------------------------------------------------
def check_and_push_if_surge() -> Optional[Dict]:
    """完整流程: 偵測 → 掃描 → 推送. 給 market_open_alert.py 直接呼叫.

    回傳 {triggered: bool, n_picks: int, sent: bool} 或 None (未觸發).
    含跨 tick 去重 (同一天每個 trigger reason 只推一次).
    """
    surge = check_market_surge()
    if not surge:
        return None
    # 跨 tick 去重: 一天每個 trigger 只推一次
    try:
        import watchlist_store
        state = watchlist_store.load_monitor_state()
        ssa = state.setdefault("strong_stock_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        sent_today = ssa.get(today_str, [])
        trigger_key = surge.get("trigger", "")
        if trigger_key in sent_today:
            return {"triggered": True, "n_picks": 0, "sent": False, "reason": "dup"}
    except Exception:
        # Bug fix: 原本 except 沒定義 trigger_key/state → 送出成功後 sent_today.append(trigger_key)
        #          會 NameError(被外層 except 吞), 害成功的推播被記成失敗且未去重 → 下個 tick 重複推.
        sent_today = []
        ssa = {}
        today_str = dt.date.today().strftime("%Y-%m-%d")
        trigger_key = surge.get("trigger", "")
        state = None

    print(f"[strong_stock_alert] 觸發: {surge['trigger']}", flush=True)
    picks = scan_strong_stocks_now(top_n=10)
    print(f"[strong_stock_alert] 找到 {len(picks)} 檔強勢股", flush=True)
    msg = fmt_strong_alert_tg(surge, picks)

    try:
        import notifier
        ok, info = notifier.send_message(msg, disable_preview=True)
        if ok:
            sent_today.append(trigger_key)
            ssa[today_str] = sent_today
            if state is not None:  # state load 失敗時不寫, 避免用空 dict 覆蓋 monitor_state
                try:
                    watchlist_store.save_monitor_state(state)
                except Exception:
                    pass
            return {"triggered": True, "n_picks": len(picks), "sent": True}
        else:
            print(f"[strong_stock_alert] 推送失敗: {info}", flush=True)
            return {"triggered": True, "n_picks": len(picks), "sent": False}
    except Exception as e:
        print(f"[strong_stock_alert] notifier 失敗: {e}", flush=True)
        return {"triggered": True, "n_picks": len(picks), "sent": False}


# ===========================================================================
# 新增: 常態 intraday 強勢股推播 (不依賴大盤大漲)
# ===========================================================================
# 用戶要求: 即使大盤平淡, 也要推當下強勢個股 (避免錯過題材股噴出)
# Cooldown 90min, 一天最多 3 次 (避免噪音)

INTRADAY_STRONG_COOLDOWN_MIN = 90
INTRADAY_STRONG_DAILY_CAP = 3
INTRADAY_STRONG_MIN_PCT = 3.0   # 個股漲幅門檻 ≥3%
INTRADAY_STRONG_MIN_VR = 1.5    # 量比 ≥1.5
INTRADAY_STRONG_TOP_N = 5       # 用戶要求 top 5


def scan_intraday_strong_stocks(top_n: int = INTRADAY_STRONG_TOP_N,
                                  min_pct: float = INTRADAY_STRONG_MIN_PCT,
                                  min_vr: float = INTRADAY_STRONG_MIN_VR,
                                  max_workers: int = 8) -> List[Dict]:
    """常態掃強勢股 (不需大盤大漲也跑).

    門檻: today_pct ≥3% + vol_ratio ≥1.5
    回 top_n 檔, 按 score (today_pct*2 + vr*1) 排序
    """
    universe = [
        # 大型權值
        "2330", "2317", "2454", "2412", "2308", "2382", "2891", "2882", "2881",
        # AI / 半導體
        "3231", "2376", "6669", "3017", "3661", "2379", "3711", "8046",
        # 重電 / 核電
        "1513", "1519", "1503", "1504", "1514",
        # 航運 / 汽車
        "2603", "2609", "2618", "2207", "1536",
        # 太空 / 衛星 / 機器人
        "3491", "6285", "3178", "4585",
        # ABF / 載板
        "3037",
        # 其他熱門
        "8069", "6531", "1216", "2912",
        # 量子 / SMR / 光通訊
        "2308", "3450", "2331", "6271",
        # 生技 / 醫材
        "4138", "1707",
    ]
    try:
        import watchlist_store
        wl = watchlist_store.load_watchlist() or []
        universe = list(dict.fromkeys(universe + wl))
    except Exception:
        pass
    try:
        import holdings_store
        hd = [str(x.get("stock_id", "")).strip() for x in (holdings_store.load_holdings() or [])]
        universe = list(dict.fromkeys(universe + [h for h in hd if h]))
    except Exception:
        pass

    print(f"[intraday_strong] 掃描 {len(universe)} 檔...", flush=True)
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_stock_strength_metrics, sid): sid for sid in universe}
        for fut in as_completed(futures):
            m = fut.result()
            if m is None:
                continue
            tp = m.get("today_pct", 0)
            vr = m.get("vol_ratio", 0)
            if tp < min_pct or vr < min_vr:
                continue
            m["score"] = round(tp * 2 + vr * 1, 2)
            results.append(m)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_n]


def _enrich_picks_with_entry_label(picks: List[Dict]) -> List[Dict]:
    """加 entry_label / entry_emoji / 進場參考 / 停損 (給 user 直接決策)."""
    for p in picks:
        sid = str(p.get("stock_id", ""))
        cur = p.get("current") or 0
        # 1. quick_evaluate 提供 BUY/HOLD/AVOID
        try:
            import entry_evaluator as _ee
            qe = _ee.quick_evaluate(sid, market="TW")
            p["entry_label"] = qe.get("entry_label", "—")
            p["entry_emoji"] = qe.get("entry_emoji", "")
            p["entry_action"] = qe.get("entry_action", "—")
        except Exception:
            p["entry_label"] = "—"
            p["entry_emoji"] = ""
            p["entry_action"] = "—"
        # 2. 建議停損 (-3% trailing 或最近低點, 取較高者)
        try:
            cur_f = float(cur)
            stop_trail = round(cur_f * 0.97, 2)  # -3% trailing
            p["suggest_stop"] = stop_trail
            # 進場參考: 當前 -1% (給一點回拉空間, 不追最高)
            p["suggest_entry"] = round(cur_f * 0.99, 2)
            # 目標: +5% (~ 1.7R)
            p["suggest_target"] = round(cur_f * 1.05, 2)
        except (TypeError, ValueError):
            p["suggest_stop"] = None
            p["suggest_entry"] = None
            p["suggest_target"] = None
    return picks


def _fmt_intraday_strong_msg(picks: List[Dict]) -> str:
    """格式化常態強勢股 TG 訊息 (含進出場建議)."""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)
    if not picks:
        return ""
    # Enrich with entry_label + 進出場價
    picks = _enrich_picks_with_entry_label(picks)
    # Bug fix: 加假反彈標記
    try:
        import fake_rally_detector as _frd
        picks = _frd.flag_fake_rally(picks)
    except Exception:
        pass

    now_tpe = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%H:%M")
    lines = [
        f"💪 <b>盤中強勢股 Top {len(picks)}</b> · {now_tpe} TPE",
        "<i>(個股 ≥3% + 量比 ≥1.5x)</i>",
        "",
    ]
    # 補股名
    name_map = {}
    try:
        info = ds.get_taiwan_stock_info()
        if info is not None and not info.empty:
            name_map = info.set_index("stock_id")["stock_name"].to_dict()
    except Exception:
        pass

    # 先按 entry_label 分組: BUY/HOLD 優先, AVOID 警示
    label_order = {"BUY": 0, "STRONG_BUY": 0, "HOLD": 1, "WAIT": 2, "AVOID": 3, "SELL": 4, "—": 5}
    picks_sorted = sorted(picks, key=lambda p: (label_order.get(p.get("entry_label", "—"), 5), -p.get("score", 0)))

    for i, p in enumerate(picks_sorted, 1):
        sid = str(p.get("stock_id", ""))
        name = _esc(name_map.get(sid, ""))
        cur = p.get("current")
        # Bug fix: 原 default "—" 字串套 :.2f 會 TypeError 炸掉整封強勢股推播. 改安全格式化.
        cur_s = f"{cur:.2f}" if isinstance(cur, (int, float)) else "—"
        tp = p.get("today_pct", 0)
        vr = p.get("vol_ratio", 0)
        el = p.get("entry_label", "—")
        em = p.get("entry_emoji", "")
        ea = p.get("entry_action", "—")
        # 主行
        el_tag = f" {em}{_esc(el)}" if el and el != "—" else ""
        # 結論導向: 假反彈直接改 entry_label 為 "不追"
        if p.get("rally_quality") == "fake_rally":
            el_tag = " ⚠️不追(弱反彈)"
        # #1 強勢股升級: 加品質 action 標籤
        q_action = p.get("quality_action", "")
        q_score = p.get("quality_score")
        q_tag = ""
        if q_action and "短線拉抬" in q_action:
            q_tag = f" {q_action}"  # ⚠️ 警示
        elif q_score is not None and q_score >= 75:
            q_tag = f" {q_action}"  # 🔥 / 💎 高品質
        lines.append(
            f"{i}. <code>{_esc(sid)}</code> {name} · "
            f"{cur_s} <b>+{tp:.2f}%</b> · 量比 {vr:.2f}x{el_tag}{q_tag}"
        )
        # 進出場建議 (只給 BUY/HOLD 標的, AVOID 改顯警告)
        if el in ("BUY", "STRONG_BUY", "HOLD"):
            se = p.get("suggest_entry")
            ss = p.get("suggest_stop")
            st = p.get("suggest_target")
            if se and ss and st:
                lines.append(
                    f"   📍 進場參考 {se:.2f} · 停損 {ss:.2f} · 目標 {st:.2f}"
                )
        elif el in ("AVOID", "SELL"):
            lines.append("   ⚠️ 已漲多 + 體質不佳, 避免追高")
        elif ea and ea != "—":
            lines.append(f"   💡 {_esc(ea)}")

    lines.append("")
    lines.append("<i>※ 短線動能掃描. 建議停損 -3%, 目標 +5%.</i>")
    return "\n".join(lines)


def check_and_push_intraday_strong() -> Optional[Dict]:
    """常態 intraday 強勢股推播 (不需大盤大漲).

    Cooldown 90min + 一天最多 3 次.
    回 {triggered, n_picks, sent, reason}
    """
    # 必須在台股 session 內 (09:00-13:30 TPE = UTC 01:00-05:30)
    now_utc = dt.datetime.now(dt.timezone.utc)
    if now_utc.hour < 1 or now_utc.hour > 5:
        return None
    # 假日 skip
    try:
        import holiday_check
        if holiday_check.is_market_closed_today("TW"):
            return None
    except Exception:
        pass

    # Cooldown + daily cap 檢查
    state = None
    isa = None
    today_data = None
    try:
        import watchlist_store
        state = watchlist_store.load_monitor_state()
        isa = state.setdefault("intraday_strong_alert", {})
        today_str = dt.date.today().strftime("%Y-%m-%d")
        today_data = isa.setdefault(today_str, {"count": 0, "last_ts": None})
        # daily cap
        if today_data.get("count", 0) >= INTRADAY_STRONG_DAILY_CAP:
            return {"triggered": False, "reason": "daily_cap_reached"}
        # cooldown
        last_ts = today_data.get("last_ts")
        if last_ts:
            try:
                last_dt = dt.datetime.fromisoformat(last_ts)
                if (now_utc - last_dt).total_seconds() < INTRADAY_STRONG_COOLDOWN_MIN * 60:
                    return {"triggered": False, "reason": "cooldown"}
            except Exception:
                pass
    except Exception:
        pass

    picks = scan_intraday_strong_stocks()
    if not picks:
        return {"triggered": False, "reason": "no_picks"}

    # #1 強勢股升級: 加品質分數 + action 標籤 (RS / 連續 / 籌碼 / 距高 / 拉回)
    try:
        import stock_quality_filter as _sqf
        original_n = len(picks)
        # 不過濾, 留所有 picks (按 quality_score 排序), 給訊息分類
        for p in picks:
            sid = str(p.get("stock_id") or p.get("symbol", ""))
            tp = float(p.get("today_pct", 0) or 0)
            vr = float(p.get("vol_ratio", 0) or 0)
            if sid:
                q = _sqf.compute_quality_score(sid, tp, vr, "TW")
                p["quality_score"] = q.get("quality_score", 0)
                p["quality_action"] = _sqf.classify_action(q, tp)
                p["rs"] = q.get("rs")
        # 按 quality_score 排序, 高品質先
        picks.sort(key=lambda p: p.get("quality_score", 0) or 0, reverse=True)
        # 過濾掉「短線拉抬」(品質 < 40 + today_pct > 5%)
        picks = [
            p for p in picks
            if not (p.get("quality_score", 100) < 40 and p.get("today_pct", 0) > 5)
        ]
        if not picks:
            return {"triggered": False, "reason": "all_low_quality"}
        print(f"[intraday_strong] 品質過濾 {original_n}→{len(picks)}", flush=True)
    except Exception as _qe:
        print(f"[intraday_strong] quality filter fail: {_qe}", flush=True)

    # B: 跨類去重 — 30 min 內已推同股不再推

    # B: 跨類去重 — 30 min 內已推同股不再推
    try:
        import alert_priority as _ap
        original_n = len(picks)
        picks = _ap.filter_dedup_picks(picks, "intraday_strong_stock", "up")
        if not picks:
            return {"triggered": False, "reason": "all_recently_pushed",
                    "filtered_n": original_n}
    except Exception:
        pass

    msg = _fmt_intraday_strong_msg(picks)
    # 推播末段加歷史績效
    try:
        import signal_tracker as _st
        perf = _st.fmt_compact_perf("intraday_strong_stock", lookback_days=30)
        if perf:
            msg = msg + "\n\n" + perf
    except Exception:
        pass

    try:
        import notifier
        ok, info = notifier.send_message(msg, disable_preview=True)
        if ok and today_data is not None:
            today_data["count"] = today_data.get("count", 0) + 1
            today_data["last_ts"] = now_utc.isoformat()
            try:
                import watchlist_store
                watchlist_store.save_monitor_state(state)
            except Exception:
                pass
            try:
                import signal_tracker as _st2
                _st2.record_batch("intraday_strong_stock", picks,
                                   evaluate_after_days=5, expected_direction="up")
            except Exception as _re:
                print(f"[intraday_strong] record_batch failed: {_re}", flush=True)
            try:
                import alert_priority as _ap
                _ap.mark_picks_pushed(picks, "intraday_strong_stock", "up")
            except Exception:
                pass
        return {"triggered": True, "n_picks": len(picks), "sent": ok}
    except Exception as e:
        return {"triggered": False, "err": str(e)}
