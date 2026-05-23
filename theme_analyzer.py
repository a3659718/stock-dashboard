"""
theme_analyzer.py
題材 / 催化劑 / 板塊熱度評分 — 給 us_upside_screener / actionable_picks 共用.

為什麼需要這個:
  美股「爆發」很大成分是 narrative-driven (AI / 核能 / 量子 / 加密 / 機器人...).
  純技術面會錯過「題材剛起來但價格還沒噴」的標的, 也無法區分:
    - NVDA breakout + AI narrative 強 → 高勝率
    - XYZ breakout 但沒題材 → 容易假突破

分數組成 (0-100):
  - 個股題材熱度 (news + keyword): 0-40
  - 板塊輪動排名 (XLK/XLE/... 1d 漲跌): 0-25
  - 財報接近度 (財報前 7 天有 boost, 但需謹慎): 0-15
  - 個股新聞熱度 (recent news count): 0-20

對外接口:
    theme_score(symbol, sector_etf=None) -> dict
        {
            "total_score": 0-100,
            "narrative_tags": ["AI", "Quantum", ...],
            "theme_strength": "strong" | "moderate" | "weak" | "none",
            "news_count": int,
            "earnings_in_days": int or None,
            "sector_rotation_rank": int or None,
            "components": {...詳細分數...}
        }

    batch_theme_scores(symbols: List[str]) -> Dict[str, dict]
"""
from __future__ import annotations

import datetime as dt
import re
import time
from typing import Dict, List, Optional

import data_sources as ds

try:
    import yfinance as yf  # type: ignore
    _YF_OK = True
except Exception:
    _YF_OK = False

# Streamlit cache
try:
    import streamlit as st  # type: ignore
except Exception:
    class _NoOp:
        def cache_data(self, *args, **kwargs):
            def deco(f):
                return f
            if len(args) == 1 and callable(args[0]):
                return args[0]
            return deco
    st = _NoOp()  # type: ignore


# ---------------------------------------------------------------------------
# 題材關鍵字 (2024-2026 主流 narrative)
# ---------------------------------------------------------------------------
THEME_KEYWORDS = {
    "AI / LLM": [
        "ai", "artificial intelligence", "gpt", "llm", "chatbot", "generative",
        "openai", "anthropic", "claude", "gemini", "copilot", "agent",
        "foundation model", "transformer", "rag", "fine-tuning",
    ],
    "Semiconductors / HBM": [
        "chip", "semiconductor", "gpu", "wafer", "fab", "foundry",
        "hbm", "chiplet", "asic", "tpu", "advanced packaging", "cowos",
    ],
    "Cloud / Data Center": [
        "cloud", "aws", "azure", "gcp", "data center", "datacenter",
        "hyperscaler", "kubernetes",
    ],
    "Cybersecurity": [
        "cyber", "security", "ransomware", "zero trust", "sase", "siem",
        "breach", "phishing", "xdr",
    ],
    "Nuclear / Energy": [
        "nuclear", "smr", "small modular reactor", "fusion", "uranium",
        "reactor", "power grid", "lng", "natural gas",
    ],
    "Crypto / Web3": [
        "bitcoin", "ethereum", "crypto", "blockchain", "stablecoin",
        "etf approval", "halving", "defi", "altcoin", "btc", "eth",
    ],
    "Robotics / Autonomous": [
        "robot", "humanoid", "autonomous", "optimus", "self-driving",
        "robotaxi", "fsd",
    ],
    "EV / Battery": [
        "ev", "electric vehicle", "battery", "solid state", "lfp",
        "charging", "megapack", "lithium",
    ],
    "Quantum": [
        "quantum", "qubit", "error correction", "superposition", "supremacy",
    ],
    "Space": [
        "satellite", "launch", "constellation", "rocket", "spacex", "starlink",
    ],
    "Drug / Biotech": [
        "glp-1", "ozempic", "wegovy", "oncology", "phase 3", "fda approval",
        "clinical trial", "biosimilar",
    ],
    "Fintech / Payments": [
        "fintech", "buy now pay later", "bnpl", "stripe", "payments",
        "neobank",
    ],
}

# 高熱度題材 (有重大 catalyst 的, 加分權重提高)
HIGH_HEAT_THEMES = {"AI / LLM", "Semiconductors / HBM", "Nuclear / Energy",
                    "Quantum", "Robotics / Autonomous", "Crypto / Web3"}


# 個股 → 主要板塊 ETF mapping (粗略, 用來查 sector rotation rank)
SYMBOL_TO_SECTOR_ETF = {
    # Tech
    **{s: "XLK" for s in [
        "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "ORCL", "CRM", "ADBE", "PLTR",
        "SMCI", "ARM", "MRVL", "QCOM", "TXN", "MU", "INTC", "PANW", "CRWD",
        "ZS", "NET", "DDOG", "MDB", "SNOW", "OKTA", "S", "FTNT", "ANET",
        "AMAT", "LRCX", "KLAC",
    ]},
    # Communication services
    **{s: "XLC" for s in [
        "GOOGL", "META", "NFLX", "DIS", "TMUS", "CMCSA", "RDDT",
    ]},
    # Consumer discretionary
    **{s: "XLY" for s in [
        "AMZN", "TSLA", "HD", "MCD", "NKE", "ABNB", "UBER", "SBUX", "BKNG",
        "RIVN", "LCID", "F", "GM",
    ]},
    # Financial
    **{s: "XLF" for s in [
        "JPM", "BAC", "V", "MA", "GS", "MS", "C", "BLK", "AXP", "WFC",
        "COIN", "HOOD", "SOFI",
    ]},
    # Energy
    **{s: "XLE" for s in [
        "XOM", "CVX", "COP", "EOG", "OXY", "PSX", "MPC", "SLB",
    ]},
    # Healthcare
    **{s: "XLV" for s in [
        "UNH", "LLY", "JNJ", "MRK", "ABBV", "TMO", "PFE", "ABT", "NVO",
        "REGN", "VRTX",
    ]},
    # Industrials
    **{s: "XLI" for s in [
        "GE", "BA", "CAT", "DE", "UNP", "RTX", "HON", "LMT", "NOC",
    ]},
    # Utilities (nuclear / power)
    **{s: "XLU" for s in [
        "NEE", "SO", "DUK", "VST", "CEG", "OKLO", "SMR",
    ]},
}


# ---------------------------------------------------------------------------
# 個別評分 component
# ---------------------------------------------------------------------------
def _detect_narratives_from_titles(titles_concat: str) -> List[str]:
    """從新聞標題串 concat 偵測命中哪些 narrative."""
    if not titles_concat:
        return []
    text = titles_concat.lower()
    found = []
    for theme, kws in THEME_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in text:
                found.append(theme)
                break  # 同 theme 命中一個關鍵字就算
    return found


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_news_for(symbol: str, max_n: int = 8) -> List[Dict]:
    """抓單檔股票最近的 yahoo finance news. Cache 30 分鐘.

    M4 修正: yfinance 0.2.40+ 改回 {"content": {title, provider, ...}, ...} 巢狀格式.
    同時支援新舊兩種格式以保持相容.
    """
    if not _YF_OK:
        return []
    try:
        t = yf.Ticker(symbol)
        items = t.news or []
    except Exception:
        items = []
    out = []
    for it in items[:max_n]:
        # M4: 同時相容平的舊格式 (it["title"]) 與新巢狀格式 (it["content"]["title"])
        content = it.get("content") if isinstance(it, dict) else None
        if isinstance(content, dict):
            # 新版巢狀格式 (yfinance >= 0.2.40)
            title = content.get("title") or it.get("title", "")
            provider_obj = content.get("provider") or {}
            publisher = (provider_obj.get("displayName") if isinstance(provider_obj, dict)
                          else str(provider_obj)) or it.get("publisher", "")
            # publish 時間欄位也改名了
            publish_time = (content.get("pubDate") or content.get("displayTime")
                              or it.get("providerPublishTime"))
            # 若是 ISO 字串轉成 unix timestamp
            if isinstance(publish_time, str):
                try:
                    publish_time = int(dt.datetime.fromisoformat(
                        publish_time.replace("Z", "+00:00")).timestamp())
                except Exception:
                    publish_time = None
            related = (content.get("relatedTickers") or it.get("relatedTickers") or [])
        else:
            # 舊版平格式
            title = it.get("title", "")
            publisher = it.get("publisher", "")
            publish_time = it.get("providerPublishTime")
            related = it.get("relatedTickers", [])
        if not title:
            continue  # 完全沒 title 的 entry skip
        out.append({
            "title": title,
            "publisher": publisher,
            "publish_time": publish_time,
            "related": related,
        })
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def _earnings_in_days(symbol: str) -> Optional[int]:
    """回傳到下次財報日的天數 (None = 抓不到 / 未來無排程).

    M1 修正: yfinance Ticker.calendar 在不同版本回 3 種格式:
      (a) dict: {"Earnings Date": [date, date], ...}  (舊版 0.1.x)
      (b) dict: {"earningsDate": date, ...}            (中間版)
      (c) DataFrame index=["Earnings Date", ...]       (新版 0.2.x)
      (d) None / 空                                      (該股無排程)
    全部都要處理. 失敗會 log (不再 silent), 方便排查.
    """
    if not _YF_OK:
        return None
    try:
        t = yf.Ticker(symbol)
        cal = t.calendar
    except Exception as e:
        # 抓不到本身就不算錯, 不 log
        return None
    if cal is None:
        return None

    next_earn = None
    parse_error = None
    try:
        # Case (a) / (b): dict
        if isinstance(cal, dict):
            for key in ("Earnings Date", "earningsDate", "earnings_date"):
                v = cal.get(key)
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    next_earn = v[0] if v else None
                else:
                    next_earn = v
                if next_earn is not None:
                    break
        # Case (c): DataFrame
        elif hasattr(cal, "loc"):
            for key in ("Earnings Date", "earnings_date"):
                try:
                    row = cal.loc[key]
                    # row 可能是 Series 或 scalar
                    if hasattr(row, "iloc"):
                        next_earn = row.iloc[0]
                    else:
                        next_earn = row
                    if next_earn is not None:
                        break
                except (KeyError, IndexError):
                    continue
        # Case: 其他未知型別
        else:
            parse_error = f"unknown calendar type: {type(cal).__name__}"
    except Exception as e:
        parse_error = f"parse error: {e}"

    if parse_error:
        print(f"[theme_analyzer] {symbol} earnings parse: {parse_error}", flush=True)
        return None
    if next_earn is None:
        return None

    # 轉成 date
    try:
        from pandas import Timestamp
        d = Timestamp(next_earn).date()
    except Exception as e:
        print(f"[theme_analyzer] {symbol} earnings date convert failed: {e}", flush=True)
        return None

    today = dt.date.today()
    delta = (d - today).days
    if delta < -30 or delta > 365:
        return None  # 不合理範圍 (可能 yfinance 回了舊資料)
    return delta


@st.cache_data(ttl=600, show_spinner=False)
def _sector_rotation_map() -> Dict[str, int]:
    """回傳 {sector_etf: rank by 1d_%}. rank 1 = 表現最好."""
    df = ds.fetch_sector_rotation()
    if df is None or df.empty:
        return {}
    if "1d_%" not in df.columns:
        return {}
    sorted_df = df.sort_values("1d_%", ascending=False).reset_index(drop=True)
    return {row["symbol"]: i + 1 for i, row in sorted_df.iterrows()}


# ---------------------------------------------------------------------------
# 主接口
# ---------------------------------------------------------------------------
def theme_score(symbol: str, sector_etf: Optional[str] = None) -> Dict:
    """單檔股票的題材 / 催化劑 / 板塊熱度綜合分數.

    Args:
        symbol: 美股 ticker
        sector_etf: 該股對應的板塊 ETF (e.g. "XLK"). None 時自動從 mapping 查.
    Returns:
        dict with total_score (0-100), narrative_tags, components, etc.
    """
    sym = symbol.upper()
    if sector_etf is None:
        sector_etf = SYMBOL_TO_SECTOR_ETF.get(sym)

    components = {}

    # === 1. 新聞題材熱度 (0-40) ===
    news = _fetch_news_for(sym, max_n=8)
    news_count = len(news)
    # 只看近 7 天新聞
    cutoff = time.time() - 7 * 86400
    recent_news = [n for n in news if (n.get("publish_time") or 0) >= cutoff]
    titles_concat = " ".join(n.get("title", "") for n in news)
    narratives = _detect_narratives_from_titles(titles_concat)

    narrative_score = 0
    for nm in narratives:
        narrative_score += 12 if nm in HIGH_HEAT_THEMES else 6
    narrative_score = min(narrative_score, 40)
    components["narrative_score"] = narrative_score

    # === 2. 板塊輪動排名 (0-25) ===
    sector_score = 0
    sector_rank = None
    if sector_etf:
        rmap = _sector_rotation_map()
        sector_rank = rmap.get(sector_etf)
        if sector_rank:
            # 11 個板塊: rank 1=25, 2=22, 3=19, ..., 11=0
            sector_score = max(0, 25 - (sector_rank - 1) * 2.5)
    components["sector_score"] = round(sector_score, 1)
    components["sector_etf"] = sector_etf
    components["sector_rank"] = sector_rank

    # === 3. 財報接近度 (0-15, 也可能為 0 或負加分) ===
    earn_days = _earnings_in_days(sym)
    earnings_score = 0
    if earn_days is not None:
        if 0 <= earn_days <= 7:
            earnings_score = 15  # 1 週內財報, 最高加分 (但同時是 binary risk!)
        elif 8 <= earn_days <= 14:
            earnings_score = 10
        elif 15 <= earn_days <= 30:
            earnings_score = 5
        elif -7 <= earn_days < 0:
            earnings_score = 8  # 剛公佈財報 (可能 gap up/down 仍在發酵)
    components["earnings_score"] = earnings_score
    components["earnings_in_days"] = earn_days

    # === 4. 個股新聞熱度 (0-20) ===
    news_heat_score = 0
    if news_count >= 6:
        news_heat_score = 20
    elif news_count >= 4:
        news_heat_score = 12
    elif news_count >= 2:
        news_heat_score = 6
    elif news_count >= 1:
        news_heat_score = 2
    components["news_heat_score"] = news_heat_score
    components["recent_news_7d"] = len(recent_news)

    total = narrative_score + sector_score + earnings_score + news_heat_score
    total = round(min(max(total, 0), 100), 1)

    if total >= 70:
        strength = "strong"
    elif total >= 45:
        strength = "moderate"
    elif total >= 20:
        strength = "weak"
    else:
        strength = "none"

    return {
        "symbol": sym,
        "total_score": total,
        "theme_strength": strength,
        "narrative_tags": list(dict.fromkeys(narratives)),  # dedup 保序
        "news_count": news_count,
        "earnings_in_days": earn_days,
        "sector_rotation_rank": sector_rank,
        "components": components,
        "top_news": [{"title": n["title"][:120],
                        "publisher": n.get("publisher", "")} for n in news[:3]],
    }


def batch_theme_scores(symbols: List[str], max_workers: int = 5) -> Dict[str, Dict]:
    """批次評分. 用 ThreadPoolExecutor 平行抓 yfinance news.
    Cache 內部各 API 都有, 重複呼叫不再打網路.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(theme_score, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = fut.result()
            except Exception:
                out[sym] = None
    return out


# ---------------------------------------------------------------------------
# 給上層 (us_upside_screener) 用的 score multiplier
# ---------------------------------------------------------------------------
def theme_multiplier(theme_result: Dict) -> float:
    """把題材總分 (0-100) 轉成 multiplier (0.8 - 1.5).
    用在最終 score = base_score * theme_multiplier.
    無題材 (0-20): 0.8 (打折, 純技術較弱)
    弱 (20-45):    1.0 (中性, 不加減)
    中等 (45-70):  1.2 (加成)
    強 (>70):      1.5 (顯著加成)
    """
    if not theme_result:
        return 1.0
    t = theme_result.get("total_score", 0)
    if t >= 70: return 1.5
    if t >= 45: return 1.2
    if t >= 20: return 1.0
    return 0.85  # 沒題材但有純技術 setup 的, 略打折但不消滅
