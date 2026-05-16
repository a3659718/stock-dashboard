"""
crypto_picker.py
偵測「當前有大幅度上漲趨勢」的加密貨幣, 用 Gemini 挑 5 檔給進場建議.

評分維度:
  1. today%       — 當日漲幅 (>3% 強勢)
  2. 7d%          — 一週累積漲幅 (>10% 確立趨勢)
  3. 30d%         — 一個月漲幅 (>20% 中期 momentum, 但 >80% 警示過熱)
  4. 量比         — 當日成交量 / 7d 均量, >1.5 表示有資金進場
  5. RSI(14)      — 50~70 健康, >75 過熱, <30 超賣
  6. distance_ma  — 距 20MA % (1~15% 健康上升, >25% 追高警告)

最終分數: 0~10, weight = today (2) + 7d (3) + 量比 (2) + 30d (1) + rsi 區間 (2)

Gemini 分析:
  取前 10 名喂進去, 讓它挑 5 個「相對最該進場」(避免全選最熱的, 因為可能已過熱)
  return 給每檔: entry/target/stop/win_prob/reason

對外接口:
  get_crypto_picks(top_n=5) -> Dict
    {
      "picks": [{"symbol", "current", "entry_low", "entry_high",
                  "target", "stop_loss", "rr", "win_prob", "reason",
                  "score", "today_pct", "7d_pct", "30d_pct"}, ...],
      "universe_size": 30,
      "market_context": "...",
      "regime": ...,
      "ai_text": "..."
    }
  fmt_crypto_picks_tg(data) -> str  HTML
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import data_sources as ds


# Top 30 by market cap (2025/05) + 一些 high-momentum 中小幣
# yfinance 用 "-USD" 後綴
CRYPTO_UNIVERSE = [
    # Tier 1 (market cap top 10)
    "BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD", "XRP-USD",
    "ADA-USD", "DOGE-USD", "AVAX-USD", "DOT-USD", "TRX-USD",
    # Tier 2 (top 20)
    "LINK-USD", "MATIC-USD", "LTC-USD", "BCH-USD", "ATOM-USD",
    "UNI-USD", "ETC-USD", "FIL-USD", "NEAR-USD", "APT-USD",
    # High momentum / narrative (2024-2025 熱門)
    "ARB-USD", "OP-USD", "INJ-USD", "SUI-USD", "TIA-USD",
    "SEI-USD", "FET-USD", "RNDR-USD", "AKT-USD",
    # Meme (波動大, 給愛追的人選)
    "PEPE-USD", "BONK-USD", "WIF-USD", "SHIB-USD", "FLOKI-USD",
]


# ---------------------------------------------------------------------------
# 抓單一幣的指標
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def _fetch_metrics(symbol: str) -> Optional[Dict]:
    """抓 yfinance 60 日 daily K, 算 today%/7d%/30d%/量比/RSI/MA20."""
    try:
        df = ds.fetch_yf_history(symbol, period="3mo", interval="1d")
        if df is None or df.empty or len(df) < 35:
            return None
        close = df["Close"].astype(float)
        vol = df["Volume"].astype(float)
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        today_pct = (last / prev - 1) * 100 if prev else 0
        pct_7d = (last / float(close.iloc[-8]) - 1) * 100 if len(close) >= 8 else 0
        pct_30d = (last / float(close.iloc[-31]) - 1) * 100 if len(close) >= 31 else 0

        # 量比 = 今日量 / 過去 7 日均量 (不含今日)
        avg7_vol = vol.iloc[-8:-1].mean() if len(vol) >= 8 else 0
        vol_ratio = float(vol.iloc[-1] / avg7_vol) if avg7_vol > 0 else 0

        # RSI(14)
        delta = close.diff().dropna()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = float((100 - 100 / (1 + rs)).iloc[-1])

        # 距 20MA
        ma20 = float(close.tail(20).mean())
        dist_ma20 = (last / ma20 - 1) * 100 if ma20 > 0 else 0

        return {
            "symbol": symbol,
            "name": symbol.replace("-USD", ""),
            "current": round(last, 4) if last < 1 else round(last, 2),
            "today_pct": round(today_pct, 2),
            "7d_pct": round(pct_7d, 2),
            "30d_pct": round(pct_30d, 2),
            "vol_ratio": round(vol_ratio, 2),
            "rsi": round(rsi, 1),
            "dist_ma20_pct": round(dist_ma20, 2),
            "ma20": round(ma20, 4) if ma20 < 1 else round(ma20, 2),
        }
    except Exception as e:
        print(f"[crypto_picker] _fetch_metrics {symbol} failed: {e}", flush=True)
        return None


def _score(m: Dict) -> float:
    """評分 0~10."""
    s = 0.0
    t = m.get("today_pct", 0) or 0
    w = m.get("7d_pct", 0) or 0
    mo = m.get("30d_pct", 0) or 0
    vr = m.get("vol_ratio", 0) or 0
    rsi = m.get("rsi", 50) or 50
    dma = m.get("dist_ma20_pct", 0) or 0

    # today%: 3-15 黃金, >15 過熱輕扣, <0 直接扣
    if 3 <= t <= 15:
        s += 2.0
    elif 15 < t <= 25:
        s += 1.0
    elif t > 25:
        s += 0.3
    elif t < 0:
        s -= 1.0
    elif 0 <= t < 3:
        s += 0.5

    # 7d%: 10-30 健康, >50 警示, <0 扣
    if 10 <= w <= 30:
        s += 3.0
    elif 30 < w <= 50:
        s += 2.0
    elif w > 50:
        s += 1.0  # 已太熱
    elif 0 <= w < 10:
        s += 1.0
    elif w < -5:
        s -= 1.5

    # 30d%: 20-60 確立中期 momentum
    if 20 <= mo <= 60:
        s += 1.5
    elif 60 < mo <= 100:
        s += 0.8
    elif mo > 100:
        s -= 0.5  # 過熱
    elif mo < -10:
        s -= 1.0

    # 量比: 1.5-3 健康放量, >5 噴出但風險高
    if 1.5 <= vr <= 3:
        s += 2.0
    elif 3 < vr <= 5:
        s += 1.2
    elif vr > 5:
        s += 0.5
    elif vr < 0.7:
        s -= 1.0

    # RSI: 50-70 黃金區間
    if 50 <= rsi <= 70:
        s += 2.0
    elif 40 <= rsi < 50:
        s += 1.0
    elif 70 < rsi <= 75:
        s += 0.5
    elif rsi > 75:
        s -= 0.5  # 過熱
    elif rsi < 30:
        s += 1.0  # 超賣反彈機會

    # 距 20MA: 2-15% 健康上升, >25% 追高
    if 2 <= dma <= 15:
        s += 0.5
    elif dma > 25:
        s -= 1.0
    elif dma < -5:
        s -= 0.5

    return round(max(0.0, min(10.0, s)), 2)


def _scan_universe() -> List[Dict]:
    """並行掃描所有 CRYPTO_UNIVERSE, 回排序後的指標清單.

    用 as_completed 而不是 for in futures, 才不會被第一個慢的 future 卡死
    後續已 done 的 future. 整體 timeout 60 秒 (30 幣 ÷ 6 workers ≈ 5 batch × 12s).
    """
    results: List[Dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_fetch_metrics, sym): sym for sym in CRYPTO_UNIVERSE}
        try:
            for fut in as_completed(futures, timeout=60):
                try:
                    m = fut.result(timeout=15)
                    if m is None:
                        continue
                    m["score"] = _score(m)
                    results.append(m)
                except Exception as e:
                    sym = futures.get(fut, "?")
                    print(f"[crypto_picker] scan {sym} failed: {e}", flush=True)
        except TimeoutError:
            print(f"[crypto_picker] _scan_universe overall timeout 60s reached, "
                  f"got {len(results)} results", flush=True)
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Gemini 分析: 從 top 10 挑 5 個給進場建議
# ---------------------------------------------------------------------------
def _gemini_pick_5(top_candidates: List[Dict], top_n: int = 5,
                    market_context: str = "",
                    model: str = "gemini-2.5-flash") -> List[Dict]:
    """從 top_candidates (~10 個) 讓 Gemini 挑 top_n 個並給 entry/target/stop.

    Return: [{symbol, entry_low, entry_high, target, stop_loss, rr,
              win_prob, hold_period, reason}, ...]
    失敗 fall back 用前 top_n 個 + 規則計算 entry/target/stop.

    熊市場下 candidates < top_n: 自動降低 top_n 為 candidates 數量 (避免 Gemini 幻覺多餘 symbol).
    """
    if not top_candidates:
        return []
    # 動態下限: 不能挑超過實際 candidate 數量
    top_n = min(top_n, len(top_candidates))

    # 失敗 fallback: 用規則
    def _rule_based_picks(cands: List[Dict]) -> List[Dict]:
        out = []
        for c in cands[:top_n]:
            cur = c["current"]
            # 進場: 現價 -1% ~ +1% (盤整買進)
            entry_low = round(cur * 0.985, 8) if cur < 1 else round(cur * 0.985, 2)
            entry_high = round(cur * 1.005, 8) if cur < 1 else round(cur * 1.005, 2)
            # 目標: +12% (對應一週 momentum)
            target = round(cur * 1.12, 8) if cur < 1 else round(cur * 1.12, 2)
            # 停損: -7% (加密波動較大, 比股票寬)
            stop = round(cur * 0.93, 8) if cur < 1 else round(cur * 0.93, 2)
            rr = round((target - cur) / (cur - stop), 2) if cur > stop else None
            out.append({
                "symbol": c["symbol"],
                "name": c["name"],
                "current": cur,
                "entry_low": entry_low, "entry_high": entry_high,
                "target": target, "stop_loss": stop, "rr": rr,
                "win_prob": "50%", "hold_period": "3-7 天",
                "reason": f"今日 {c['today_pct']:+.1f}%, 7d {c['7d_pct']:+.1f}%, "
                          f"量比 {c['vol_ratio']:.1f}x, RSI {c['rsi']:.0f}",
                "score": c.get("score", 0),
                "today_pct": c["today_pct"],
                "7d_pct": c["7d_pct"],
                "30d_pct": c["30d_pct"],
            })
        return out

    try:
        import ai_analyzer as _ai
        if not _ai.gemini_available():
            return _rule_based_picks(top_candidates)
    except ImportError:
        return _rule_based_picks(top_candidates)

    # 組 Gemini prompt
    blocks = []
    for c in top_candidates[:10]:
        blocks.append(
            f"  {c['symbol']:>10s}  cur=${c['current']:<10}  "
            f"today {c['today_pct']:+.2f}%  7d {c['7d_pct']:+.2f}%  "
            f"30d {c['30d_pct']:+.2f}%  vol {c['vol_ratio']:.1f}x  "
            f"RSI {c['rsi']:.0f}  dist20MA {c['dist_ma20_pct']:+.1f}%  "
            f"score={c['score']}"
        )
    candidates_text = "\n".join(blocks)

    prompt = f"""你是專業加密貨幣 swing trader. 下面是當前 {len(top_candidates[:10])} 個有上漲跡象的幣種:

{candidates_text}

{f"市場 context: {market_context}" if market_context else ""}

請從上面 candidates 中挑出 **正好 {top_n} 個** 最適合「3-7 天波段進場」的幣 (不是全選最熱的, 要平衡 momentum 跟過熱風險).
**只能用上面 candidates 列出的 symbol, 不要憑空編 ticker.**

對每檔給:
- entry_low / entry_high: 建議進場區間 (USD)
- target: 目標價 (USD), 預期 7-15% 內達成
- stop_loss: 停損 (USD), 距現價 5-10%
- win_prob: 預估上漲機率 (50-75%, 不要寫太樂觀)
- hold_period: "3-7 天" / "1-2 週"
- reason: 1-2 句具體理由 (引用上面數據)

回 **嚴格 JSON array** (不要前後贅述, 不要 markdown):
[
  {{"symbol": "ETH-USD", "entry_low": 3500, "entry_high": 3550, "target": 3850, "stop_loss": 3300, "win_prob": "62%", "hold_period": "5-7 天", "reason": "..."}},
  ...
]
"""

    try:
        import google.generativeai as genai
        genai.configure(api_key=_ai.get_gemini_key())
        m = genai.GenerativeModel(model)
        resp = m.generate_content(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 2000,
                                "response_mime_type": "application/json"},
            safety_settings=_ai.get_safety_settings(),
        )
        text = (getattr(resp, "text", None) or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
        picks_raw = json.loads(text)
        if not isinstance(picks_raw, list) or not picks_raw:
            return _rule_based_picks(top_candidates)

        # 合併 Gemini 結果跟原本的 metrics (補 score / pct 等)
        metrics_map = {c["symbol"]: c for c in top_candidates}
        out = []
        for p in picks_raw[:top_n]:
            sym = p.get("symbol")
            if not sym or sym not in metrics_map:
                continue
            metric = metrics_map[sym]
            try:
                el = float(p.get("entry_low") or metric["current"] * 0.985)
                eh = float(p.get("entry_high") or metric["current"] * 1.005)
                target = float(p.get("target") or metric["current"] * 1.12)
                stop = float(p.get("stop_loss") or metric["current"] * 0.93)
                entry_mid = (el + eh) / 2
                rr = round((target - entry_mid) / (entry_mid - stop), 2) if entry_mid > stop else None
            except (TypeError, ValueError):
                continue
            out.append({
                "symbol": sym,
                "name": metric["name"],
                "current": metric["current"],
                "entry_low": el, "entry_high": eh,
                "target": target, "stop_loss": stop, "rr": rr,
                "win_prob": str(p.get("win_prob", "50%")),
                "hold_period": str(p.get("hold_period", "3-7 天")),
                "reason": str(p.get("reason", ""))[:200],
                "score": metric.get("score", 0),
                "today_pct": metric["today_pct"],
                "7d_pct": metric["7d_pct"],
                "30d_pct": metric["30d_pct"],
                "vol_ratio": metric["vol_ratio"],
                "rsi": metric["rsi"],
            })
        if not out:
            return _rule_based_picks(top_candidates)
        return out
    except Exception as e:
        print(f"[crypto_picker] _gemini_pick_5 failed: {e}", flush=True)
        return _rule_based_picks(top_candidates)


# ---------------------------------------------------------------------------
# 對外接口
# ---------------------------------------------------------------------------
def get_crypto_picks(top_n: int = 5) -> Dict:
    """主流程: scan universe → score → top 10 → Gemini 挑 5 → return data dict."""
    scanned = _scan_universe()
    if not scanned:
        return {"picks": [], "universe_size": 0, "error": "全部抓取失敗"}

    # 過濾條件: today_pct > 0 跟 7d_pct > 0 (確保是上升趨勢, 不是反彈)
    candidates = [c for c in scanned if (c.get("today_pct", 0) > 0 and c.get("7d_pct", 0) > 0)]
    # 若濾完不足 5, 放寬只看 7d > 0
    if len(candidates) < top_n:
        candidates = [c for c in scanned if c.get("7d_pct", 0) > 0]
    top_candidates = candidates[:10]  # 給 Gemini 看 10 個

    # 市場 context: BTC 走勢 + 全市場平均
    market_context = ""
    try:
        btc = next((c for c in scanned if c["symbol"] == "BTC-USD"), None)
        if btc:
            avg_today = sum(c.get("today_pct", 0) for c in scanned) / max(1, len(scanned))
            market_context = (
                f"BTC 今日 {btc['today_pct']:+.2f}% / 7d {btc['7d_pct']:+.2f}%; "
                f"全市場 ({len(scanned)} 幣) 今日均 {avg_today:+.2f}%"
            )
    except Exception:
        pass

    picks = _gemini_pick_5(top_candidates, top_n=top_n, market_context=market_context)

    # 加 regime 風險提示 (crypto 通常跟 NASDAQ / US 大盤相關)
    regime = {}
    try:
        import regime_detector
        regime = regime_detector.detect_market_regime("US")  # crypto 跟美股相關性高
    except Exception:
        pass

    return {
        "picks": picks,
        "top_candidates_count": len(top_candidates),
        "universe_size": len(scanned),
        "market_context": market_context,
        "scanned": scanned[:15],  # 給 dashboard 顯示 universe 排名
        "regime": regime,
    }


# ---------------------------------------------------------------------------
# 推播訊息格式化
# ---------------------------------------------------------------------------
def fmt_crypto_picks_tg(data: Dict) -> str:
    """格式化成 TG HTML 訊息."""
    import html as _html
    def _esc(s):
        return _html.escape(str(s) if s is not None else "", quote=False)

    if not data or data.get("error"):
        return f"<b>🪙 加密貨幣推薦</b>\n\n抓取失敗: {_esc(data.get('error',''))}"

    picks = data.get("picks") or []
    if not picks:
        return ("<b>🪙 加密貨幣推薦</b>\n\n"
                "今日沒有符合條件的標的 (掃 30 幣, 今日+7d 都正報酬才入選).")

    lines = [
        f"<b>🪙 加密貨幣 Top {len(picks)} 進場推薦 ({dt.date.today().strftime('%Y-%m-%d')})</b>",
    ]
    mc = data.get("market_context", "")
    if mc:
        lines.append(f"<i>{_esc(mc)}</i>")

    # regime banner (簡短版)
    regime = data.get("regime") or {}
    if regime.get("regime"):
        icon_map = {"bull":"🟢","bull_weak":"🟡","range":"⚪","bear_weak":"🟠","bear":"🔴"}
        ic = icon_map.get(regime.get("regime"), "")
        lines.append(f"{ic} 美股大盤: {_esc(regime.get('regime_label', ''))} (加密通常與其相關)")

    lines.append("")
    for i, p in enumerate(picks, 1):
        sym = _esc(p.get("symbol", ""))
        cur = p.get("current", 0)
        el = p.get("entry_low", 0)
        eh = p.get("entry_high", 0)
        tg = p.get("target", 0)
        sl = p.get("stop_loss", 0)
        rr = p.get("rr")
        rr_str = f"R:R {rr}" + (" ⭐⭐" if rr and rr >= 3 else " ⭐" if rr and rr >= 2 else "") if rr else ""
        wp = _esc(p.get("win_prob", ""))
        hold = _esc(p.get("hold_period", ""))
        reason = _esc(p.get("reason", ""))
        today = p.get("today_pct", 0)
        wk = p.get("7d_pct", 0)
        mo = p.get("30d_pct", 0)

        lines.append(f"<b>{i}. <code>{sym}</code></b>  ${cur}  {rr_str}")
        lines.append(f"   今日 {today:+.2f}% · 7d {wk:+.2f}% · 30d {mo:+.2f}%")
        lines.append(f"   進場 ${el} ~ ${eh}")
        lines.append(f"   目標 ${tg} / 停損 ${sl}")
        lines.append(f"   上漲機率 {wp} · 持有 {hold}")
        if reason:
            lines.append(f"   {reason}")
        lines.append("")

    lines.append("<i>⚠ 加密波動高於股票, 部位請較股票低 30-50%. 7 天內未達標應檢視 thesis.</i>")
    # G7 fix: 用 notifier._truncate_tg_msg 統一 byte-length 截斷, 避免 TG 4096 byte HTTP 400
    import notifier
    return notifier._truncate_tg_msg("\n".join(lines))
