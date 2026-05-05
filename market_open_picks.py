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
    "PLTR", "SMCI", "MSTR", "COIN",            # high-momentum
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
    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}

    for theme in top_themes:
        df = leaders_map.get(theme, pd.DataFrame())
        if df is None or df.empty:
            stock_ids = sector_pulse.TW_THEMES.get(theme, [])
            market_map = info.set_index("stock_id")["type"].to_dict()
            df = sector_pulse.fetch_intraday_metrics(stock_ids, market_map)
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
    all_picks_rows = []
    for p in picks:
        st_df = p.get("stocks")
        if st_df is None or (hasattr(st_df, "empty") and st_df.empty):
            continue
        for _, row in st_df.iterrows():
            d = row.to_dict()
            d["_theme"] = p["theme"]
            all_picks_rows.append(d)
    catalysts = stock_catalyst.annotate_picks_with_catalysts(all_picks_rows, market="TW")
    events = earnings_calendar.annotate_picks_with_events(all_picks_rows, market="TW")

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


def get_tw_close_analysis() -> Dict:
    """台股盤後 15:00 分析 — 全日表現 + 日韓比對 + AI 推理結論."""
    # TW 各族群全日表現 (sector_pulse 用 yfinance, 收盤後就是當日完整漲跌)
    hot = sector_pulse.compute_hot_themes()
    themes_df = hot.get("themes")

    # 日韓大盤
    jp_df = ds.fetch_yf_history("^N225", period="5d", interval="1d")
    kr_df = ds.fetch_yf_history("^KS11", period="5d", interval="1d")
    jp_pct = 0.0
    kr_pct = 0.0
    if not jp_df.empty and len(jp_df) >= 2:
        c = jp_df["Close"].astype(float)
        jp_pct = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
    if not kr_df.empty and len(kr_df) >= 2:
        c = kr_df["Close"].astype(float)
        kr_pct = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100

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
    twii_pct = 0.0
    if not twii_df.empty and len(twii_df) >= 2:
        c = twii_df["Close"].astype(float)
        twii_close = float(c.iloc[-1])
        twii_pct = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100

    # 盤後新增: 外資出貨嫌疑 + 隔日上漲機率高 top 3
    foreign_dumping = []
    next_day_picks = []
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
    }


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
    }


# ---------------------------------------------------------------------------
# 美股盤後 +2h 分析 (含台股次日開盤推測)
# ---------------------------------------------------------------------------
def _gemini_us_close_reasoning(sectors_df: pd.DataFrame, spy_pct: float,
                                qqq_pct: float, dia_pct: float,
                                fg: dict, model: str = "gemini-2.5-flash") -> str:
    try:
        import ai_analyzer as _ai
    except ImportError:
        return ""
    if not _ai.gemini_available():
        return ""

    fg_line = ""
    if fg and fg.get("score") is not None:
        fg_line = f"CNN F&G: {fg['score']:.0f} ({fg.get('rating')})"

    sector_lines = []
    if sectors_df is not None and not sectors_df.empty:
        for _, r in sectors_df.iterrows():
            sym = r.get("symbol")
            name = r.get("sector", "")
            r1 = r.get("1d_%", 0)
            sector_lines.append(f"  {sym} {name}: {r1:+.2f}%")

    sector_block = "\n".join(sector_lines) if sector_lines else "(無資料)"

    prompt = f"""你是美股 + 全球宏觀分析師。今日美股已收盤，請推理：

【美股大盤當日】
SPY: {spy_pct:+.2f}%
QQQ: {qqq_pct:+.2f}%
DIA: {dia_pct:+.2f}%
{fg_line}

【板塊輪動 (1d)】
{sector_block}

請用繁體中文回應 (避免太多 emoji)，結構：

------ 今日美股總結 ------
- 主流方向 (科技 / 防禦 / 循環 / 能源 等)
- 強勢板塊與背後的市場原因
- 市場情緒判讀

------ 對台股次日開盤推測 ------
- 區域影響: 美股強勢時，台股 ADR / 半導體股可能開高 N% 左右
- 留意風險: Fed / 地緣 / 公司財報 等任何隔夜訊號
- 預估台股開盤偏向: 開高走高 / 開高走低 / 開低 / 平盤

------ 給台灣投資人的操作建議 ------
- 1-2 個具體可行建議

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


def get_us_overnight_summary() -> Dict:
    """取美股最近一次收盤的隔夜表現 (給 TW 開盤分析參考)."""
    out = {}
    for sym, name in [("SPY", "S&P 500"), ("QQQ", "NASDAQ"), ("DIA", "DOW")]:
        df = ds.fetch_yf_history(sym, period="3d", interval="1d")
        if df.empty or len(df) < 2:
            continue
        try:
            c = df["Close"].astype(float)
            pct = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
            out[sym] = {"name": name, "pct": round(pct, 2)}
        except Exception:
            continue
    sectors = ds.fetch_sector_rotation()
    if sectors is not None and not sectors.empty:
        out["sectors"] = sectors
    fg = ds.fetch_fear_greed()
    if fg:
        out["fg"] = fg
    return out


def find_tw_beneficiaries_from_us(us_sectors_df: pd.DataFrame,
                                    min_pct: float = 0.5,
                                    top_per_theme: int = 4) -> Dict[str, List[Dict]]:
    """根據美股強勢板塊，找對應的台股可能受惠者.
    回傳: {tw_theme: [{stock_id, name, us_drivers}, ...]}
    """
    if us_sectors_df is None or us_sectors_df.empty or "1d_%" not in us_sectors_df.columns:
        return {}

    strong = us_sectors_df[us_sectors_df["1d_%"] > min_pct]
    if strong.empty:
        return {}

    info = ds.get_taiwan_stock_info()
    name_map = info.set_index("stock_id")["stock_name"].to_dict() if "stock_name" in info.columns else {}

    # 收集 TW 題材 → driver
    theme_drivers: Dict[str, List[str]] = {}
    for _, r in strong.iterrows():
        sym = r.get("symbol", "")
        name = r.get("sector", "")
        pct = r.get("1d_%", 0)
        themes = US_SECTOR_TO_TW_THEMES.get(sym, [])
        for t in themes:
            theme_drivers.setdefault(t, []).append(f"{sym} {name} {pct:+.2f}%")

    out: Dict[str, List[Dict]] = {}
    seen: set = set()
    for theme, drivers in theme_drivers.items():
        stock_ids = sector_pulse.TW_THEMES.get(theme, [])
        picks = []
        for sid in stock_ids:
            if sid in seen:
                continue
            picks.append({
                "stock_id": sid,
                "name": name_map.get(sid, ""),
                "theme": theme,
            })
            seen.add(sid)
            if len(picks) >= top_per_theme:
                break
        if picks:
            out[theme] = {"drivers": drivers, "picks": picks}
    return out


def _gemini_recommend_tw_after_us(beneficiaries: Dict, model: str = "gemini-2.5-flash") -> Dict[str, str]:
    """讓 Gemini 為每檔台股寫 1 句受惠美股的具體理由."""
    try:
        import ai_analyzer as _ai
    except ImportError:
        return {}
    if not _ai.gemini_available() or not beneficiaries:
        return {}

    blocks = []
    all_ids: List[str] = []
    for theme, info in beneficiaries.items():
        drivers = ", ".join(info.get("drivers", []))
        picks = info.get("picks", [])
        if not picks:
            continue
        names = ", ".join(f"{p['stock_id']} {p['name']}" for p in picks)
        blocks.append(f"[{theme}] 受美股驅動: {drivers}\n候選台股: {names}")
        all_ids.extend(p["stock_id"] for p in picks)

    if not blocks:
        return {}

    prompt = f"""你是熟悉台美股聯動的分析師。下面是今日美股強勢板塊 → 對應台股題材 → 候選個股。
請為每檔候選台股寫 1 句具體「受惠美股的方式」(產品 / 客戶 / 訂單 / 供應鏈位置 等)。

請用嚴格 JSON 格式回應，key 是 stock_id，value 是 1 句中文理由。
範例: {{"6669": "AI Server ODM Direct，直接接 NVIDIA / Meta 大單", "2382": "Microsoft AI 伺服器代工龍頭"}}

不要加任何前後 markdown，只回 JSON。

待分析:

{chr(10).join(blocks)}"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={
                "temperature": 0.4,
                "max_output_tokens": 1500,
                "response_mime_type": "application/json",
            },
            safety_settings=_ai.get_safety_settings(),
        )
        text = (resp.text or "").strip()
        if not text:
            return {}
        import json, re
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        data = json.loads(text)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _gemini_holiday_reasoning(spy_pct: float, qqq_pct: float, dia_pct: float,
                                asia_data: dict, oil: dict, macro: dict,
                                top_news: list, trump_posts: list,
                                model: str = "gemini-2.5-flash") -> str:
    """假日重大消息 Gemini 推理對台股下次開盤的影響."""
    try:
        import ai_analyzer as _ai
    except ImportError:
        return ""
    if not _ai.gemini_available():
        return ""

    # 組 prompt 各部分
    asia_str = ""
    if asia_data and asia_data.get("snapshot"):
        asia_lines = [f"  {s['country']} {s['market']}: {s['daily_pct']:+.2f}%"
                       for s in asia_data["snapshot"]]
        asia_str = "亞洲市場今日:\n" + "\n".join(asia_lines)
        if asia_data.get("events"):
            asia_str += "\n  異常事件: " + "; ".join(
                f"{e['country']} {e['market']} [{e['event']}]" for e in asia_data["events"][:5]
            )

    oil_str = ""
    if oil:
        oil_str = f"WTI 原油: ${oil.get('price')} ({oil.get('pct_5d', 0):+.1f}% 5d) — {oil.get('signal','')}"

    macro_str = ""
    if macro:
        parts = [f"{n}: {m['value']} ({m['pct_5d']:+.2f}%)" for n, m in macro.items()]
        macro_str = "Macro: " + " · ".join(parts)

    news_str = ""
    if top_news:
        news_lines = []
        for n in top_news[:12]:
            sent = n.get("sentiment", 0)
            tag = "📈" if sent > 0 else ("📉" if sent < 0 else "▪")
            t = n.get("title_zh") or n.get("title", "")
            news_lines.append(f"  {tag} [{n.get('source','')}] {t}")
        news_str = "今日重要新聞:\n" + "\n".join(news_lines)

    trump_str = ""
    if trump_posts:
        trump_lines = []
        for t in trump_posts[:3]:
            text = t.get("text", "")[:200]
            trump_lines.append(f"  - {text}")
        trump_str = "Trump Truth Social:\n" + "\n".join(trump_lines)

    prompt = f"""今日台股休市。請整理今日全球 / 區域市場重大消息，並推理對台股「下個交易日開盤」的可能影響。

【美股大盤】
SPY: {spy_pct:+.2f}%   QQQ: {qqq_pct:+.2f}%   DIA: {dia_pct:+.2f}%

{asia_str}

{oil_str}
{macro_str}

{news_str}

{trump_str}

------

請用繁體中文回應 (避免太多 emoji)，結構：

------ 今日重要事件摘要 ------
- 列 3-5 個對台股影響最大的事件 / 新聞 / 政治面 / Macro
- 每個 1-2 句

------ 利多 vs 利空 分類 ------
明確標註哪些是台股下次開盤的「利多」，哪些是「利空」

------ 對台股下個開盤推測 ------
- 評估 開高 / 開低 / 平盤 的可能性
- 給出最可能情境 + 1-2 個風險情境

------ 給投資人準備建議 ------
- 1-2 個具體可行動作 (例如「持股觀望」、「準備加碼名單」、「降低部位」)

結尾加「以上分析僅供參考，不構成投資建議」。"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.5, "max_output_tokens": 1500},
            safety_settings=_ai.get_safety_settings(),
        )
        return (resp.text or "").strip()
    except Exception as e:
        return f"(Gemini 推理失敗: {e})"


def get_holiday_news_summary() -> Dict:
    """台股休市日 22:00 推播：彙整全球重大消息 + Gemini 推理對台股下次開盤影響."""
    # 美股大盤 (US 可能在交易中或盤後)
    spy_df = ds.fetch_yf_history("SPY", period="3d", interval="1d")
    qqq_df = ds.fetch_yf_history("QQQ", period="3d", interval="1d")
    dia_df = ds.fetch_yf_history("DIA", period="3d", interval="1d")

    def _pct(df):
        if df.empty or len(df) < 2:
            return 0.0
        try:
            c = df["Close"].astype(float)
            return (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
        except Exception:
            return 0.0

    spy_pct = _pct(spy_df)
    qqq_pct = _pct(qqq_df)
    dia_pct = _pct(dia_df)

    # 亞洲市場
    asia = asia_markets.check_asia_markets()

    # Macro / Oil
    try:
        import news_sources
        oil = news_sources.fetch_oil_signal()
        macro = news_sources.fetch_macro_indicators()
        news = news_sources.fetch_world_news()
        news = news_sources.enrich_news_with_sentiment(news)
        # 翻譯成中文 (Gemini 可用時)
        try:
            import ai_analyzer as _ai
            if _ai.gemini_available():
                news = news_sources.translate_news_titles(news)
        except Exception:
            pass
        # 排序: 利多/利空優先，中性最後
        news.sort(key=lambda n: -abs(n.get("sentiment", 0)))
        trump = news_sources.fetch_trump_truth_social(max_items=3)
    except Exception:
        oil, macro, news, trump = {}, {}, [], []

    # F&G
    fg = ds.fetch_fear_greed()

    # Gemini 推理
    ai_text = _gemini_holiday_reasoning(
        spy_pct, qqq_pct, dia_pct,
        asia, oil, macro, news, trump,
    )

    # 5 支台股潛力股 (假日復盤後可關注)
    macro_str = (
        f"假日復盤前 macro: 美股 SPY {spy_pct:+.2f}%, QQQ {qqq_pct:+.2f}%, "
        f"日經 {asia.get('snapshot',[{}])[0].get('daily_pct', 0) if asia.get('snapshot') else 0:+.2f}%, "
        f"F&G: {fg.get('rating','')}"
    )
    potential_picks = potential_picker.find_picks_for_holiday(
        macro_context=macro_str, top_n=5
    )

    return {
        "spy_pct": round(spy_pct, 2),
        "qqq_pct": round(qqq_pct, 2),
        "dia_pct": round(dia_pct, 2),
        "fg": fg,
        "asia": asia,
        "oil": oil,
        "macro": macro,
        "news": news[:12],
        "trump": trump,
        "ai_text": ai_text,
        "potential_picks": potential_picks,
    }


def get_us_close_analysis() -> Dict:
    """美股收盤 +2 小時 (18:00 EDT / 06:00 隔日台北) 全日綜合 + 對台股次日影響推理."""
    sectors = ds.fetch_sector_rotation()
    fg = ds.fetch_fear_greed()

    spy_df = ds.fetch_yf_history("SPY", period="2d", interval="1d")
    qqq_df = ds.fetch_yf_history("QQQ", period="2d", interval="1d")
    dia_df = ds.fetch_yf_history("DIA", period="2d", interval="1d")

    def _pct(df):
        if df.empty or len(df) < 2:
            return 0.0
        try:
            c = df["Close"].astype(float)
            return (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100
        except Exception:
            return 0.0

    spy_pct = _pct(spy_df)
    qqq_pct = _pct(qqq_df)
    dia_pct = _pct(dia_df)

    ai_text = _gemini_us_close_reasoning(sectors, spy_pct, qqq_pct, dia_pct, fg)

    # 受惠美股的台股推薦
    beneficiaries = find_tw_beneficiaries_from_us(sectors, min_pct=0.5)
    beneficiary_reasons = _gemini_recommend_tw_after_us(beneficiaries) if beneficiaries else {}

    # 5 支台股潛力股 + 目標價
    macro_str = (
        f"美股 SPY {spy_pct:+.2f}% / QQQ {qqq_pct:+.2f}%, "
        f"F&G: {fg.get('rating','')}"
        if fg else f"美股 SPY {spy_pct:+.2f}% / QQQ {qqq_pct:+.2f}%"
    )
    potential_picks = potential_picker.find_picks_from_us_sectors(
        sectors, macro_context=macro_str, top_n=5
    )

    return {
        "sectors": sectors,
        "spy_pct": round(spy_pct, 2),
        "qqq_pct": round(qqq_pct, 2),
        "dia_pct": round(dia_pct, 2),
        "fg": fg,
        "ai_text": ai_text,
        "beneficiaries": beneficiaries,
        "beneficiary_reasons": beneficiary_reasons,
        "potential_picks": potential_picks,
    }
