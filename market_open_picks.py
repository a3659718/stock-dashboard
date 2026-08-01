"""
market_open_picks.py
開盤後 30 分鐘分析：

【台股版】
  1) 計算當下熱門題材 (sector_pulse.compute_hot_themes)
  2) 取「平均%」最高的前 3 個族群（=資金主流）
  3) 每個族群挑 3 檔「動能潛在」的個股 (不一定漲最多)
  4) 動能標準: 站月線 + 量比>1.2 + 5d漲幅<12% + 今日漲幅 0.3~7% (避免追頂)

【美股版】
  1) 板塊 ETF 1d 表現排序，取前 3 板塊
  2) 每個板塊池內挑 3 檔
  3) 額外輸出「成長動能極強 + 近期 IPO」單獨 5 檔
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import asia_markets
import chip_analyzer
import data_sources as ds
import earnings_calendar
import laggard_finder
import market_predictor
import potential_picker
import sector_pulse
import stock_catalyst

# ---------------------------------------------------------------------------
# 台股族群 vs 日韓對應產業 mapping (用於盤後比對)
# ---------------------------------------------------------------------------
TW_TO_ASIA_SECTOR_MAP = {
    "AI 伺服器":  ["JP 半導體", "KR 半導體"],
    "AI 邊緣":    ["JP 半導體", "KR 半導體"],
    "AI PC":      ["JP 半導體"],
    "ABF 載板":   ["JP 半導體", "KR 半導體"],
    "高頻高速":   ["JP 半導體"],
    "矽光子":     ["JP 半導體"],
    "PCB":        ["JP 半導體", "KR 半導體"],
    "被動元件":   ["JP 電子零組件"],
    "面板":       ["JP 電子零組件", "KR 半導體"],
    "汽車零件":   ["JP 汽車", "KR 汽車"],
    "電動車":     ["JP 汽車", "KR 汽車"],
    "金融":       ["JP 金融", "KR 金融"],
    "航運":       ["JP 海運"],
    "重電族群":   ["JP 工業"],
    "儲能":       ["JP 工業"],
    "散熱":       ["JP 電子零組件"],
}


# 日韓代表股 (用於計算對應產業的當日漲幅)
# 美股板塊 → 台股對應題材 (用於美股盤後推薦受惠台股)
US_SECTOR_TO_TW_THEMES = {
    "XLK":  ["AI 伺服器", "AI 邊緣", "AI PC", "ABF 載板", "PCB", "高頻高速", "矽光子"],
    "XLC":  ["AI 伺服器", "低軌衛星"],
    "XLY":  ["航運", "汽車零件", "電動車"],
    "XLI":  ["重電族群", "汽車零件", "機器人"],
    "XLF":  ["金融"],
    "XLV":  ["生技"],
    "XLB":  [],
    "XLE":  [],
    "XLU":  ["重電族群", "儲能"],
    "XLRE": [],
    "XLP":  [],
}


JP_KR_PROXIES = {
    "JP 半導體":     ["8035.T", "6857.T", "6920.T"],   # Tokyo Electron, Advantest, Lasertec
    "JP 電子零組件": ["6981.T", "6594.T"],              # Murata, Nidec
    "JP 汽車":       ["7203.T", "7267.T"],              # Toyota, Honda
    "JP 海運":       ["9101.T", "9104.T"],              # NYK, MOL
    "JP 工業":       ["6502.T", "6501.T"],              # Toshiba, Hitachi
    "JP 金融":       ["8306.T", "8316.T"],              # MUFG, SMFG
    "KR 半導體":     ["005930.KS", "000660.KS"],        # Samsung Elec, SK Hynix
    "KR 汽車":       ["005380.KS"],                     # Hyundai Motor
    "KR 金融":       ["105560.KS", "055550.KS"],        # KB Financial, Shinhan
}


# ---------------------------------------------------------------------------
# 美股板塊 → 代表性個股池
# ---------------------------------------------------------------------------
US_SECTOR_STOCKS: Dict[str, List[str]] = {
    "XLK": ["NVDA", "MSFT", "AAPL", "AVGO", "AMD", "ADBE", "CRM", "ORCL", "PANW", "CRWD", "PLTR", "SNOW", "MDB"],
    "XLE": ["XOM", "CVX", "COP", "EOG", "OXY", "PSX", "MPC", "SLB"],
    "XLF": ["JPM", "BAC", "WFC", "GS", "MS", "C", "BLK", "AXP", "V", "MA"],
    "XLV": ["UNH", "LLY", "JNJ", "MRK", "ABBV", "TMO", "PFE", "ABT"],
    "XLY": ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG", "ABNB"],
    "XLP": ["WMT", "PG", "COST", "KO", "PEP", "PM", "MO"],
    "XLI": ["GE", "BA", "CAT", "DE", "UNP", "RTX", "HON"],
    "XLB": ["LIN", "FCX", "APD", "SHW", "ECL"],
    "XLU": ["NEE", "SO", "DUK", "VST", "CEG"],
    "XLRE": ["PLD", "EQIX", "AMT", "CCI"],
    "XLC": ["META", "GOOGL", "NFLX", "DIS", "TMUS", "CMCSA"],
}


# 「成長動能 / 近期 IPO」池：高成長+高熱度名單 (2023-2025 IPO 或 高 RS)
US_GROWTH_IPO_POOL = [
    "RDDT", "CRWV", "ARM", "ASTS", "RBLX", "CART", "KLC",
    "IONQ", "RGTI", "QBTS", "SOUN", "BBAI",  # AI/Quantum
    "PLTR", "SMCI",            # high-momentum
    "OKLO", "VST", "CEG",                       # nuclear/power
    "CRWD", "PANW", "DDOG", "MDB",              # cyber/cloud
    "HOOD", "SOFI", "AFRM",                     # fintech
    "ANET", "SMR", "TEM",                        # other growth
]


# ---------------------------------------------------------------------------
# 共用 helpers
# ---------------------------------------------------------------------------
def _score_stock_momentum(metrics: Dict) -> float:
    """0-10 的動能潛力分數，偏好「起漲位 + 量能配合」。"""
    s = 0.0
    today = metrics.get("今日%")
    five = metrics.get("5日%")
    twenty = metrics.get("20日%") if "20日%" in metrics else None
    ratio = metrics.get("量比")

    if today is not None:
        if 0.3 <= today <= 4:
            s += 2.5
        elif 4 < today <= 7:
            s += 1.5
        elif today > 7:
            s += 0.3  # 太強了反而扣分
        elif today < 0:
            s -= 1
    if ratio is not None:
        if 1.2 <= ratio <= 3:
            s += 2.5
        elif 3 < ratio <= 5:
            s += 1.5
        elif ratio < 0.8:
            s -= 1.0
    if five is not None:
        if 0 <= five <= 8:
            s += 2.0  # 起漲位
        elif 8 < five <= 15:
            s += 1.0
        elif five > 20:
            s -= 1.5
        elif five < -3:
            s -= 0.5
    return round(max(0.0, s), 2)


# ---------------------------------------------------------------------------
# 加權指數即時 (盤中)
# ---------------------------------------------------------------------------
def _fetch_twii_intraday() -> Dict:
    """抓盤中加權指數即時資料.
    回傳: {current, prev_close, today_open, change_pct, change_pts, day_range_pct}
    抓不到時 → 各欄位 None.
    """
    out = {
        "current": None, "prev_close": None, "today_open": None,
        "change_pct": None, "change_pts": None, "day_high": None, "day_low": None,
    }
    # 1) 5m 線 (盤中即時) — 取最後一根當 current, 同日第一根當 today_open
    df = ds.fetch_yf_history("^TWII", period="2d", interval="5m")
    if df is not None and not df.empty:
        try:
            df = df.copy()
            date_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
            df["_dt"] = pd.to_datetime(df[date_col])
            df["_d"] = df["_dt"].dt.date
            today = df["_d"].max()
            today_bars = df[df["_d"] == today].sort_values("_dt")
            if not today_bars.empty:
                out["current"] = float(today_bars["Close"].iloc[-1])
                out["today_open"] = float(today_bars["Open"].iloc[0])
                out["day_high"] = float(today_bars["High"].max())
                out["day_low"] = float(today_bars["Low"].min())
        except Exception:
            pass
    # 2) 抓昨收 (用 daily 線取倒數第二根)
    df_d = ds.fetch_yf_history("^TWII", period="5d", interval="1d")
    if df_d is not None and not df_d.empty and len(df_d) >= 2:
        try:
            close_d = df_d["Close"].astype(float)
            out["prev_close"] = float(close_d.iloc[-2])
            # 如果 5m 抓不到 current, 就用 daily 最後一根當 current
            if out["current"] is None:
                out["current"] = float(close_d.iloc[-1])
                out["today_open"] = float(df_d["Open"].astype(float).iloc[-1])
        except Exception:
            pass
    # 3) 算 change vs 昨收
    if out["current"] and out["prev_close"]:
        out["change_pts"] = round(out["current"] - out["prev_close"], 2)
        out["change_pct"] = round((out["current"] / out["prev_close"] - 1) * 100, 2)
        out["current"] = round(out["current"], 2)
        out["prev_close"] = round(out["prev_close"], 2)
    if out["today_open"]:
        out["today_open"] = round(out["today_open"], 2)
    if out["day_high"]:
        out["day_high"] = round(out["day_high"], 2)
    if out["day_low"]:
        out["day_low"] = round(out["day_low"], 2)
    return out


# ---------------------------------------------------------------------------
# 台股開盤分析
# ---------------------------------------------------------------------------
def get_tw_open_picks(top_themes_n: int = 3, picks_per_theme: int = 3) -> Dict:
    """回傳前 N 族群與每族群挑出的個股。"""
    hot = sector_pulse.compute_hot_themes()
    themes_df = hot.get("themes")
    leaders_map = hot.get("leaders") or {}
    if themes_df is None or themes_df.empty:
        return {"error": "尚未取得題材資料 (盤前/休市?)"}

    top_themes = themes_df.head(top_themes_n)["題材"].tolist()
    picks: List[Dict] = []
    seen_sids: set = set()  # 跨題材去重

    info = ds.get_taiwan_stock_info()
    # 韌性: FinMind token 失效/額度爆 + 本地快取空時, info 會是「無欄位空 df」。
    # 舊寫法直接 info.set_index("stock_id") → KeyError "None of ['stock_id'] are in the columns"
    # → 整個台股盤前推播 fatal exit 1, 一則都不發。改成先驗證欄位存在, 缺就退成空 map,
    # fetch_intraday_metrics 的 market_map 空時會自動 .TW/.TWO 兩種都試 (sector_pulse 已處理)。
    _has_info = info is not None and not info.empty and "stock_id" in info.columns
    name_map = (info.set_index("stock_id")["stock_name"].to_dict()
                if _has_info and "stock_name" in info.columns else {})
    market_map_full = (info.set_index("stock_id")["type"].to_dict()
                       if _has_info and "type" in info.columns else {})

    for theme in top_themes:
        df = leaders_map.get(theme, pd.DataFrame())
        if df is None or df.empty:
            stock_ids = sector_pulse.TW_THEMES.get(theme, [])
            df = sector_pulse.fetch_intraday_metrics(stock_ids, market_map_full)
        if df is None or df.empty:
            picks.append({"theme": theme, "stocks": pd.DataFrame()})
            continue

        df = df.copy()
        # 跨題材去重
        df = df[~df["stock_id"].isin(seen_sids)]
        df["score"] = df.apply(lambda r: _score_stock_momentum(r.to_dict()), axis=1)
        df = df[df["score"] > 0].sort_values("score", ascending=False).head(picks_per_theme)
        if "stock_name" not in df.columns:
            df["stock_name"] = df["stock_id"].map(name_map).fillna("")
        seen_sids.update(df["stock_id"].tolist())
        picks.append({"theme": theme, "stocks": df})

    # 註: 之前有套籌碼過濾 (外資出貨/散戶接刀剔除), 但籌碼資料 T+1 落後,
    # 容易錯過「外資賣完反彈」的動能股 (台玻 1802 案例), 已拿掉自動排除。
    # 如要恢復可呼叫 chip_filter.fetch_messy_map() 標註 (不剔除).

    # 大盤盤型預測 + 評估歷史準確率
    prediction = market_predictor.predict_tw_pattern()
    if not prediction.get("error"):
        market_predictor.save_prediction(prediction)
    market_predictor.evaluate_pending_predictions()
    accuracy = market_predictor.accuracy_stats(market="TW", lookback_days=30)

    # 替每檔個股補上催化劑摘要 (Gemini 一次批次處理)
    # 注意: compute_hot_themes() 內部已對 leaders 跑過一次 annotate_picks_with_catalysts,
    # 那一次的結果保留在 DataFrame 的「催化劑」欄. 這裡先複用該欄, 只對「催化劑為空」的
    # 個股再 call 一次 Gemini, 避免重複燒 quota.
    all_picks_rows = []
    catalysts: Dict[str, str] = {}
    for p in picks:
        st_df = p.get("stocks")
        if st_df is None or (hasattr(st_df, "empty") and st_df.empty):
            continue
        for _, row in st_df.iterrows():
            d = row.to_dict()
            d["_theme"] = p["theme"]
            all_picks_rows.append(d)
            sid = str(d.get("stock_id", ""))
            existing_cat = d.get("催化劑", "")
            if sid and existing_cat and str(existing_cat).strip():
                catalysts[sid] = str(existing_cat)
    # 只對沒催化劑的 picks 再 annotate 一次
    missing_rows = [r for r in all_picks_rows
                    if str(r.get("stock_id", "")) and not catalysts.get(str(r.get("stock_id", "")))]
    if missing_rows:
        new_cats = stock_catalyst.annotate_picks_with_catalysts(missing_rows, market="TW")
        catalysts.update(new_cats)
    events = earnings_calendar.annotate_picks_with_events(all_picks_rows, market="TW")

    # === 訊號追蹤: 把催化劑利多 + 強勢族群龍頭記下來, 3 天後驗證 ===
    try:
        import signal_tracker
        for r in all_picks_rows:
            sid = str(r.get("stock_id", ""))
            if not sid:
                continue
            cur_price = r.get("現價")
            if cur_price is None:
                continue
            cat = catalysts.get(sid, "")
            # 強勢族群龍頭 → 隔日漲
            signal_tracker.record_signal(
                "strong_sector_leader", sid, name=r.get("stock_name", ""),
                predicted_price=cur_price, expected_direction="up",
                evaluate_after_days=1,
                extras={"theme": r.get("_theme", ""), "today_pct": r.get("今日%")},
            )
            # 有催化劑利多 → 3 天漲
            # B11 修正: 加括號明確化布林優先級, 避免後續 refactor 出錯
            if cat and ("利多" in cat or "催化" in cat):
                signal_tracker.record_signal(
                    "catalyst", sid, name=r.get("stock_name", ""),
                    predicted_price=cur_price, expected_direction="up",
                    evaluate_after_days=3,
                    extras={"catalyst": cat[:120]},
                )
    except Exception as _e:
        print(f"[market_open_picks] signal_tracker.record (open) failed: {_e}", flush=True)

    # 籌碼 / 主力換手分析 (Gemini)
    pick_ids = [str(r.get("stock_id", "")) for r in all_picks_rows if r.get("stock_id")]
    pick_names = {str(r.get("stock_id", "")): r.get("stock_name", "")
                   for r in all_picks_rows if r.get("stock_id")}
    chips = chip_analyzer.analyze_chips_batch(pick_ids, pick_names) if pick_ids else {}

    # 亞洲鄰近市場狀況
    asia = asia_markets.check_asia_markets()

    # 強勢族群裡的落後股 + Gemini 跟漲機會分析
    laggards = laggard_finder.find_tw_laggards()
    laggards_ai = laggard_finder.analyze_laggards_with_gemini(laggards, market="TW") if laggards else {}

    # 美股隔夜行情 (給 AI 跨市場推理用)
    us_overnight = get_us_overnight_summary()

    # 加權指數即時 (盤中 5m 線取最後一根, 比對昨收)
    twii_info = _fetch_twii_intraday()

    # Regime 偵測 (大盤狀態 — 空頭時 AI 分析跟 actionable_picks 都會降溫)
    regime: Dict = {}
    try:
        import regime_detector
        regime = regime_detector.detect_market_regime("TW")
    except Exception as _e:
        print(f"[market_open_picks] regime_detector failed: {_e}", flush=True)

    # 萌芽族群 (還沒上排行榜但有 leading indicator)
    emerging: List[Dict] = []
    try:
        import emerging_themes
        emerging = emerging_themes.find_emerging_themes(top_n=3)
    except Exception as _e:
        print(f"[market_open_picks] emerging_themes failed: {_e}", flush=True)

    return {
        "themes": themes_df.head(top_themes_n),
        "picks": picks,
        "prediction": prediction,
        "accuracy": accuracy,
        "catalysts": catalysts,
        "events": events,
        "asia": asia,
        "twii": twii_info,
        "laggards": laggards,
        "laggards_ai": laggards_ai,
        "us_overnight": us_overnight,
        "chips": chips,
        "regime": regime,
        "emerging": emerging,
    }


# ---------------------------------------------------------------------------
# 台股盤後分析 (15:00) — 含日韓對應產業比對 + AI 推理
# ---------------------------------------------------------------------------
def _compute_jp_kr_sector_pcts() -> Dict[str, float]:
    """計算每個 JP/KR 對應產業當日平均漲幅."""
    out: Dict[str, float] = {}
    for sec_name, syms in JP_KR_PROXIES.items():
        pcts = []
        for sym in syms:
            df = ds.fetch_yf_history(sym, period="5d", interval="1d")
            if df.empty or len(df) < 2:
                continue
            try:
                close = df["Close"].astype(float)
                last = float(close.iloc[-1])
                prev = float(close.iloc[-2])
                if prev > 0:
                    pcts.append((last / prev - 1) * 100)
            except Exception:
                continue
        if pcts:
            out[sec_name] = round(sum(pcts) / len(pcts), 2)
    return out


def _gemini_close_reasoning(tw_themes: pd.DataFrame, jp_pct: float, kr_pct: float,
                              jp_kr_sectors: Dict[str, float],
                              theme_to_asia: Dict[str, List[str]],
                              model: str = "gemini-2.5-flash") -> str:
    """讓 Gemini 寫盤後比對結論."""
    try:
        import ai_analyzer as _ai
    except ImportError:
        return ""
    if not _ai.gemini_available():
        return ""

    # 組對照表
    rows = []
    for _, r in tw_themes.head(8).iterrows():
        theme = r["題材"]
        avg = r.get("平均%")
        asia_secs = theme_to_asia.get(theme, [])
        asia_str = ", ".join(f"{s} {jp_kr_sectors.get(s, 'N/A')}%" for s in asia_secs) if asia_secs else "(無對應)"
        rows.append(f"  {theme}: 台股均漲 {avg}% | 對應日韓: {asia_str}")

    prompt = f"""你是亞洲股市分析師。今日台股已收盤，請根據以下資料推理：

【台股各熱門族群當日表現】
{chr(10).join(rows)}

【日股 / 韓股大盤當日漲跌】
日經 225: {jp_pct:+.2f}%
韓國 KOSPI: {kr_pct:+.2f}%

請用繁體中文回應 (避免太多 emoji 與廢話)，結構：

------ 區域同步 vs 台股獨秀 ------
- 哪些族群屬於「亞洲區域同步」漲跌? (台股 + 日韓對應產業同向)
- 哪些族群是「台股獨秀」? (台股漲、日韓不漲；或反過來)
- 對「台股獨秀」族群，可能是什麼原因 (法人佈局 / 訊息面 / 籌碼結構)

------ 明日開盤推測 ------
- 區域同步族群: 日韓收盤後到台股次日開盤前的影響
- 留意可能影響台股的隔夜訊號 (美股 / 油價 / 美元 / Fed 等)

------ 操作節奏 ------
- 給 1-2 個具體建議 (持續觀察 / 待回測支撐 / 區域同步續航 / 規避台股獨秀股 等)

結尾加「以上分析僅供參考，不構成投資建議」。"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 1500},
            safety_settings=_ai.get_safety_settings(),
        )
        return (resp.text or "").strip()
    except Exception as e:
        return f"(Gemini 推理失敗: {e})"


def _scalar(x) -> float:
    """把可能是 Series/ndarray/np scalar 的東西安全轉成 float。
    yfinance 偶爾回 MultiIndex 欄 → df['Close'].iloc[-1] 變 Series → 直接 float() 會炸
    'cannot convert the series to <class float>'。這裡取最後一個元素再轉, 徹底防呆。"""
    try:
        if hasattr(x, "iloc"):        # pandas Series
            x = x.iloc[-1]
        elif hasattr(x, "item"):      # numpy scalar / 0-d array
            try:
                return float(x.item())
            except Exception:
                pass
        return float(x)
    except Exception:
        return float("nan")


def _index_day_pct(df) -> float:
    """從日線 df 算「最後一日 vs 前一日」漲跌% — 全程防呆, 壞資料回 0.0 不炸。"""
    try:
        if df is None or df.empty or len(df) < 2:
            return 0.0
        c = df["Close"]
        last = _scalar(c.iloc[-1])
        prev = _scalar(c.iloc[-2])
        if prev and prev == prev and last == last:   # 非 0 且非 NaN
            return (last / prev - 1) * 100
    except Exception as e:
        print(f"[market_open_picks] _index_day_pct 失敗: {e}", flush=True)
    return 0.0


def get_tw_close_analysis() -> Dict:
    """台股盤後 15:00 分析 — 全日表現 + 日韓比對 + AI 推理結論."""
    # TW 各族群全日表現 (sector_pulse 用 yfinance, 收盤後就是當日完整漲跌)
    hot = sector_pulse.compute_hot_themes()
    themes_df = hot.get("themes")

    # 日韓大盤
    jp_df = ds.fetch_yf_history("^N225", period="5d", interval="1d")
    kr_df = ds.fetch_yf_history("^KS11", period="5d", interval="1d")
    jp_pct = _index_day_pct(jp_df)
    kr_pct = _index_day_pct(kr_df)

    # 日韓對應產業表現
    jp_kr_sectors = _compute_jp_kr_sector_pcts()

    # AI 推理
    ai_text = ""
    if themes_df is not None and not themes_df.empty:
        ai_text = _gemini_close_reasoning(
            themes_df, jp_pct, kr_pct, jp_kr_sectors, TW_TO_ASIA_SECTOR_MAP
        )

    # 加權指數收盤
    twii_df = ds.fetch_yf_history("^TWII", period="5d", interval="1d")
    twii_close = 0.0
    twii_pct = _index_day_pct(twii_df)
    if twii_df is not None and not twii_df.empty:
        twii_close = _scalar(twii_df["Close"].iloc[-1])
        if twii_close != twii_close:  # NaN 保護
            twii_close = 0.0

    # 盤後新增: 外資出貨嫌疑 + 隔日上漲機率高 top 3 + 避開訊號
    foreign_dumping = []
    next_day_picks = []
    avoid_picks = []
    try:
        import closing_analyzer
        foreign_dumping = closing_analyzer.analyze_foreign_dumping(top_n=5, max_scan=80)
    except Exception as e:
        print(f"[market_open_picks] foreign_dumping failed: {e}", flush=True)
    try:
        import closing_analyzer
        next_day_picks = closing_analyzer.pick_next_day_breakout(top_n=3, max_scan=150)
    except Exception as e:
        print(f"[market_open_picks] next_day_breakout failed: {e}", flush=True)
    try:
        import avoid_signals
        avoid_picks = avoid_signals.find_avoid_picks(top_n=5, max_scan=80)
    except Exception as e:
        print(f"[market_open_picks] avoid_picks failed: {e}", flush=True)

    # === 訊號追蹤: 紀錄盤後幾類預測 + 驗證昨天到期的 ===
    accuracy_block = ""
    try:
        import signal_tracker
        # 1. 先驗證所有到期的舊預測
        n_eval = signal_tracker.evaluate_pending()
        if n_eval:
            print(f"[market_open_picks] 驗證 {n_eval} 筆訊號", flush=True)
        # 2. 隔日上漲 Top 3 (closing_analyzer.pick_next_day_breakout)
        for d in next_day_picks[:3]:
            sid = str(d.get("stock_id", ""))
            cur = d.get("metrics", {}).get("close") or d.get("current")
            if sid and cur:
                signal_tracker.record_signal(
                    "next_day_breakout", sid, name=d.get("name", ""),
                    predicted_price=cur, expected_direction="up",
                    evaluate_after_days=1,
                    extras={"score": d.get("score")},
                )
        # 3. 避開訊號 → 3 日跌
        for d in avoid_picks[:5]:
            sid = str(d.get("stock_id", ""))
            cur = d.get("current")
            if sid and cur:
                signal_tracker.record_signal(
                    "avoid_pick", sid, name=d.get("name", ""),
                    predicted_price=cur, expected_direction="down",
                    evaluate_after_days=3,
                    extras={"score": d.get("score"), "reasons": d.get("reasons")},
                )
        # 4. 取本月準確率區塊 (推播末顯示)
        accuracy_block = signal_tracker.fmt_accuracy_block(lookback_days=30)
    except Exception as _e:
        print(f"[market_open_picks] signal_tracker (close) failed: {_e}", flush=True)

    # 「為什麼今天這樣走 + 明日重點」一句話 AI 摘要 (給推播首段用)
    why_summary = _gemini_one_line_why(
        twii_pct=round(twii_pct, 2),
        jp_pct=round(jp_pct, 2),
        kr_pct=round(kr_pct, 2),
        themes_df=themes_df,
        foreign_dumping=foreign_dumping,
    )

    return {
        "themes": themes_df,
        "twii_close": round(twii_close, 2),
        "twii_pct": round(twii_pct, 2),
        "jp_pct": round(jp_pct, 2),
        "kr_pct": round(kr_pct, 2),
        "jp_kr_sectors": jp_kr_sectors,
        "theme_to_asia_map": TW_TO_ASIA_SECTOR_MAP,
        "ai_text": ai_text,
        "foreign_dumping": foreign_dumping,
        "next_day_picks": next_day_picks,
        "avoid_picks": avoid_picks,
        "why_summary": why_summary,
        "accuracy_block": accuracy_block,
    }


def _gemini_one_line_why(twii_pct: float, jp_pct: float, kr_pct: float,
                          themes_df, foreign_dumping: list,
                          model: str = "gemini-2.5-flash") -> str:
    """產生「今日大盤動因 + 明日重點」一句話 (≤ 80 字).

    用最少資料快速問 Gemini, 失敗 / 沒 key 回 "" (推播會 skip 此區塊).
    """
    try:
        import ai_analyzer as _ai
        if not _ai.gemini_available():
            return ""
        # 組精簡 context
        top_themes = []
        bot_themes = []
        if themes_df is not None and not themes_df.empty:
            for _, r in themes_df.head(3).iterrows():
                top_themes.append(f"{r.get('題材','')} {r.get('平均%','')}%")
            for _, r in themes_df.tail(2).iterrows():
                bot_themes.append(f"{r.get('題材','')} {r.get('平均%','')}%")
        dump_str = ""
        if foreign_dumping:
            d0 = foreign_dumping[0]
            dump_str = f"外資出貨嫌疑首位: {d0.get('stock_id','')} {d0.get('name','')}"
        prompt = (
            f"今日台股加權 {twii_pct:+.2f}%, 日經 {jp_pct:+.2f}%, 韓 KOSPI {kr_pct:+.2f}%. "
            f"領漲族群: {', '.join(top_themes) or '(無顯著)'}. "
            f"落後族群: {', '.join(bot_themes) or '(無)'}. "
            f"{dump_str}. "
            "用 1 句話 (≤ 60 中文字) 總結「今日大盤怎麼走 + 為什麼」, "
            "再用 1 句話 (≤ 30 中文字) 點出「明日盯什麼」. 直接給 2 行內容, "
            "不要 markdown / 不要前後贅述."
        )
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 200},
            safety_settings=_ai.get_safety_settings(),
        )
        text = (getattr(resp, "text", None) or "").strip()
        # 防呆: 只取前 2 行, 各截 100 char
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()][:2]
        return "\n".join(ln[:100] for ln in lines)
    except Exception as e:
        print(f"[market_open_picks] gemini_one_line_why failed: {e}", flush=True)
        return ""


# ---------------------------------------------------------------------------
# 美股開盤分析
# ---------------------------------------------------------------------------
def _us_stock_metrics(symbol: str) -> Optional[Dict]:
    df = ds.fetch_yf_history(symbol, period="3mo", interval="1d")
    if df.empty or len(df) < 6:
        return None
    try:
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        chg = (last / prev - 1) * 100 if prev else 0
        five = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else None
        twenty = (last / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else None
        avg5_vol = vol.iloc[-6:-1].mean()
        ratio = float(vol.iloc[-1] / avg5_vol) if avg5_vol > 0 else None
        return {
            "symbol": symbol, "現價": round(last, 2),
            "今日%": round(chg, 2), "5日%": round(five, 2) if five is not None else None,
            "20日%": round(twenty, 2) if twenty is not None else None,
            "量比": round(ratio, 2) if ratio else None,
        }
    except Exception:
        return None


def _get_us_regime() -> Dict:
    """美股 regime — get_us_open_picks 用."""
    try:
        import regime_detector
        return regime_detector.detect_market_regime("US")
    except Exception:
        return {}


def get_us_open_picks(top_sectors_n: int = 3, picks_per_sector: int = 3,
                      growth_top: int = 5) -> Dict:
    """美股版本：板塊輪動排序 + 每板塊 3 檔 + 成長動能 5 檔."""
    sectors = ds.fetch_sector_rotation()
    if sectors is None or sectors.empty:
        return {"error": "板塊資料尚未取得"}

    # 用 1d_% 排序 (剛開盤後最敏感)
    if "1d_%" in sectors.columns:
        sectors_sorted = sectors.sort_values("1d_%", ascending=False).head(top_sectors_n)
    else:
        sectors_sorted = sectors.head(top_sectors_n)

    sector_picks: List[Dict] = []
    seen_us_sids: set = set()
    for _, sec in sectors_sorted.iterrows():
        sym = sec["symbol"]
        candidates = US_SECTOR_STOCKS.get(sym, [])
        # 跨板塊去重
        candidates = [c for c in candidates if c not in seen_us_sids]
        rows = []
        for s in candidates:
            m = _us_stock_metrics(s)
            if not m:
                continue
            m["score"] = _score_stock_momentum(m)
            rows.append(m)
        if rows:
            df = pd.DataFrame(rows)
            df = df[df["score"] > 0].sort_values("score", ascending=False).head(picks_per_sector)
            seen_us_sids.update(df["symbol"].tolist())
            sector_picks.append({"sector": f"{sym} {sec.get('sector','')}", "stocks": df})
        else:
            sector_picks.append({"sector": f"{sym} {sec.get('sector','')}", "stocks": pd.DataFrame()})

    # 成長動能極強 / 近期 IPO 池
    growth_rows = []
    for s in US_GROWTH_IPO_POOL:
        m = _us_stock_metrics(s)
        if not m:
            continue
        # 偏好高 RS / 高 momentum
        # 自製 growth score
        gscore = 0.0
        if m.get("20日%") and m["20日%"] > 0:
            gscore += min(3.0, m["20日%"] / 5.0)
        if m.get("5日%") and 0 <= m["5日%"] <= 15:
            gscore += 2.0
        elif m.get("5日%") and m["5日%"] > 15:
            gscore += 0.5
        if m.get("今日%") and m["今日%"] > 0:
            gscore += min(2.0, m["今日%"] / 2)
        if m.get("量比") and 1.2 <= m["量比"] <= 5:
            gscore += 2.0
        m["growth_score"] = round(gscore, 2)
        growth_rows.append(m)

    growth_df = pd.DataFrame(growth_rows)
    if not growth_df.empty:
        growth_df = growth_df.sort_values("growth_score", ascending=False).head(growth_top)

    # 大盤預測 + 準確率
    prediction = market_predictor.predict_us_pattern()
    if not prediction.get("error"):
        market_predictor.save_prediction(prediction)
    market_predictor.evaluate_pending_predictions()
    accuracy = market_predictor.accuracy_stats(market="US", lookback_days=30)

    # 美股催化劑
    all_us_rows = []
    for sp in sector_picks:
        st_df = sp.get("stocks")
        if st_df is None or st_df.empty:
            continue
        for _, row in st_df.iterrows():
            d = row.to_dict()
            d["stock_id"] = d.get("symbol", "")  # 統一欄位
            d["_sector"] = sp["sector"]
            all_us_rows.append(d)
    if growth_df is not None and not growth_df.empty:
        for _, row in growth_df.iterrows():
            d = row.to_dict()
            d["stock_id"] = d.get("symbol", "")
            d["_sector"] = "成長動能 / IPO"
            all_us_rows.append(d)
    catalysts = stock_catalyst.annotate_picks_with_catalysts(all_us_rows, market="US")
    events = earnings_calendar.annotate_picks_with_events(all_us_rows, market="US")

    # 美股板塊落後股
    laggards = laggard_finder.find_us_laggards()
    laggards_ai = laggard_finder.analyze_laggards_with_gemini(laggards, market="US") if laggards else {}

    # 5 支台股潛力股 (基於目前美股強勢板塊)
    macro_str = ""
    if sectors is not None and not sectors.empty:
        top3 = sectors.head(3)
        macro_str = "美股強勢板塊: " + ", ".join(
            f"{r['symbol']} {r['sector']} {r.get('1d_%', 0):+.2f}%"
            for _, r in top3.iterrows()
        )
    # 5 支台股潛力股 (受惠美股強勢板塊)
    # 此 step 依賴 FinMind, 失敗時 graceful skip 不要炸到整個推播
    try:
        potential_picks = potential_picker.find_picks_from_us_sectors(
            sectors, macro_context=macro_str, top_n=5
        )
    except Exception as e:
        print(f"[market_open_picks] potential_picker failed: {e}", flush=True)
        potential_picks = []

    # 美股版萌芽 sector (RS vs SPY 突破 + 成員放量背離)
    us_emerging = []
    try:
        import emerging_themes
        us_emerging = emerging_themes.find_us_emerging_sectors(top_n=3)
    except Exception as _e:
        print(f"[market_open_picks] us_emerging failed: {_e}", flush=True)

    return {
        "sectors": sectors_sorted,
        "sector_picks": sector_picks,
        "growth": growth_df,
        "prediction": prediction,
        "accuracy": accuracy,
        "catalysts": catalysts,
        "events": events,
        "laggards": laggards,
        "laggards_ai": laggards_ai,
        "potential_picks": potential_picks,
        "regime": _get_us_regime(),
        "emerging": us_emerging,
    }


def _gemini_us_close_reasoning(sectors_df: pd.DataFrame, spy_pct: float,
                                qqq_pct: float, dia_pct: float,
                                fg: dict, model: str = "gemini-2.5-flash") -> str:
    """美股盤後 Gemini 推理 (簡短版, 重點放對台股次日影響)."""
    try:
        import ai_analyzer as _ai
        if not _ai.gemini_available():
            return ""
    except ImportError:
        return ""
    sec_lines = []
    if sectors_df is not None and not sectors_df.empty:
        for _, r in sectors_df.head(8).iterrows():
            sec_lines.append(f"  {r.get('symbol')} {r.get('sector','')}: {r.get('1d_%','')}%")
    fg_str = f"F&G {fg.get('score','—'):.0f} ({fg.get('rating','')})" if fg else "F&G N/A"
    prompt = (
        f"美股盤後綜合: SPY {spy_pct:+.2f}%, QQQ {qqq_pct:+.2f}%, DIA {dia_pct:+.2f}%. {fg_str}\n\n"
        f"板塊輪動 (1d):\n" + "\n".join(sec_lines) + "\n\n"
        "請用繁體中文做盤後分析, 結構:\n"
        "## 全日綜合\n"
        "1-2 句總結今日美股強弱原因.\n"
        "## 對台股次日開盤推測\n"
        "1-2 句, 哪些族群可能受惠 / 受壓.\n"
        "## 操作建議\n"
        "1 句, 是否該加減碼 / 觀望.\n"
        "全文 ≤ 250 字, 不要前後贅述."
    )
    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 600},
            safety_settings=_ai.get_safety_settings(),
        )
        return (getattr(resp, "text", None) or "").strip()
    except Exception as e:
        print(f"[market_open_picks] _gemini_us_close_reasoning failed: {e}", flush=True)
        return ""


def get_us_overnight_summary() -> Dict:
    """美股隔夜 summary — 給 tw_open 推播當「美股隔夜行情」用. 比 get_us_open_picks 輕量."""
    spy = ds.fetch_yf_history("SPY", period="2d", interval="1d")
    qqq = ds.fetch_yf_history("QQQ", period="2d", interval="1d")
    dia = ds.fetch_yf_history("DIA", period="2d", interval="1d")
    def _p(df):
        if df is None or df.empty or len(df) < 2:
            return None
        try:
            c = df["Close"].astype(float)
            return round((float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100, 2)
        except Exception:
            return None
    sectors = ds.fetch_sector_rotation()
    fg = ds.fetch_fear_greed()
    return {
        "SPY": {"pct": _p(spy)},
        "QQQ": {"pct": _p(qqq)},
        "DIA": {"pct": _p(dia)},
        "sectors": sectors,
        "fg": fg,
    }


def find_tw_beneficiaries_from_us(us_sectors_df) -> Dict:
    """美股板塊強勢 → 對應的台股族群 (簡單映射, 給 us_close 推播用)."""
    if us_sectors_df is None or us_sectors_df.empty:
        return {}
    tw_map = {
        "XLK": ("AI 半導體", ["2330", "2454", "3661", "6669"]),
        "XLF": ("金控", ["2882", "2891", "5880"]),
        "XLE": ("塑化石化", ["1301", "1303", "6505"]),
        "XLV": ("生技", ["1707", "4128"]),
        "XLY": ("零售消費", ["1216", "2912"]),
    }
    out = {}
    try:
        for _, r in us_sectors_df.head(5).iterrows():
            sym = r.get("symbol")
            r1 = r.get("1d_%")
            if r1 is None or r1 < 0.5:
                continue
            if sym not in tw_map:
                continue
            theme, sids = tw_map[sym]
            picks = [{"stock_id": s, "name": ""} for s in sids[:3]]
            out[theme] = {
                "drivers": [f"{sym} {r1:+.2f}%"],
                "picks": picks,
            }
    except Exception as e:
        print(f"[market_open_picks] find_tw_beneficiaries_from_us failed: {e}", flush=True)
    return out


def _gemini_recommend_tw_after_us(beneficiaries: Dict, model: str = "gemini-2.5-flash") -> Dict[str, str]:
    """對 beneficiaries 裡每檔 TW 股票, 用 Gemini 給 1 句受惠原因. 失敗回 {}."""
    if not beneficiaries:
        return {}
    try:
        import ai_analyzer as _ai
        if not _ai.gemini_available():
            return {}
        all_picks = []
        for theme, info in beneficiaries.items():
            for p in info.get("picks", []):
                all_picks.append({"theme": theme, "stock_id": p["stock_id"]})
        if not all_picks:
            return {}
        lines = [f"  {p['stock_id']} (受惠 {p['theme']})" for p in all_picks]
        prompt = (
            "下列台股可能受惠美股板塊強勢, 請用繁體中文 1 句話解釋每檔受惠原因 (≤ 30 字):\n"
            + "\n".join(lines) + "\n\n"
            "回覆嚴格 JSON, key=stock_id, value=1 句說明. 範例:\n"
            "{\"2330\": \"AI 晶片需求帶動先進製程訂單\"}\n"
            "不要前後贅述, 只回 JSON."
        )
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.3, "max_output_tokens": 800,
                                "response_mime_type": "application/json"},
            safety_settings=_ai.get_safety_settings(),
        )
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            return {}
        import json, re
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[market_open_picks] _gemini_recommend_tw_after_us failed: {e}", flush=True)
        return {}


def _gemini_holiday_reasoning(spy_pct: float, qqq_pct: float, dia_pct: float,
                               asia: dict, news: list, model: str = "gemini-2.5-flash") -> str:
    """假日重大消息推理 — 給 holiday_news 推播用."""
    try:
        import ai_analyzer as _ai
        if not _ai.gemini_available():
            return ""
    except ImportError:
        return ""
    news_lines = []
    for n in (news or [])[:8]:
        t = n.get("title_zh") or n.get("title", "")
        news_lines.append(f"  - {t[:100]}")
    prompt = (
        f"美股: SPY {spy_pct:+.2f}%, QQQ {qqq_pct:+.2f}%, DIA {dia_pct:+.2f}%\n\n"
        f"重要新聞:\n" + "\n".join(news_lines) + "\n\n"
        "請用繁體中文寫休市日重大消息分析:\n"
        "## 重點回顧\n2 句概述今日國際重要動態.\n"
        "## 對台股下個交易日影響\n2 句, 哪些族群會受惠 / 受壓.\n"
        "全文 ≤ 200 字."
    )
    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 500},
            safety_settings=_ai.get_safety_settings(),
        )
        return (getattr(resp, "text", None) or "").strip()
    except Exception as e:
        print(f"[market_open_picks] _gemini_holiday_reasoning failed: {e}", flush=True)
        return ""


def get_holiday_news_summary() -> Dict:
    """假日重大消息推播資料."""
    spy = ds.fetch_yf_history("SPY", period="2d", interval="1d")
    qqq = ds.fetch_yf_history("QQQ", period="2d", interval="1d")
    dia = ds.fetch_yf_history("DIA", period="2d", interval="1d")
    def _p(df):
        if df is None or df.empty or len(df) < 2:
            return 0.0
        try:
            c = df["Close"].astype(float)
            return round((float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100, 2)
        except Exception:
            return 0.0
    spy_pct = _p(spy)
    qqq_pct = _p(qqq)
    dia_pct = _p(dia)
    asia = {}
    try:
        import asia_markets
        asia = asia_markets.check_asia_markets()
    except Exception:
        pass
    news = []
    try:
        import news_sources
        # 用 fetch_finance_news 聚合純財經 RSS + 含財經關鍵字的一般新聞
        # (避免推到假日的 weekend 政治 / 體育 / 娛樂等噪音)
        raw_news = news_sources.fetch_finance_news(max_items=10)
        news = raw_news or []
        # 補 sentiment score (title 關鍵字評分) — fmt_holiday_news 用 sentiment 顯示 📈/📉/▪
        try:
            import stock_catalyst
            for n in news:
                s = stock_catalyst._score_news_sentiment(n.get("title", "") or "", lang="en")
                n["sentiment"] = s.get("score", 0)
        except Exception as _e:
            print(f"[market_open_picks] news sentiment scoring failed: {_e}", flush=True)
        # 排序: sentiment 強 (利多 / 利空) 的優先, 中性的後面 → 提高假日推播信息密度
        news.sort(key=lambda n: abs(n.get("sentiment", 0) or 0), reverse=True)
    except Exception as e:
        print(f"[market_open_picks] fetch_finance_news failed: {e}", flush=True)
    oil = {}
    try:
        import news_sources
        oil = news_sources.fetch_oil_signal()
    except Exception:
        pass
    fg = ds.fetch_fear_greed()
    trump = []
    try:
        import news_sources
        trump = news_sources.fetch_trump_truth_social(max_items=3)
    except Exception:
        pass
    potential_picks = []
    try:
        import potential_picker
        # 用真實存在的 find_picks_for_holiday (我之前重建時誤寫成 pick_top_potential_stocks ghost)
        # macro_context 給 Gemini 一些前提資料 (美股漲跌 + 油價 + 重大新聞)
        macro_ctx = (
            f"美股: SPY {spy_pct:+.2f}%, QQQ {qqq_pct:+.2f}%, DIA {dia_pct:+.2f}%. "
            f"WTI 油價 ${oil.get('price', '—')} ({oil.get('pct_5d', 0):+.1f}% 5d). "
            f"重大新聞: " + "; ".join(n.get("title", "")[:60] for n in (news or [])[:3])
        )
        potential_picks = potential_picker.find_picks_for_holiday(
            macro_context=macro_ctx, top_n=5,
        )
    except Exception as _e:
        print(f"[market_open_picks] potential_picks (holiday) failed: {_e}", flush=True)
    ai_text = _gemini_holiday_reasoning(spy_pct, qqq_pct, dia_pct, asia, news)
    return {
        "spy_pct": spy_pct, "qqq_pct": qqq_pct, "dia_pct": dia_pct,
        "asia": asia, "news": news, "oil": oil, "fg": fg, "trump": trump,
        "potential_picks": potential_picks, "ai_text": ai_text,
    }


def get_weekend_recap_summary() -> Dict:
    """週末重點摘要 = holiday_news 內容 + 7d 全球表現 + crypto 週狀態 + ETF 對比 + Gemini 下週展望.

    跟 get_holiday_news_summary 共用 base, 但加更多 weekend-specific 內容.
    """
    base = get_holiday_news_summary()

    # ===== 7 日全球指數表現 =====
    week_perf: Dict = {}
    INDICES_7D = {
        "^GSPC":  "S&P 500",
        "^IXIC":  "Nasdaq",
        "^SOX":   "費半",
        "^TWII":  "台灣加權",
        "^N225":  "日經 225",
        "^KS11":  "韓國 KOSPI",
    }
    for sym, name in INDICES_7D.items():
        try:
            df = ds.fetch_yf_history(sym, period="10d", interval="1d")
            if df is None or df.empty or len(df) < 5:
                continue
            c = df["Close"].astype(float)
            last = float(c.iloc[-1])
            # 5d ago (大約 1 週前 — 跳過週末沒交易日)
            wk_ago = float(c.iloc[-6]) if len(c) >= 6 else float(c.iloc[0])
            pct_5d = (last / wk_ago - 1) * 100
            week_perf[sym] = {"name": name, "last": last, "pct_5d": round(pct_5d, 2)}
        except Exception as e:
            print(f"[weekend_recap] week_perf {sym} failed: {e}", flush=True)

    # ===== 加密貨幣 7 日 =====
    crypto_perf: Dict = {}
    for sym, name in [("BTC-USD", "BTC"), ("ETH-USD", "ETH")]:
        try:
            df = ds.fetch_yf_history(sym, period="10d", interval="1d")
            if df is None or df.empty or len(df) < 5:
                continue
            c = df["Close"].astype(float)
            last = float(c.iloc[-1])
            wk_ago = float(c.iloc[-6]) if len(c) >= 6 else float(c.iloc[0])
            pct_5d = (last / wk_ago - 1) * 100
            crypto_perf[sym] = {"name": name, "last": last, "pct_5d": round(pct_5d, 2)}
        except Exception as e:
            print(f"[weekend_recap] crypto_perf {sym} failed: {e}", flush=True)

    # ===== ETF 持股對比 (從 monitor_state.active_etf_holdings 讀目前持股) =====
    etf_snapshot: list = []
    try:
        import watchlist_store
        import active_etf_monitor
        state = watchlist_store.load_monitor_state()
        etf_state = state.get("active_etf_holdings", {})
        for code, cfg in active_etf_monitor.ETF_CONFIG.items():
            entry = etf_state.get(code, {})
            stocks = entry.get("stocks", {})
            if not stocks:
                continue
            # top 5 持股
            sorted_stocks = sorted(
                stocks.items(), key=lambda x: -float(x[1].get("pct", 0) or 0)
            )[:5]
            etf_snapshot.append({
                "etf_code": code,
                "etf_name": cfg.get("name", code),
                "data_date": entry.get("last_data_date", "—"),
                "top5": [
                    {"sid": sid, "name": s.get("name", ""), "pct": s.get("pct", 0)}
                    for sid, s in sorted_stocks
                ],
            })
    except Exception as e:
        print(f"[weekend_recap] etf_snapshot failed: {e}", flush=True)

    # ===== Gemini 下週展望 =====
    next_week_outlook = ""
    try:
        import ai_analyzer as _ai
        if _ai.gemini_available():
            import google.generativeai as genai
            genai.configure(api_key=_ai.get_gemini_key())
            news = base.get("news") or []
            news_titles = "\n".join(
                f"  - {n.get('title','')[:90]}" for n in news[:6]
            )
            week_perf_str = "\n".join(
                f"  {w['name']}: {w['pct_5d']:+.2f}%"
                for w in week_perf.values()
            )
            prompt = (
                f"今週全球指數表現:\n{week_perf_str}\n\n"
                f"重要新聞:\n{news_titles}\n\n"
                f"請用繁體中文寫「下週台股展望」, 嚴格 ≤250 字:\n"
                f"## 🌐 本週回顧 (1-2句)\n"
                f"## 🔭 下週關注 (3-4 點重要事件 / 數據 / 風險)\n"
                f"## 📊 操作節奏 (2 句, 短線 / 中線建議)\n"
                f"結尾加 '僅供參考'."
            )
            m = genai.GenerativeModel("gemini-2.5-flash")
            resp = m.generate_content(
                prompt,
                generation_config={"temperature": 0.4, "max_output_tokens": 800},
                safety_settings=_ai.get_safety_settings(),
            )
            next_week_outlook = (getattr(resp, "text", "") or "").strip()
    except Exception as e:
        print(f"[weekend_recap] gemini next_week failed: {e}", flush=True)

    base["week_perf"] = week_perf
    base["crypto_perf"] = crypto_perf
    base["etf_snapshot"] = etf_snapshot
    base["next_week_outlook"] = next_week_outlook
    base["is_weekend_recap"] = True
    return base


def get_us_close_analysis() -> Dict:
    """美股收盤 +2 小時全日綜合 + 對台股次日影響推理."""
    sectors = ds.fetch_sector_rotation()
    fg = ds.fetch_fear_greed()
    spy_df = ds.fetch_yf_history("SPY", period="2d", interval="1d")
    qqq_df = ds.fetch_yf_history("QQQ", period="2d", interval="1d")
    dia_df = ds.fetch_yf_history("DIA", period="2d", interval="1d")
    def _pct(df):
        if df is None or df.empty or len(df) < 2: return 0.0
        try:
            c = df["Close"].astype(float)
            return (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
        except Exception:
            return 0.0
    spy_pct = _pct(spy_df); qqq_pct = _pct(qqq_df); dia_pct = _pct(dia_df)
    ai_text = _gemini_us_close_reasoning(sectors, spy_pct, qqq_pct, dia_pct, fg)
    beneficiaries = find_tw_beneficiaries_from_us(sectors)
    beneficiary_reasons = _gemini_recommend_tw_after_us(beneficiaries) if beneficiaries else {}
    potential_picks = []
    try:
        import potential_picker
        macro_ctx = (
            f"美股盤後: SPY {spy_pct:+.2f}%, QQQ {qqq_pct:+.2f}%, DIA {dia_pct:+.2f}%."
        )
        potential_picks = potential_picker.find_picks_for_holiday(
            macro_context=macro_ctx, top_n=5,
        )
    except Exception as e:
        print(f"[market_open_picks] potential_picks (us_close) failed: {e}", flush=True)
    return {
        "spy_pct": round(spy_pct, 2), "qqq_pct": round(qqq_pct, 2), "dia_pct": round(dia_pct, 2),
        "sectors": sectors, "fg": fg, "ai_text": ai_text,
        "beneficiaries": beneficiaries, "beneficiary_reasons": beneficiary_reasons,
        "potential_picks": potential_picks, "regime": _get_us_regime(),
    }
