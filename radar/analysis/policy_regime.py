"""policy_regime.py — 政策情境族群觀察清單（2027 民主黨掌握國會 / 分裂政府）。

背景（2026-11 期中選舉初步結果）:
  民主黨拿下眾議院與參議院，2027-01 新國會就任 → 川普任期後半段為「分裂政府」。
  行政權（關稅、行政命令）仍在白宮；但新增減稅、去監管、預算刪減需要國會，難度大增；
  國會監督（聽證、調查）與預算 / 舉債上限攻防會變多。

這個模組做兩件事:
  1. POLICY_THEMES — 一份「假設清單」：每個族群寫明方向（受惠 / 受壓 / 觀察）、理由、
     美股代表股、台股供應鏈代表股、要盯的催化劑。
  2. scan_policy_themes() — 用實際股價驗證假設: 族群籃子近 5 / 20 日報酬 vs 大盤
     (美股對 SPY、台股對 0050)，標出「市場已反映 / 尚未反映 / 與假設相反」。

⚠️ 這是情境假設，不是投資建議。政策方向會隨法案、談判與法院判決改變，請搭配
   實際新聞與價格確認；scan 結果只描述「價格是否支持假設」，不預測未來。
"""
from __future__ import annotations

from typing import Dict, List, Optional

REGIME_NAME = "2027 民主黨國會 × 川普白宮（分裂政府）"
REGIME_START = "2027-01-03"  # 第 120 屆國會就任

# direction: "benefit" 受惠 / "pressure" 受壓 / "watch" 觀察（雙向）
POLICY_THEMES: List[Dict] = [
    {
        "key": "aca_managed_care",
        "name": "健保 / ACA・Medicaid 管理式醫療",
        "direction": "benefit",
        "thesis": "民主黨談判首要籌碼是延長 ACA 保費補貼、回補 Medicaid 刪減 → "
                  "以 Medicaid / ACA 為主的保險商與醫院受惠。",
        "us": ["CNC", "MOH", "OSCR", "ELV", "HCA", "THC"],
        "tw": [],
        "catalysts": ["ACA 補貼延長法案", "Medicaid 撥款", "預算 / 舉債上限談判"],
    },
    {
        "key": "clean_energy_grid",
        "name": "綠能 / 電網（IRA 抵免保留）",
        "direction": "benefit",
        "thesis": "國會不再可能進一步砍 IRA 抵免，抵免延長是民主黨的談判項目；"
                  "電網升級屬兩黨共識 → 太陽能、電網設備、重電受惠。",
        "us": ["FSLR", "ENPH", "NEE", "PWR", "ETN", "GEV"],
        "tw": ["1513.TW", "1519.TW", "1503.TW", "9958.TW"],
        "catalysts": ["IRA 抵免延長", "電網 / 輸電撥款", "各州再生能源標案"],
    },
    {
        "key": "rate_sensitive",
        "name": "利率敏感（公用事業 / REIT / 長債）",
        "direction": "benefit",
        "thesis": "分裂政府較難通過大規模財政刺激 → 赤字擴張預期降溫、殖利率下行，"
                  "選後美債已走強。",
        "us": ["XLU", "XLRE", "TLT", "IYR"],
        "tw": ["00679B.TWO", "00687B.TWO"],
        "catalysts": ["10 年期殖利率", "FOMC 點陣圖", "財政赤字預估"],
    },
    {
        "key": "staples_defensive",
        "name": "必需消費（防禦）",
        "direction": "benefit",
        "thesis": "政策僵局 + 成長放緩時資金偏好防禦；機構點名必需消費相對受惠。",
        "us": ["XLP", "PG", "KO", "WMT", "COST"],
        "tw": ["1216.TW", "2912.TW"],
        "catalysts": ["消費者信心", "關稅成本轉嫁"],
    },
    {
        "key": "mega_platforms",
        "name": "大型科技平台",
        "direction": "watch",
        "thesis": "選後大型平台走強（不確定性消除）；但民主黨國會的反壟斷 / 隱私聽證會增加，"
                  "屬「基本面強、監管雜音多」。",
        "us": ["AAPL", "MSFT", "GOOGL", "META", "AMZN"],
        "tw": ["2330.TW", "2317.TW", "3008.TW"],
        "catalysts": ["反壟斷聽證 / 訴訟", "AI 監管草案", "財報"],
    },
    {
        "key": "ai_datacenter",
        "name": "AI 資料中心 / 算力基建",
        "direction": "pressure",
        "thesis": "多州民主黨新任州長主張資料中心暫停 / 電價與用水審查 → 中小型 AI 基建股選後重挫；"
                  "大型雲端資本支出若延續，台灣伺服器鏈影響有限，需盯 capex 指引。",
        "us": ["CRWV", "NBIS", "VST", "CEG", "SMCI", "VRT"],
        "tw": ["2382.TW", "3231.TW", "6669.TW", "2308.TW"],
        "catalysts": ["州級資料中心法規", "電力併網審查", "雲端四巨頭 capex 指引"],
    },
    {
        "key": "defense",
        "name": "國防軍工",
        "direction": "pressure",
        "thesis": "國防預算增幅需國會撥款，民主黨可能卡住或改分配；選後 RTX 等走弱。",
        "us": ["LMT", "RTX", "NOC", "GD", "ITA"],
        "tw": ["2634.TW"],
        "catalysts": ["國防授權法 NDAA", "年度撥款 / 停擺風險"],
    },
    {
        "key": "financials_dereg",
        "name": "金融 / 去監管受惠股",
        "direction": "pressure",
        "thesis": "去監管立法動能消失、國會監督加強；殖利率下行壓縮淨利差 → 金融相對承壓。",
        "us": ["XLF", "JPM", "GS", "SCHW", "KRE"],
        "tw": [],
        "catalysts": ["資本適足（Basel III）規則", "銀行聽證", "殖利率曲線"],
    },
    {
        "key": "drug_pricing",
        "name": "藥價（Medicare 談判擴大）",
        "direction": "pressure",
        "thesis": "降藥價是少數兩黨都想做的事，民主黨國會可能推擴大 Medicare 議價範圍 → 大藥廠承壓。",
        "us": ["PFE", "MRK", "BMY", "ABBV", "XLV"],
        "tw": [],
        "catalysts": ["Medicare 議價名單", "PBM 改革法案"],
    },
    {
        "key": "fossil_energy",
        "name": "傳統油氣",
        "direction": "pressure",
        "thesis": "聯邦土地鑽探鬆綁、油氣補貼類立法停滯；能源價格仍是主要驅動，政策屬次要壓力。",
        "us": ["XOM", "CVX", "OXY", "XLE"],
        "tw": ["6505.TW"],
        "catalysts": ["油價", "聯邦租賃政策", "OPEC+"],
    },
    {
        "key": "tariff_exporters",
        "name": "關稅 / 台灣出口鏈",
        "direction": "watch",
        "thesis": "關稅主要是行政權，國會只能透過監督或立法限制；民主黨國會可能推動限縮總統關稅權 → "
                  "台灣出口鏈若關稅放緩是利多，但白宮仍可另起關稅，雙向風險。",
        "us": ["EWT", "SOXX"],
        "tw": ["2330.TW", "2454.TW", "2303.TW", "3711.TW"],
        "catalysts": ["國會限縮關稅權法案", "232 半導體關稅", "法院對 IEEPA 關稅判決"],
    },
]

DIRECTION_LABEL = {"benefit": "🟢 受惠", "pressure": "🔴 受壓", "watch": "🟡 觀察"}


def get_themes(direction: Optional[str] = None) -> List[Dict]:
    if direction is None:
        return list(POLICY_THEMES)
    return [t for t in POLICY_THEMES if t["direction"] == direction]


def _returns(symbol: str) -> Optional[Dict[str, float]]:
    """回傳 {"r5": %, "r20": %}; 抓不到資料回 None。"""
    from radar.data import data_sources as ds
    try:
        df = ds.fetch_yf_history(symbol, period="3mo", interval="1d")
    except Exception as e:
        print(f"[policy_regime] {symbol} 抓價失敗, 該檔不計入籃子: {e}", flush=True)
        return None
    if df is None or df.empty or "Close" not in df.columns:
        return None
    # 用還原股價: TLT / XLU / 00679B 月配息、KO / PG 季配, 除息跳空會被當成負報酬
    close = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
    if hasattr(close, "columns"):  # yfinance MultiIndex → 取第一欄
        close = close.iloc[:, 0]
    close = close.dropna()
    if len(close) < 21:
        return None
    last = float(close.iloc[-1])
    return {
        "r5": (last / float(close.iloc[-6]) - 1) * 100,
        "r20": (last / float(close.iloc[-21]) - 1) * 100,
    }


def _basket(symbols: List[str]) -> Optional[Dict]:
    rets = {s: r for s in symbols if (r := _returns(s)) is not None}
    if not rets:
        return None
    n = len(rets)
    return {
        "r5": sum(r["r5"] for r in rets.values()) / n,
        "r20": sum(r["r20"] for r in rets.values()) / n,
        "n": n,
        "members": rets,
    }


def _verdict(direction: str, rel20: float) -> str:
    """用 20 日相對大盤的超額報酬判斷價格是否支持假設 (±2% 以內視為中性)。"""
    if direction == "watch":
        return "雙向觀察"
    sign = 1 if direction == "benefit" else -1
    edge = rel20 * sign
    if edge >= 2:
        return "市場已反映"
    if edge <= -2:
        return "與假設相反"
    return "尚未反映"


def scan_policy_themes(include_tw: bool = True) -> List[Dict]:
    """逐族群計算籃子報酬與相對大盤，回傳 list (依方向、超額報酬排序)。

    每列: key, name, direction, thesis, catalysts,
          us: {r5, r20, rel5, rel20, n} | None, tw: 同 | None, verdict
    """
    # 基準抓不到時不能當 0% —— 那會把原始報酬當成「超額報酬」, 多頭月全部顯示「已反映」
    bench_us = _returns("SPY")
    bench_tw = _returns("0050.TW") if include_tw else None

    rows = []
    for t in POLICY_THEMES:
        row = {k: t[k] for k in ("key", "name", "direction", "thesis", "catalysts")}
        row["us_symbols"], row["tw_symbols"] = t["us"], t["tw"]
        us = _basket(t["us"]) if t["us"] else None
        if us:
            us["rel5"] = us["r5"] - bench_us["r5"] if bench_us else None
            us["rel20"] = us["r20"] - bench_us["r20"] if bench_us else None
        tw = _basket(t["tw"]) if (include_tw and t["tw"]) else None
        if tw:
            tw["rel5"] = tw["r5"] - bench_tw["r5"] if bench_tw else None
            tw["rel20"] = tw["r20"] - bench_tw["r20"] if bench_tw else None
        row["us"], row["tw"] = us, tw
        ref = next((x for x in (us, tw) if x and x.get("rel20") is not None), None)
        if ref:
            row["verdict"] = _verdict(t["direction"], ref["rel20"])
        else:
            row["verdict"] = "無基準" if (us or tw) else "無資料"
        rows.append(row)

    order = {"benefit": 0, "watch": 1, "pressure": 2}
    def _rel(r):
        for x in (r["us"], r["tw"]):
            if x and x.get("rel20") is not None:
                return x["rel20"]
        return 0.0
    rows.sort(key=lambda r: (order[r["direction"]], -_rel(r)))
    return rows


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def fmt_policy_regime_tg(rows: List[Dict]) -> str:
    """Telegram HTML 訊息。"""
    import html
    lines = [f"🏛️ <b>政策情境族群</b>｜{html.escape(REGIME_NAME)}",
             "<i>20 日相對大盤（美股 vs SPY、台股 vs 0050）</i>", ""]
    for r in rows:
        us, tw = r.get("us"), r.get("tw")
        parts = []
        if us:
            parts.append(f"美 {_pct(us['rel20'])}")
        if tw:
            parts.append(f"台 {_pct(tw['rel20'])}")
        lines.append(f"{DIRECTION_LABEL[r['direction']]} <b>{html.escape(r['name'])}</b> "
                     f"{' / '.join(parts) or '無資料'}｜{r['verdict']}")
    lines += ["", "⚠️ 情境假設，非投資建議；價格只說明市場是否已反映。"]
    return "\n".join(lines)
