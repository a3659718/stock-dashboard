"""
rate_cycle_advisor.py
利率週期 (Fed cycle) 偵測 + 族群建議.

偵測邏輯:
  - 用短期 (SHY) vs 長期 (TLT) 30d 走勢推 cycle
  - SHY 跌 + TLT 跌 → 升息週期 (hike) — 全 bond 跌 (殖利率同步上升)
  - SHY 漲 + TLT 漲 → 降息週期 (cut) — 全 bond 漲 (殖利率同步下降)
  - 混合 / 不夠明確 → 暫停期 (pause)

  (2026-08 修正: 原本直接比較兩檔 ETF 的「價格漲跌 %」, 門檻是 SHY ±0.5% / TLT ±1.0%
  (只差 2 倍)。但 SHY(1-3 年短債)存續期間約 1.9 年, TLT(20 年以上長債)約 17 年,
  對同一段殖利率變動, 價格敏感度差距是 ~9 倍, 不是 2 倍 — 換算下來舊門檻等於要求
  SHY 隱含殖利率變動達 ~26bps 才觸發, TLT 卻只要 ~6bps 就觸發, 導致 AND 條件實際上
  幾乎完全被 SHY 那關卡住, TLT 的門檻形同虛設。現在改成先用各自的 duration 概算
  反推「隱含殖利率變動 (bps)」, 兩檔 ETF 在同一把尺 (bps) 上比較, 才是真正對稱的
  「短天期 + 長天期是否同步反映同一個 Fed 週期」判斷。duration 用市場公開的概略值,
  非即時抓取, 之後 ETF 持債組成變化的話可能需要重新校正這兩個常數。)

族群建議來自學術 + Fed cycle 經典 playbook.

API:
  detect_cycle() -> Dict
  get_sector_advice(cycle) -> Dict
"""
from __future__ import annotations

from typing import Dict, List


CYCLE_LABELS = {
    "cut":   ("🟢", "降息週期", "寬鬆"),
    "hike":  ("🔴", "升息週期", "緊縮"),
    "pause": ("🟡", "暫停期",   "等待 Fed"),
    "unknown": ("⚪", "未知", "資料不足"),
}


# 族群 playbook (Fed cycle classic)
PLAYBOOK = {
    "cut": {
        "outperform": [
            ("QQQ / NVDA / AAPL", "成長股", "降息 → 折現率降, 高 PE 重估"),
            ("IWM", "小型股", "降息 → 融資成本降, 小公司獲利改善"),
            ("XLRE", "REITs", "降息 → 房貸成本降, REIT 估值上"),
            ("TLT", "長債 ETF", "利率反向, 直接受惠"),
            ("GLD / IAU", "黃金", "降息 → 美元弱, 黃金漲"),
            ("EEM", "新興市場", "美元弱 → 新興市場資金流入"),
        ],
        "avoid": [
            ("XLF / JPM", "銀行", "降息 → 利差縮, margin 壓力"),
            ("現金/Money Market", "持現金成本上升"),
        ],
        "tw_outperform": [
            ("2330 / 6669", "科技/AI 半導體"),
            ("3293", "鈊象 (高成長)"),
            ("00929 / 00940", "高股息 ETF (利率敏感資產)"),
        ],
        "tw_avoid": [
            ("2880-2891", "金融股 (利差壓力)"),
        ],
    },
    "hike": {
        "outperform": [
            ("XLF / JPM / BAC", "銀行", "升息 → 利差擴, 淨利息收入增"),
            ("XLE / XOM", "能源", "升息常伴通膨, 能源價格 supported"),
            ("XLU / XLP", "公用/必需消費", "防禦類, 升息抗跌"),
            ("SHY", "短債 ETF", "短期利率上, SHY 高 yield"),
            ("Value 股 (BRK.B)", "Value 跑贏 Growth"),
        ],
        "avoid": [
            ("NVDA / TSLA / 高 PE growth", "估值壓縮", "DCF 折現率上, 高 PE 重估下"),
            ("TLT / 長債", "利率反向, 直接受傷"),
            ("XLRE", "REIT 估值壓縮"),
            ("EEM", "美元強 → 新興市場資金外流"),
        ],
        "tw_outperform": [
            ("2880-2891", "金融股 (利差受惠)"),
            ("1101-1303", "傳產 (價值修復)"),
            ("2603-2618", "航運 (通膨 hedge)"),
        ],
        "tw_avoid": [
            ("2330 高 PE 部分", "估值壓縮"),
            ("成長股 (6669 等)", "DCF 重估"),
        ],
    },
    "pause": {
        "outperform": [
            ("S&P 500 (SPY)", "整體大盤 + sector rotation"),
            ("XLV (Healthcare)", "防禦+成長 兼具"),
            ("Quality factor", "高 ROE + 低 Debt 公司"),
        ],
        "avoid": [
            ("極端波動標的", "等待 Fed 訊號明確"),
        ],
        "tw_outperform": [
            ("0050 / 0056", "ETF 分散風險"),
            ("龍頭股", "2330 / 2317 等品質股"),
        ],
        "tw_avoid": [
            ("高 beta 投機股", "未明趨勢"),
        ],
    },
}


def _fetch_30d_change(symbol: str) -> float | None:
    try:
        import data_sources as ds
        df = ds.fetch_yf_history(symbol, period="45d", interval="1d")
        if df is None or df.empty or len(df) < 22:
            return None
        c = df["Close"].astype(float)
        return round((float(c.iloc[-1]) / float(c.iloc[-22]) - 1) * 100, 2)
    except Exception:
        return None


# 概略 effective duration (年) — 市場公開估計值, 非即時抓取。
# SHY = iShares 1-3 Year Treasury Bond ETF, TLT = iShares 20+ Year Treasury Bond ETF.
SHY_DURATION_YR = 1.9
TLT_DURATION_YR = 17.0

# 隱含殖利率變動門檻 (bps) — 兩檔 ETF 換算成同一把尺後共用同一個門檻。
# 30 天內短天期 + 長天期同步位移 15bps 以上, 才視為有意義的 Fed cycle 訊號
# (避免單一天期雜訊 / 曲線非平行移動時誤判)。
YIELD_BPS_THRESHOLD = 15.0


def _implied_yield_change_bps(pct_change: float, duration_yr: float) -> float:
    """用 duration 概略反推「隱含殖利率變動」(bps).

    債券價格 ≈ -duration × Δyield (Δyield 以小數表示), 所以
    Δyield(bps) ≈ -(價格漲跌% / duration) × 100。
    正值 = 殖利率上升 (bond 價格跌); 負值 = 殖利率下降 (bond 價格漲)。
    這只是概略估算 (忽略 convexity / duration 隨時間變化), 用來讓 SHY 和 TLT
    在「同一把尺」上比較, 而不是直接比價格漲跌 % (兩者 duration 差 ~9 倍,
    直接比 % 沒有意義)。
    """
    return -(pct_change / duration_yr) * 100


def detect_cycle() -> Dict:
    """根據 SHY / TLT 30d 推 cycle. 回 {cycle, label, evidence, shy_yield_bps, tlt_yield_bps}."""
    out = {
        "cycle": "unknown", "shy_30d": None, "tlt_30d": None,
        "shy_yield_bps": None, "tlt_yield_bps": None, "evidence": "",
    }
    shy = _fetch_30d_change("SHY")
    tlt = _fetch_30d_change("TLT")
    out["shy_30d"] = shy
    out["tlt_30d"] = tlt
    if shy is None or tlt is None:
        return out

    shy_bps = _implied_yield_change_bps(shy, SHY_DURATION_YR)
    tlt_bps = _implied_yield_change_bps(tlt, TLT_DURATION_YR)
    out["shy_yield_bps"] = round(shy_bps, 1)
    out["tlt_yield_bps"] = round(tlt_bps, 1)

    detail = (
        f"SHY {shy:+.2f}%(≈{shy_bps:+.0f}bps) + TLT {tlt:+.2f}%(≈{tlt_bps:+.0f}bps)"
    )
    if shy_bps >= YIELD_BPS_THRESHOLD and tlt_bps >= YIELD_BPS_THRESHOLD:
        # 短 + 長天期殖利率同步上升 → 升息預期
        out["cycle"] = "hike"
        out["evidence"] = f"{detail} 殖利率同步上升 → 升息預期"
    elif shy_bps <= -YIELD_BPS_THRESHOLD and tlt_bps <= -YIELD_BPS_THRESHOLD:
        # 短 + 長天期殖利率同步下降 → 降息預期
        out["cycle"] = "cut"
        out["evidence"] = f"{detail} 殖利率同步下降 → 降息預期"
    else:
        out["cycle"] = "pause"
        out["evidence"] = f"{detail} 未同步明確位移 → 暫停期 / 等待 Fed"
    return out


def get_sector_advice(cycle: str) -> Dict:
    """回該 cycle 的族群建議 + emoji label."""
    if cycle not in PLAYBOOK:
        cycle = "pause"
    emoji, label, regime = CYCLE_LABELS.get(cycle, CYCLE_LABELS["unknown"])
    return {
        "cycle": cycle,
        "emoji": emoji,
        "label": label,
        "regime": regime,
        "outperform": PLAYBOOK[cycle].get("outperform", []),
        "avoid": PLAYBOOK[cycle].get("avoid", []),
        "tw_outperform": PLAYBOOK[cycle].get("tw_outperform", []),
        "tw_avoid": PLAYBOOK[cycle].get("tw_avoid", []),
    }



def analyze_outperform_with_gemini(cycle: str, advice: dict) -> str:
    """用 Gemini 解讀「看好族群為什麼受惠」, 簡化 avoid 部分."""
    if not advice:
        return ""
    try:
        import ai_analyzer
        if not ai_analyzer.gemini_available():
            return ""
        out = advice.get("outperform") or []
        if not out:
            return ""
        ctx_lines = [f"Fed cycle 目前判定: {advice.get('label')}",
                     f"看好族群 (請 2 句中文白話解讀「為什麼這 cycle 受惠」+「進場時機」):"]
        for item in out[:4]:
            if len(item) >= 2:
                ctx_lines.append(f"- {item[0]} ({item[1]})")
        prompt = "\n".join(ctx_lines) + "\n\n聚焦結論, 不要列數據."
        from ai_analyzer import _get_model
        model = _get_model()
        if model is None: return ""
        resp = model.generate_content(prompt)
        return (resp.text or "").strip() if resp else ""
    except Exception as e:
        print(f"[rate_cycle] gemini fail: {e}", flush=True)
        return ""
