"""
theme_analyzer.py
Theme / catalyst / sector heat scoring — shared by us_upside_screener / actionable_picks.

Score composition (0-100):
  - Stock narrative heat (news + keyword): 0-50 (incl. multi-narrative bonus)
  - Sector rotation rank (XLK/XLE/...): 0-25
  - Earnings proximity (within 7 days = boost): 0-15
  - Stock news heat (recent count): 0-20

API:
    theme_score(symbol, sector_etf=None) -> dict
    batch_theme_scores(symbols) -> dict
    theme_multiplier(theme_result) -> float
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

# Streamlit cache (no-op fallback for non-streamlit context)
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


# --- Theme keywords (2024-2026 leading narratives) ---
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

# High-heat themes (catalysts that historically drive bigger moves)
HIGH_HEAT_THEMES = {"AI / LLM", "Semiconductors / HBM", "Nuclear / Energy",
                    "Quantum", "Robotics / Autonomous"}


# Symbol -> sector ETF mapping (aligned with us_upside_screener DEFAULT_US_UNIVERSE)
SYMBOL_TO_SECTOR_ETF = {
    **{s: "XLK" for s in [
        "AAPL", "MSFT", "NVDA", "AVGO", "AMD", "ORCL", "CRM", "ADBE", "PLTR",
        "SMCI", "ARM", "MRVL", "QCOM", "TXN", "MU", "INTC", "PANW", "CRWD",
        "ZS", "NET", "DDOG", "MDB", "SNOW", "OKTA", "S", "FTNT", "ANET",
        "AMAT", "LRCX", "KLAC", "MCHP", "SWKS", "QRVO", "AMBA", "RMBS", "ON",
        "ALAB", "ASML",
        "IONQ", "RGTI", "QBTS", "SOUN", "BBAI", "AI", "TEM", "OUST", "CRWV",
        "U", "TWLO",
    ]},
    **{s: "XLC" for s in [
        "GOOGL", "META", "NFLX", "DIS", "TMUS", "CMCSA", "RDDT", "PINS",
    ]},
    **{s: "XLY" for s in [
        "AMZN", "TSLA", "HD", "MCD", "NKE", "ABNB", "UBER", "SBUX", "BKNG",
        "RIVN", "LCID", "F", "GM", "SHOP", "MELI", "CART", "DKNG", "BIRK",
        "DUOL", "TOST", "BROS", "RBLX", "GRAB",
    ]},
    **{s: "XLF" for s in [
        "JPM", "BAC", "V", "MA", "GS", "MS", "C", "BLK", "AXP", "WFC", "BRK-B",
        "HOOD", "SOFI", "AFRM", "PYPL", "SQ",
        "MARA", "RIOT", "HUT",
    ]},
    **{s: "XLE" for s in [
        "XOM", "CVX", "COP", "EOG", "OXY", "PSX", "MPC", "SLB",
    ]},
    **{s: "XLV" for s in [
        "UNH", "LLY", "JNJ", "MRK", "ABBV", "TMO", "PFE", "ABT", "NVO",
        "REGN", "VRTX",
    ]},
    # XLI includes Space stocks (RKLB / ASTS) — RKLB 2024 was this category
    **{s: "XLI" for s in [
        "GE", "BA", "CAT", "DE", "UNP", "RTX", "HON", "LMT", "NOC",
        "RKLB", "ASTS",
        "CPRT",
    ]},
    **{s: "XLU" for s in [
        "NEE", "SO", "DUK", "VST", "CEG", "OKLO", "SMR",
    ]},
    **{s: "XLP" for s in [
        "WMT", "COST", "PG", "KO", "PEP", "MO", "PM",
    ]},
}


def _detect_narratives_from_titles(titles_concat: str) -> List[str]:
    """Detect which narratives appear in concatenated news titles."""
    if not titles_concat:
        return []
    text = titles_concat.lower()
    found = []
    for theme, kws in THEME_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in text:
                found.append(theme)
                break
    return found


@st.cache_data(ttl=1800, show_spinner=False)
def _fetch_news_for(symbol: str, max_n: int = 8) -> List[Dict]:
    """Fetch recent Yahoo Finance news for a symbol. Cache 30min.

    Supports both old flat format (it['title']) and new nested
    format (it['content']['title']) from yfinance 0.2.40+.
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
        content = it.get("content") if isinstance(it, dict) else None
        if isinstance(content, dict):
            title = content.get("title") or it.get("title", "")
            provider_obj = content.get("provider") or {}
            publisher = (provider_obj.get("displayName") if isinstance(provider_obj, dict)
                          else str(provider_obj)) or it.get("publisher", "")
            publish_time = (content.get("pubDate") or content.get("displayTime")
                              or it.get("providerPublishTime"))
            if isinstance(publish_time, str):
                try:
                    publish_time = int(dt.datetime.fromisoformat(
                        publish_time.replace("Z", "+00:00")).timestamp())
                except Exception:
                    publish_time = None
            related = (content.get("relatedTickers") or it.get("relatedTickers") or [])
        else:
            title = it.get("title", "")
            publisher = it.get("publisher", "")
            publish_time = it.get("providerPublishTime")
            related = it.get("relatedTickers", [])
        if not title:
            continue
        out.append({
            "title": title,
            "publisher": publisher,
            "publish_time": publish_time,
            "related": related,
        })
    return out


@st.cache_data(ttl=3600, show_spinner=False)
def _earnings_in_days(symbol: str) -> Optional[int]:
    """Days until next earnings. None if unavailable.

    Handles 3 yfinance calendar formats:
      (a) dict: {"Earnings Date": [date, ...]}      (old)
      (b) dict: {"earningsDate": date}              (mid)
      (c) DataFrame index=["Earnings Date", ...]     (new 0.2.x)
    """
    if not _YF_OK:
        return None
    try:
        t = yf.Ticker(symbol)
        cal = t.calendar
    except Exception:
        return None
    if cal is None:
        return None

    next_earn = None
    parse_error = None
    try:
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
        elif hasattr(cal, "loc"):
            for key in ("Earnings Date", "earnings_date"):
                try:
                    row = cal.loc[key]
                    if hasattr(row, "iloc"):
                        next_earn = row.iloc[0]
                    else:
                        next_earn = row
                    if next_earn is not None:
                        break
                except (KeyError, IndexError):
                    continue
        else:
            parse_error = "unknown calendar type: " + type(cal).__name__
    except Exception as e:
        parse_error = "parse error: " + str(e)

    if parse_error:
        print("[theme_analyzer] " + symbol + " earnings parse: " + parse_error, flush=True)
        return None
    if next_earn is None:
        return None

    try:
        from pandas import Timestamp
        d = Timestamp(next_earn).date()
    except Exception as e:
        print("[theme_analyzer] " + symbol + " earnings convert failed: " + str(e), flush=True)
        return None

    today = dt.date.today()
    delta = (d - today).days
    if delta < -30 or delta > 365:
        return None
    return delta


@st.cache_data(ttl=600, show_spinner=False)
def _sector_rotation_map() -> Dict[str, int]:
    """Return {sector_etf: rank by 1d_%}. rank 1 = best."""
    df = ds.fetch_sector_rotation()
    if df is None or df.empty:
        return {}
    if "1d_%" not in df.columns:
        return {}
    sorted_df = df.sort_values("1d_%", ascending=False).reset_index(drop=True)
    return {row["symbol"]: i + 1 for i, row in sorted_df.iterrows()}


def theme_score(symbol: str, sector_etf: Optional[str] = None) -> Dict:
    """Composite theme / catalyst / sector heat score for a single symbol.

    Returns dict with total_score (0-100), narrative_tags, components, etc.
    """
    sym = symbol.upper()
    if sector_etf is None:
        sector_etf = SYMBOL_TO_SECTOR_ETF.get(sym)

    components = {}

    # 1. News narrative heat (0-50, incl. multi-narrative bonus)
    news = _fetch_news_for(sym, max_n=8)
    news_count = len(news)
    cutoff = time.time() - 7 * 86400
    recent_news = [n for n in news if (n.get("publish_time") or 0) >= cutoff]
    titles_concat = " ".join(n.get("title", "") for n in news)
    narratives = _detect_narratives_from_titles(titles_concat)

    narrative_score = 0
    for nm in narratives:
        narrative_score += 12 if nm in HIGH_HEAT_THEMES else 6
    # Multi-narrative convergence bonus: stocks hitting 3+ narratives
    # often have cross-thematic catalysts (e.g., RKLB = Space + Defense + AI)
    n_distinct = len(set(narratives))
    if n_distinct >= 4:
        narrative_score += 10
    elif n_distinct >= 3:
        narrative_score += 5
    narrative_score = min(narrative_score, 50)
    components["narrative_score"] = narrative_score
    components["distinct_narratives"] = n_distinct

    # 2. Sector rotation rank (0-25)
    sector_score = 0
    sector_rank = None
    if sector_etf:
        rmap = _sector_rotation_map()
        sector_rank = rmap.get(sector_etf)
        if sector_rank:
            sector_score = max(0, 25 - (sector_rank - 1) * 2.5)
    components["sector_score"] = round(sector_score, 1)
    components["sector_etf"] = sector_etf
    components["sector_rank"] = sector_rank

    # 3. Earnings proximity (0-15)
    earn_days = _earnings_in_days(sym)
    earnings_score = 0
    if earn_days is not None:
        if 0 <= earn_days <= 7:
            earnings_score = 15
        elif 8 <= earn_days <= 14:
            earnings_score = 10
        elif 15 <= earn_days <= 30:
            earnings_score = 5
        elif -7 <= earn_days < 0:
            earnings_score = 8
    components["earnings_score"] = earnings_score
    components["earnings_in_days"] = earn_days

    # 4. Stock news heat (0-20)
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
        "narrative_tags": list(dict.fromkeys(narratives)),
        "news_count": news_count,
        "earnings_in_days": earn_days,
        "sector_rotation_rank": sector_rank,
        "components": components,
        "top_news": [{"title": n["title"][:120],
                        "publisher": n.get("publisher", "")} for n in news[:3]],
    }


def batch_theme_scores(symbols: List[str], max_workers: int = 5) -> Dict[str, Dict]:
    """Batch score with ThreadPoolExecutor. Cache hits keep this cheap."""
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


def theme_multiplier(theme_result: Dict) -> float:
    """Convert theme total_score (0-100) into multiplier (0.85 - 1.5)
    for upstream score = base_score * theme_multiplier.

    No theme (0-20): 0.85
    Weak (20-45):    1.0
    Moderate (45-70): 1.2
    Strong (>=70):   1.5
    """
    if not theme_result:
        return 1.0
    t = theme_result.get("total_score", 0)
    if t >= 70:
        return 1.5
    if t >= 45:
        return 1.2
    if t >= 20:
        return 1.0
    return 0.85
