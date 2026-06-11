"""
institutional_positioning.py
外資 / 投信 / 自營商 / 大型交易人 (大摩等) 籌碼面 snapshot.

用途: 給 daily_outlook_advisor + pre_market_alert 在 Gemini prompt 與推播中
顯示「籌碼面」訊號 — 是台股盤中走勢最關鍵的領先指標。

抓取項目:
  1. 三大法人現貨買賣超 (整市場合計, 億元) — 近 5 日
  2. 三大法人台指期淨多空口數 — 近 5 日
  3. 大型交易人 (大摩 / JPM / Goldman) 台指期未平倉淨多空
  4. 選擇權 PCR (Put/Call OI Ratio) — VIX TW

資料來源:
  優先 FinMind (需 token), fallback TWSE/期交所公開 HTML.

API:
  fetch_institutional_snapshot() -> Dict
  format_positioning_for_tg(snap) -> str
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional


def _safe_finmind_data(dataset: str, days: int = 7) -> Optional[List[Dict]]:
    """通用 FinMind 抓最近 N 天."""
    try:
        import os
        token = os.getenv("FINMIND_TOKEN") or ""
        if not token:
            try:
                import streamlit as st
                token = st.secrets.get("FINMIND_TOKEN", "")  # type: ignore
            except Exception:
                pass
        if not token:
            return None
        import requests
        end = dt.date.today().strftime("%Y-%m-%d")
        start = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y-%m-%d")
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": dataset,
            "start_date": start,
            "end_date": end,
            "token": token,
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        js = r.json()
        if js.get("status") != 200:
            return None
        return js.get("data") or []
    except Exception as e:
        print(f"[positioning] finmind {dataset} fail: {e}", flush=True)
        return None


def _fetch_inst_spot() -> Dict:
    """外資/投信/自營商 現貨買賣超 (整市場合計, 億元) — 近 5 日."""
    out = {"data": [], "summary": "", "foreign_streak": 0, "trust_streak": 0}
    rows = _safe_finmind_data("TaiwanStockInstitutionalInvestorsBuySell", days=14)
    if not rows:
        return out
    # 整市場合計: 各日 sum
    by_date = {}
    for r in rows:
        d = r.get("date", "")
        nm = r.get("name", "")
        buy_sell = float(r.get("buy", 0)) - float(r.get("sell", 0))  # 元
        if d not in by_date:
            by_date[d] = {"Foreign_Investor": 0.0, "Investment_Trust": 0.0,
                           "Dealer": 0.0}
        # name 對應: Foreign_Investor / Investment_Trust / Dealer
        if "Foreign" in nm or "外資" in nm:
            by_date[d]["Foreign_Investor"] += buy_sell
        elif "Investment_Trust" in nm or "投信" in nm:
            by_date[d]["Investment_Trust"] += buy_sell
        elif "Dealer" in nm or "自營商" in nm:
            by_date[d]["Dealer"] += buy_sell
    dates = sorted(by_date.keys())[-5:]
    data = []
    for d in dates:
        rec = by_date[d]
        # 轉億元 (元 → 億 = /1e8)
        data.append({
            "date": d,
            "foreign_yi": round(rec["Foreign_Investor"] / 1e8, 2),
            "trust_yi": round(rec["Investment_Trust"] / 1e8, 2),
            "dealer_yi": round(rec["Dealer"] / 1e8, 2),
        })
    out["data"] = data
    # 連續買/賣超 — Bug fix: 真正的「從最後一天反向數連續同向」
    if data:
        latest = data[-1]
        def _streak(field: str) -> int:
            if latest[field] == 0:
                return 0
            sign = 1 if latest[field] > 0 else -1
            n = 0
            for d in reversed(data):
                if (sign > 0 and d[field] > 0) or (sign < 0 and d[field] < 0):
                    n += 1
                else:
                    break
            return n * sign
        out["foreign_streak"] = _streak("foreign_yi")
        out["trust_streak"] = _streak("trust_yi")
        out["summary"] = (
            f"外資 {latest['foreign_yi']:+.2f} 億 | "
            f"投信 {latest['trust_yi']:+.2f} 億 | "
            f"自營 {latest['dealer_yi']:+.2f} 億"
        )
    return out


def _fetch_inst_futures() -> Dict:
    """三大法人台指期未平倉 (口數) — 近 5 日."""
    out = {"data": [], "summary": "", "foreign_net_oi": 0}
    rows = _safe_finmind_data("TaiwanFuturesInstitutionalInvestors", days=10)
    if not rows:
        return out
    # 篩台指期 TX
    rows = [r for r in rows if r.get("futures_id") == "TX"
            or r.get("contract_type") == "TXF"
            or r.get("name") in ("Foreign_Investor", "Investment_Trust", "Dealer")]
    by_date = {}
    for r in rows:
        d = r.get("date", "")
        nm = r.get("name") or r.get("institutional_investors", "")
        # 取未平倉淨額 (open_interest_balance_long - open_interest_balance_short)
        long_oi = float(r.get("open_interest_balance_long", 0) or 0)
        short_oi = float(r.get("open_interest_balance_short", 0) or 0)
        net_oi = long_oi - short_oi
        if d not in by_date:
            by_date[d] = {"Foreign_Investor": 0, "Investment_Trust": 0, "Dealer": 0}
        if "Foreign" in nm or "外資" in nm:
            by_date[d]["Foreign_Investor"] = net_oi
        elif "Trust" in nm or "投信" in nm:
            by_date[d]["Investment_Trust"] = net_oi
        elif "Dealer" in nm or "自營" in nm:
            by_date[d]["Dealer"] = net_oi
    dates = sorted(by_date.keys())[-5:]
    for d in dates:
        out["data"].append({
            "date": d,
            "foreign_oi": int(by_date[d]["Foreign_Investor"]),
            "trust_oi": int(by_date[d]["Investment_Trust"]),
            "dealer_oi": int(by_date[d]["Dealer"]),
        })
    if out["data"]:
        latest = out["data"][-1]
        out["foreign_net_oi"] = latest["foreign_oi"]
        sign = "🟢 偏多" if latest["foreign_oi"] >= 5000 else \
               ("🔴 偏空" if latest["foreign_oi"] <= -5000 else "⚪ 中性")
        out["summary"] = (
            f"外資台指期淨 {latest['foreign_oi']:+,} 口 {sign} | "
            f"投信 {latest['trust_oi']:+,} | "
            f"自營 {latest['dealer_oi']:+,}"
        )
    return out


def _fetch_option_pcr() -> Dict:
    """選擇權 PCR (台指 OP put/call OI ratio).

    Bug fix (MED): 改用 TaiwanOptionDailyMarketReport 抓「全市場」put/call OI
    (原本用法人 dataset + open_interest_balance_long 是錯的, 等於只算
    法人多方部位的 put/call 比, 不是真正 PCR)
    """
    out = {"pcr": None, "signal": ""}
    # 優先 dataset: TaiwanOptionDailyMarketReport (全市場合計, 含 put_oi / call_oi)
    rows = _safe_finmind_data("TaiwanOptionDailyMarketReport", days=7)
    put_oi = 0.0
    call_oi = 0.0

    if rows:
        # 篩 TXO (台指選擇權)
        txo_rows = [r for r in rows if (r.get("option_id") or r.get("contract_id") or "") in ("TXO", "TX")]
        if txo_rows:
            latest_date = max(r.get("date", "") for r in txo_rows)
            today_rows = [r for r in txo_rows if r.get("date") == latest_date]
            for r in today_rows:
                # 不同 FinMind 版本欄位名可能不同, 試多種 key
                cp = (r.get("call_put") or r.get("type") or "").lower()
                oi = float(r.get("open_interest", 0) or r.get("open_interest_total", 0) or 0)
                if "put" in cp:
                    put_oi += oi
                elif "call" in cp:
                    call_oi += oi

    if call_oi > 0 and put_oi > 0:
        pcr = round(put_oi / call_oi, 2)
        out["pcr"] = pcr
        if pcr >= 1.3:
            out["signal"] = "🟢 PCR > 1.3 — 偏空保護過重, 反向偏多訊號"
        elif pcr >= 1.0:
            out["signal"] = "⚪ PCR 中性偏空"
        elif pcr >= 0.8:
            out["signal"] = "⚪ PCR 中性偏多"
        else:
            out["signal"] = "🔴 PCR < 0.8 — 過度樂觀, 反向警示"
    # 抓不到就 return 空 (notifier 會自動 skip 顯示, 不會顯示錯的 PCR)
    return out


def _fetch_futures_basis() -> Dict:
    """台指期升貼水 (basis) — 期貨價 - 現貨價.

    升水 (basis > 0): 期貨高於現貨 → 多頭強烈
    貼水 (basis < 0): 期貨低於現貨 → 空頭強烈, 或避險需求高
    台股近月一般在 -30 ~ +30 之間, 異常值有訊號意義.
    """
    out = {"basis": None, "basis_pct": None, "signal": ""}
    try:
        import data_sources as ds
        # 台指期主力月 (yfinance: ^TWII 是現股, TX=F 可能不存在)
        # FinMind: TaiwanFuturesDaily
        rows = _safe_finmind_data("TaiwanFuturesDaily", days=3)
        if not rows:
            return out
        # 篩 TX (台指近月)
        tx_rows = [r for r in rows if r.get("futures_id") == "TX" or "TXF" in str(r.get("contract_date", ""))]
        if not tx_rows:
            return out
        # 取最新日近月合約
        latest_date = max(r.get("date", "") for r in tx_rows)
        today = [r for r in tx_rows if r.get("date") == latest_date]
        if not today:
            return out
        # 取「結算」近月 (Contract month earliest)
        today.sort(key=lambda x: x.get("contract_date", ""))
        near = today[0]
        fut_close = float(near.get("close", 0) or 0)
        if fut_close == 0:
            return out
        # 現股: TWII
        twii_df = ds.fetch_yf_history("^TWII", period="3d", interval="1d")
        if twii_df is None or twii_df.empty:
            return out
        spot = float(twii_df["Close"].iloc[-1])
        basis = fut_close - spot
        basis_pct = (basis / spot) * 100 if spot > 0 else 0
        out["basis"] = round(basis, 2)
        out["basis_pct"] = round(basis_pct, 3)
        if basis >= 50:
            out["signal"] = "🟢 升水 ≥50 點 — 多頭強烈"
        elif basis >= 0:
            out["signal"] = "🟡 微升水 — 多頭略佔上風"
        elif basis >= -50:
            out["signal"] = "🟡 微貼水 — 多空均衡偏空"
        else:
            out["signal"] = "🔴 貼水 ≥50 點 — 空頭強烈或避險需求高"
    except Exception as e:
        print(f"[positioning] futures_basis fail: {e}", flush=True)
    return out


def _fetch_retail_mtx() -> Dict:
    """小台 (MTX) 散戶留倉 — 散戶反指標.

    散戶大量留多 → 反向偏空; 大量留空 → 反向偏多
    """
    out = {"retail_net": None, "signal": ""}
    rows = _safe_finmind_data("TaiwanFuturesInstitutionalInvestors", days=5)
    if not rows:
        return out
    # MTX (小台) = 散戶代理 (法人很少操作小台)
    mtx_rows = [r for r in rows if r.get("futures_id") == "MTX" or r.get("contract_type") == "MTX"]
    if not mtx_rows:
        return out
    latest_date = max(r.get("date", "") for r in mtx_rows)
    today = [r for r in mtx_rows if r.get("date") == latest_date]
    if not today:
        return out
    # 法人留倉 = 法人對小台部位 (其餘是散戶 = 整市場 - 法人)
    # 散戶 = 全市場未平倉 - 法人持倉
    # 簡化: 用法人總淨額負值近似散戶淨
    fi_net = 0
    for r in today:
        long_oi = float(r.get("open_interest_balance_long", 0) or 0)
        short_oi = float(r.get("open_interest_balance_short", 0) or 0)
        fi_net += long_oi - short_oi
    retail_net = -fi_net  # 散戶反向
    out["retail_net"] = int(retail_net)
    if retail_net >= 8000:
        out["signal"] = "🔴 散戶大量留多 (反指標) — 偏空訊號"
    elif retail_net >= 3000:
        out["signal"] = "🟡 散戶偏多 — 中性偏空"
    elif retail_net <= -8000:
        out["signal"] = "🟢 散戶大量留空 (反指標) — 偏多訊號"
    elif retail_net <= -3000:
        out["signal"] = "🟡 散戶偏空 — 中性偏多"
    else:
        out["signal"] = "⚪ 散戶部位中性"
    return out


def _fetch_major_traders() -> Dict:
    """大型交易人 (前 5 大特定法人, 含大摩/JPM/Goldman) 台指期淨多空."""
    out = {"net": 0, "summary": ""}
    # FinMind 沒有直接的大表 dataset, 從期交所 HTML 抓
    try:
        import requests
        from datetime import date, timedelta
        d = date.today() - timedelta(days=1)
        # 期交所大型交易人未平倉表 daily
        url = (
            "https://www.taifex.com.tw/cht/3/largeTraderFutQry?"
            f"queryStartDate={d.strftime('%Y/%m/%d')}&"
            f"queryEndDate={d.strftime('%Y/%m/%d')}&commodityId=TXF"
        )
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return out
        # 簡單解析 (前 5 大特定法人 net = top5 net)
        # 期交所 HTML 結構複雜, 這裡用 fallback: 抓 「特定法人」「前五大」「未平倉淨額」
        txt = r.text
        # 暫時 placeholder: 沒解析就 return 空
        if "特定法人" in txt or "Specific" in txt:
            out["summary"] = "(大表已抓, 請查期交所明細)"
    except Exception:
        pass
    return out


def fetch_institutional_snapshot() -> Dict:
    """整合所有籌碼面 snapshot, 提供給 daily_outlook + pre_market."""
    snap = {
        "spot": _fetch_inst_spot(),
        "futures": _fetch_inst_futures(),
        "pcr": _fetch_option_pcr(),
        "major_traders": _fetch_major_traders(),
        "basis": _fetch_futures_basis(),
        "retail_mtx": _fetch_retail_mtx(),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    # 綜合 bias
    bias_score = 0
    spot = snap["spot"]; fut = snap["futures"]; pcr = snap["pcr"]
    if spot.get("data"):
        latest_yi = spot["data"][-1]["foreign_yi"]
        if latest_yi > 50: bias_score += 2
        elif latest_yi > 0: bias_score += 1
        elif latest_yi < -50: bias_score -= 2
        elif latest_yi < 0: bias_score -= 1
    if fut.get("foreign_net_oi"):
        oi = fut["foreign_net_oi"]
        if oi >= 10000: bias_score += 2
        elif oi >= 5000: bias_score += 1
        elif oi <= -10000: bias_score -= 2
        elif oi <= -5000: bias_score -= 1
    if pcr.get("pcr") is not None:
        p = pcr["pcr"]
        if p >= 1.3: bias_score += 1
        elif p < 0.8: bias_score -= 1
    snap["bias_score"] = bias_score
    if bias_score >= 3:
        snap["bias_label"] = "🟢 籌碼面強烈偏多"
    elif bias_score >= 1:
        snap["bias_label"] = "🟢 籌碼面偏多"
    elif bias_score <= -3:
        snap["bias_label"] = "🔴 籌碼面強烈偏空"
    elif bias_score <= -1:
        snap["bias_label"] = "🔴 籌碼面偏空"
    else:
        snap["bias_label"] = "⚪ 籌碼面中性"
    return snap


def format_positioning_for_tg(snap: Dict) -> str:
    """格式化籌碼面 TG 區塊 (含現貨/期貨/PCR/升貼水/小台)."""
    if not snap:
        return ""
    lines = ["📑 <b>外資籌碼面</b>", f"<i>{snap.get('bias_label', '')}</i>"]
    spot = snap.get("spot", {})
    if spot.get("summary"):
        lines.append(f"• 現貨: {spot['summary']}")
        fs = spot.get("foreign_streak", 0)
        ts = spot.get("trust_streak", 0)
        if abs(fs) >= 3:
            tag = "連買" if fs > 0 else "連賣"
            lines.append(f"  └ 外資 {tag} {abs(fs)} 日")
        if abs(ts) >= 3:
            tag = "連買" if ts > 0 else "連賣"
            lines.append(f"  └ 投信 {tag} {abs(ts)} 日")
    fut = snap.get("futures", {})
    if fut.get("summary"):
        lines.append(f"• 期貨: {fut['summary']}")
    pcr = snap.get("pcr", {})
    if pcr.get("pcr") is not None:
        lines.append(f"• 選擇權 PCR: {pcr['pcr']:.2f}")
        if pcr.get("signal"):
            lines.append(f"  └ {pcr['signal']}")
    basis = snap.get("basis", {})
    if basis.get("basis") is not None:
        b = basis["basis"]
        sign = "+" if b >= 0 else ""
        lines.append(f"• 期貨升貼水: {sign}{b:.2f} 點")
        if basis.get("signal"):
            lines.append(f"  └ {basis['signal']}")
    rtm = snap.get("retail_mtx", {})
    if rtm.get("retail_net") is not None:
        rn = rtm["retail_net"]
        sign = "+" if rn >= 0 else ""
        lines.append(f"• 小台散戶淨倉: {sign}{rn:,} 口 (反指標)")
        if rtm.get("signal"):
            lines.append(f"  └ {rtm['signal']}")
    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


def summarize_for_gemini(snap: Dict) -> str:
    """精簡版 (給 Gemini prompt)."""
    if not snap:
        return ""
    parts = []
    spot = snap.get("spot", {})
    if spot.get("summary"):
        parts.append(f"外資現貨 {spot['summary']}")
    fut = snap.get("futures", {})
    if fut.get("foreign_net_oi"):
        parts.append(f"外資台指期淨{fut['foreign_net_oi']:+,}口")
    pcr = snap.get("pcr", {})
    if pcr.get("pcr") is not None:
        parts.append(f"PCR {pcr['pcr']:.2f}")
    basis = snap.get("basis", {})
    if basis.get("basis") is not None:
        parts.append(f"期貨升貼水 {basis['basis']:+.2f} 點")
    rtm = snap.get("retail_mtx", {})
    rtm = snap.get("retail_mtx", {})
    if rtm.get("retail_net") is not None:
        parts.append(f"小台散戶淨倉 {rtm['retail_net']:+,} 口")
    if snap.get("bias_label"):
        parts.append(snap["bias_label"])
    return " | ".join(parts)
