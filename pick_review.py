"""
pick_review.py — 推播推薦股票的「隔日驗收」

回答使用者每天早上想知道的那件事:
  「昨天推播推薦的那幾檔(台股 + 美股), 隔一個交易日收盤到底有沒有漲?
    真的有漲的比例是多少 %?」

判定標準 (使用者指定):
  隔一個【交易日】的收盤價 > 推薦當下的價格 → 算「有漲」(命中)。
  門檻 0%, 不扣手續費/滑價 (見 signal_tracker.HIT_THRESHOLD_PCT)。

資料來源:
  所有推播在推的當下用 record_picks() 把「代號 / 名稱 / 推薦當下價 / 屬於哪根日 K」
  寫進 signal_tracker (monitor_state["signals"], 存 Google Sheets 跨 run persist)。
  signal_tracker.evaluate_pending() 之後用「交易日 bar」補上結果。

為什麼要用「結算日 (actual_date)」而不是「推播日」分組:
  不同推播的價格基準不一樣 — 晨報 08:00 引用前一交易日收盤 (隔天 = 當天盤),
  盤中 09:32 選股引用當天盤中價 (隔天 = 明天盤)。若照推播日分組, 同一天推播的
  兩批股票結算日會差一天, 晚結算的那批永遠等不到被報導的機會。改用結算日分組,
  每一檔都會在「它結果出爐後的第一封晨報」被報一次, 不重不漏。

API:
  record_picks(signal_type, items, market, source, asof=None, evaluate_after_days=1)
  build_review_block()  -> str   # 給晨報用的 TG HTML 段落
  review_data()         -> Dict  # 給 dashboard / 測試用的原始結果
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import signal_tracker


# 市場 → 用來決定「最後一個完整交易日」的指數代號 (只打一次行情)
_MARKET_PROXY = {"TW": "^TWII", "US": "SPY"}

# 每個市場在驗收段落最多列幾檔 (TG 訊息長度控制)
_MAX_LIST_PER_MARKET = 6


def _today_tpe_str() -> str:
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


# ===========================================================================
# 記錄
# ===========================================================================
def last_trading_day(market: str = "TW") -> Optional[str]:
    """該市場最後一個【已收盤】交易日的日期字串 (YYYY-MM-DD), 抓不到回 None.

    用途: 盤前推播 (晨報 08:00 / 台股盤前 08:30 / 美股盤前) 引用的「現價」其實是
    前一個交易日的收盤價。要正確驗收「隔一天有沒有漲」, 必須把 asof 標成那一天,
    否則會拿「推播當天」往後數一天 → 整整晚一個交易日, 驗到的是別天的行情。
    """
    proxy = _MARKET_PROXY.get(str(market).upper())
    if not proxy:
        return None
    try:
        bars = signal_tracker.fetch_daily_bars(proxy, market=market, period="1mo")
    except Exception as e:
        print(f"[pick_review] last_trading_day({market}) failed: {e}", flush=True)
        return None
    return bars[-1][0] if bars else None


def record_picks(signal_type: str, items: List[Dict], market: str = "TW",
                  source: str = "", asof: Optional[str] = None,
                  price_is_last_close: bool = False,
                  evaluate_after_days: int = 1,
                  expected_direction: str = "up") -> int:
    """把一批「推播出去的推薦股」記進帳本, 回實際記了幾筆.

    items: [{stock_id|symbol, name, current|price}, ...]
    price_is_last_close=True → 自動把 asof 設成該市場最後一個交易日
      (盤前推播用; 盤中/盤後推播不要設, 預設用推播當天)。
    """
    if not items:
        return 0
    try:
        if asof is None and price_is_last_close:
            asof = last_trading_day(market)
        return signal_tracker.record_batch(
            signal_type, items,
            evaluate_after_days=evaluate_after_days,
            expected_direction=expected_direction,
            market=market, source=source or signal_type, asof=asof,
        )
    except Exception as e:
        print(f"[pick_review] record_picks({signal_type}) failed: {e}", flush=True)
        return 0


# ===========================================================================
# 驗收
# ===========================================================================
def review_data(refresh: bool = True) -> Dict:
    """算出「最近一個結算日」的逐檔結果 + 命中率.

    回:
    {
      "TW": {"date": "2026-08-24", "items": [...], "n":5, "hit":3, "pct":60.0, "avg_pct":1.2},
      "US": {...},
      "pending": 7,          # 還在等結果的筆數
      "cum": {"TW": {...}, "US": {...}, "ALL": {...}},   # 近 30 日累計
    }
    某市場沒有已結算資料時該 key 為 None。
    """
    if refresh:
        try:
            n = signal_tracker.evaluate_pending()
            if n:
                print(f"[pick_review] evaluate_pending 補上 {n} 筆結果", flush=True)
        except Exception as e:
            print(f"[pick_review] evaluate_pending failed: {e}", flush=True)

    try:
        records = signal_tracker.load_records()
    except Exception as e:
        print(f"[pick_review] load_records failed: {e}", flush=True)
        records = []

    out: Dict = {"TW": None, "US": None, "pending": 0, "cum": {}}

    done = [r for r in records
            if r.get("hit") is not None and r.get("actual_date")
            and r.get("expected_direction", "up") == "up"]
    out["pending"] = sum(1 for r in records
                         if r.get("hit") is None
                         and not (r.get("extras") or {}).get("_no_data"))

    for mkt in ("TW", "US"):
        rows = [r for r in done if signal_tracker._record_market(r) == mkt]
        if not rows:
            continue
        latest = max(r["actual_date"] for r in rows)
        batch = [r for r in rows if r.get("actual_date") == latest]
        # 同一檔可能同時被多個訊號推 (例如晨報 + 盤前) — 以 stock_id 去重, 只留一筆
        seen = set()
        items = []
        for r in sorted(batch, key=lambda x: -(x.get("actual_pct") or 0)):
            sid = str(r.get("stock_id", ""))
            if sid in seen:
                continue
            seen.add(sid)
            items.append({
                "stock_id": sid,
                "name": r.get("name") or "",
                "pct": r.get("actual_pct"),
                "hit": bool(r.get("hit")),
                "predicted_price": r.get("predicted_price"),
                "actual_price": r.get("actual_price"),
                "predicted_at": r.get("predicted_at"),
                "signal_type": r.get("signal_type"),
                "source": (r.get("extras") or {}).get("source") or r.get("signal_type"),
            })
        n = len(items)
        hit = sum(1 for i in items if i["hit"])
        pcts = [float(i["pct"]) for i in items if i.get("pct") is not None]
        out[mkt] = {
            "date": latest,
            "items": items,
            "n": n,
            "hit": hit,
            "pct": round(hit / n * 100, 1) if n else None,
            "avg_pct": round(sum(pcts) / len(pcts), 2) if pcts else None,
        }

    for mkt in ("TW", "US", None):
        try:
            s = signal_tracker.accuracy_summary(None, lookback_days=30, market=mkt)
            out["cum"][mkt or "ALL"] = s
        except Exception:
            out["cum"][mkt or "ALL"] = {"n": 0, "hit": 0, "pct": None}
    return out


def _fmt_market_block(flag: str, title: str, blk: Optional[Dict]) -> List[str]:
    if not blk or not blk.get("items"):
        return []
    d = blk["date"]
    try:
        d_short = dt.datetime.strptime(d, "%Y-%m-%d").strftime("%-m/%-d")
    except Exception:
        try:
            d_short = dt.datetime.strptime(d, "%Y-%m-%d").strftime("%m/%d")
        except Exception:
            d_short = d
    lines = [f"{flag} <b>{title}</b> ({d_short} 收盤結算)"]
    for it in blk["items"][:_MAX_LIST_PER_MARKET]:
        mark = "✅" if it["hit"] else "❌"
        pct = it.get("pct")
        pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "—"
        label = f"{it['stock_id']} {it['name']}".strip()
        lines.append(f"  {mark} {label} {pct_s}")
    extra = len(blk["items"]) - _MAX_LIST_PER_MARKET
    if extra > 0:
        lines.append(f"  <i>…另 {extra} 檔</i>")
    pct = blk.get("pct")
    avg = blk.get("avg_pct")
    mark = "🟢" if (pct is not None and pct >= 60) else ("🟡" if (pct is not None and pct >= 40) else "🔴")
    tail = f"  {mark} <b>隔日上漲比例 {blk['hit']}/{blk['n']} = {pct:.0f}%</b>"
    if avg is not None:
        tail += f" · 平均 {avg:+.2f}%"
    lines.append(tail)
    return lines


def build_review_block(refresh: bool = True) -> str:
    """晨報用的「昨日推薦驗收」段落 (TG HTML). 沒有任何已結算資料時回 ""。"""
    try:
        data = review_data(refresh=refresh)
    except Exception as e:
        print(f"[pick_review] build_review_block failed: {e}", flush=True)
        return ""

    body: List[str] = []
    body += _fmt_market_block("🇹🇼", "台股推薦驗收", data.get("TW"))
    if body and data.get("US"):
        body.append("")
    body += _fmt_market_block("🇺🇸", "美股推薦驗收", data.get("US"))

    if not body:
        pend = data.get("pending") or 0
        if pend:
            return (f"🎯 <b>昨日推薦驗收</b>\n  <i>尚無已結算的推薦 "
                    f"({pend} 檔等待隔日收盤結果, 明天起開始統計)</i>")
        return ""

    lines = ["🎯 <b>昨日推薦驗收</b> — 隔一個交易日收盤 vs 推薦當下價"] + body

    # 近 30 日累計 (建立長期信任感)
    cum = (data.get("cum") or {}).get("ALL") or {}
    lines.append("")
    if (cum.get("n") or 0) >= 10 and cum.get("pct") is not None:
        mark = "🟢" if cum["pct"] >= 60 else ("🟡" if cum["pct"] >= 40 else "🔴")
        extra = ""
        if cum.get("avg_pct") is not None:
            extra = f" · 平均 {cum['avg_pct']:+.2f}%"
        lines.append(f"  📊 近 30 日累計:{mark} {cum['hit']}/{cum['n']} = {cum['pct']:.0f}%{extra}")
    elif (cum.get("n") or 0) > 0:
        lines.append(f"  📊 近 30 日累計樣本 {cum['n']} 筆 (滿 10 筆才顯示勝率)")

    pend = data.get("pending") or 0
    if pend:
        lines.append(f"  <i>另有 {pend} 檔等待結算</i>")
    return "\n".join(lines)


if __name__ == "__main__":  # 手動檢查用
    import json
    print(json.dumps(review_data(), ensure_ascii=False, indent=2, default=str))
    print()
    print(build_review_block(refresh=False))
