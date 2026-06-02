"""
portfolio_risk.py
組合風險分析 — sector 集中度 / 個股權重 / 估計 beta.

API:
  analyze_portfolio_risk() -> Dict
    回 {
        "holdings_n": int,
        "total_value": float,
        "sectors": [{"sector", "weight_pct", "stocks"}, ...],
        "max_sector": str,
        "max_sector_pct": float,
        "concentration_warnings": [str, ...],
        "stocks": [{"stock_id", "weight_pct", "sector"}, ...],
        "warnings": [str, ...],   # 集中度/單檔過大等
    }
"""
from __future__ import annotations

from typing import Dict, List


# 集中度警告門檻
SECTOR_CONCENTRATION_WARN_PCT = 50.0  # 單一 sector ≥ 50% → 警告
SECTOR_CONCENTRATION_CRIT_PCT = 70.0  # ≥ 70% → 嚴重警告
SINGLE_STOCK_WARN_PCT = 25.0          # 單檔 ≥ 25% → 警告
SINGLE_STOCK_CRIT_PCT = 40.0          # ≥ 40% → 嚴重


# 相關性映射 (簡化 — 同 sector 視為高相關)
US_SECTOR_TO_PROXY = {
    "Technology": "XLK",
    "Semiconductors": "XLK",  # 半導體列科技
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
}

TW_INDUSTRY_TO_FAMILY = {
    # 全部歸到 family 讓集中度判斷簡單
    "半導體業": "Tech/Semi",
    "電子零組件業": "Tech/Semi",
    "電腦及週邊設備業": "Tech/Semi",
    "其他電子業": "Tech/Semi",
    "通信網路業": "Tech/Semi",
    "光電業": "Tech/Semi",
    "電子通路業": "Tech/Semi",
    "資訊服務業": "Tech/Semi",
    "金融保險業": "Financial",
    "鋼鐵工業": "Steel/Material",
    "塑膠工業": "Material",
    "化學工業": "Material",
    "航運業": "Shipping",
    "電機機械": "Industrial",
    "建材營造業": "Construction",
    "食品工業": "Consumer",
    "紡織纖維": "Consumer",
    "貿易百貨": "Consumer",
    "油電燃氣業": "Utility",
    "玻璃陶瓷": "Material",
}


def _classify_family(industry: str, market: str) -> str:
    """同類股 family — 用簡化 mapping. 同 family 視為高相關."""
    if not industry:
        return "Unknown"
    if market == "TW":
        return TW_INDUSTRY_TO_FAMILY.get(industry, "Other")
    # US: 直接用 sector (粗類)
    if "Technology" in industry or "Semi" in industry:
        return "Tech/Semi"
    if "Financial" in industry:
        return "Financial"
    if "Healthcare" in industry or "Health" in industry:
        return "Healthcare"
    if "Energy" in industry or "Oil" in industry:
        return "Energy"
    if "Communication" in industry:
        return "Communication"
    return industry or "Other"


def _fetch_sector_for_stock(stock_id: str, market: str) -> str:
    """抓單檔 sector / industry."""
    try:
        if market == "TW":
            import data_sources as ds
            info = ds.get_taiwan_stock_info()
            if info is not None and not info.empty:
                row = info[info["stock_id"] == stock_id]
                if not row.empty:
                    for col in ["industry_category", "industry", "type"]:
                        if col in row.columns:
                            v = row.iloc[0].get(col)
                            if v: return str(v)
        else:
            import fundamentals_us as fu
            fund = fu.fetch_us_fundamentals(stock_id)
            sec = fund.get("sector") or fund.get("industry")
            if sec: return str(sec)
    except Exception:
        pass
    return "Unknown"


def analyze_portfolio_risk() -> Dict:
    """整合 holdings → 算 sector 集中度 + 單檔權重 + 警告."""
    try:
        import holdings_store
        holdings = holdings_store.load_holdings() or []
    except Exception as e:
        return {"err": f"load_holdings failed: {e}", "holdings_n": 0}

    if not holdings:
        return {"holdings_n": 0, "warnings": ["📭 尚未設定持倉"], "sectors": [], "stocks": []}

    # HIGH bug fix: 抓最新價 + shares 沒設用「假設 10 萬投入」當 proxy
    DEFAULT_BUDGET_PER_STOCK = 100000  # 沒 shares 假設每檔投 10 萬
    enriched = []
    for h in holdings:
        sid = str(h.get("stock_id", "")).strip().upper()
        if not sid:
            continue
        mk = h.get("market", "TW" if sid.isdigit() else "US")
        # 抓最新價
        cur_price = None
        try:
            import data_sources as ds
            ticker_main = f"{sid}.TW" if mk == "TW" else sid
            df_ = ds.fetch_yf_history(ticker_main, period="5d", interval="1d")
            if (df_ is None or df_.empty) and mk == "TW":
                df_ = ds.fetch_yf_history(f"{sid}.TWO", period="5d", interval="1d")
            if df_ is not None and not df_.empty:
                cur_price = float(df_["Close"].iloc[-1])
        except Exception:
            pass
        if cur_price is None or cur_price <= 0:
            try:
                cur_price = float(h.get("entry_price") or 0)
            except (TypeError, ValueError):
                cur_price = 0
        # shares fallback (假設投入 10 萬 / 最新價)
        shares = h.get("shares") or h.get("lots")
        try:
            shares = float(shares) if shares else None
        except (TypeError, ValueError):
            shares = None
        if shares is None or shares <= 0:
            if cur_price and cur_price > 0:
                shares = DEFAULT_BUDGET_PER_STOCK / cur_price
            else:
                shares = 1.0
        value = (cur_price if cur_price else 100) * shares
        sector = _fetch_sector_for_stock(sid, mk)
        family = _classify_family(sector, mk)
        # MED bug fix: ETF (00xx) 歸 "ETF" family
        if mk == "TW" and sid.startswith("00") and (family in ("Other", "Unknown")):
            family = "ETF"
        enriched.append({
            "stock_id": sid,
            "market": mk,
            "value": value,
            "sector": sector,
            "family": family,
        })

    total_value = sum(e["value"] for e in enriched) or 1.0

    # Sector / family 集中度
    family_map: Dict[str, Dict] = {}
    for e in enriched:
        fam = e["family"]
        family_map.setdefault(fam, {"value": 0, "stocks": []})
        family_map[fam]["value"] += e["value"]
        family_map[fam]["stocks"].append(e["stock_id"])

    sectors = sorted(
        [
            {
                "sector": fam,
                "value": v["value"],
                "weight_pct": round(v["value"] / total_value * 100, 2),
                "stocks": v["stocks"],
                "n": len(v["stocks"]),
            }
            for fam, v in family_map.items()
        ],
        key=lambda x: x["weight_pct"], reverse=True,
    )

    max_sector = sectors[0]["sector"] if sectors else "—"
    max_sector_pct = sectors[0]["weight_pct"] if sectors else 0
    max_sector_n = sectors[0]["n"] if sectors else 0

    # 單檔權重
    stocks_w = sorted(
        [
            {
                "stock_id": e["stock_id"],
                "weight_pct": round(e["value"] / total_value * 100, 2),
                "sector": e["sector"],
                "family": e["family"],
            }
            for e in enriched
        ],
        key=lambda x: x["weight_pct"], reverse=True,
    )

    # 警告
    warnings = []
    if max_sector_pct >= SECTOR_CONCENTRATION_CRIT_PCT:
        warnings.append(
            f"🔴 嚴重: <b>{max_sector}</b> 集中度 {max_sector_pct:.0f}% "
            f"({max_sector_n} 檔), 同族群暴跌 1 起重創"
        )
    elif max_sector_pct >= SECTOR_CONCENTRATION_WARN_PCT:
        warnings.append(
            f"🟡 注意: <b>{max_sector}</b> 集中度 {max_sector_pct:.0f}% "
            f"({max_sector_n} 檔), 建議分散 ≤ 50%"
        )

    if stocks_w and stocks_w[0]["weight_pct"] >= SINGLE_STOCK_CRIT_PCT:
        warnings.append(
            f"🔴 單檔過大: <b>{stocks_w[0]['stock_id']}</b> "
            f"佔 {stocks_w[0]['weight_pct']:.0f}%, 建議減碼 ≤ 40%"
        )
    elif stocks_w and stocks_w[0]["weight_pct"] >= SINGLE_STOCK_WARN_PCT:
        warnings.append(
            f"🟡 單檔較重: <b>{stocks_w[0]['stock_id']}</b> "
            f"佔 {stocks_w[0]['weight_pct']:.0f}%, 留意 ≤ 25%"
        )

    if not warnings:
        warnings.append("🟢 組合分散度良好")

    return {
        "holdings_n": len(enriched),
        "total_value": round(total_value, 2),
        "sectors": sectors,
        "max_sector": max_sector,
        "max_sector_pct": max_sector_pct,
        "warnings": warnings,
    }
