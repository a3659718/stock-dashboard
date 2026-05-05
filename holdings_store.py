"""
holdings_store.py
台股持倉清單 — 跟 watchlist 完全分開.

差別:
  watchlist  = 觀察名單, 只在價格穿越門檻時推警報
  holdings   = 已持有, 每天 tw_close 做完整分析 (技術+籌碼+新聞+Gemini)

最多 15 檔 TW, 持久化到 Google Sheets (跟 watchlist 共用 service account).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Dict, List, Optional


HOLDINGS_FILE = Path("holdings.json")
MAX_HOLDINGS = 15


# ---------------------------------------------------------------------------
# Google Sheets backend (重用 watchlist 的 _get_sheet)
# ---------------------------------------------------------------------------
def _get_sheet():
    try:
        import watchlist_store
        return watchlist_store._get_sheet("holdings")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------
def _normalize(d: Dict) -> Dict:
    out = {
        "stock_id": str(d.get("stock_id", "")).strip().upper(),
        "name": str(d.get("name", "")).strip(),
        "entry_price": d.get("entry_price"),
        "shares": d.get("shares"),
        "note": str(d.get("note", "")).strip(),
        "added_date": str(d.get("added_date", "")).strip(),
    }
    try:
        out["entry_price"] = float(out["entry_price"]) if out["entry_price"] not in (None, "", 0, 0.0) else None
    except Exception:
        out["entry_price"] = None
    try:
        out["shares"] = int(out["shares"]) if out["shares"] not in (None, "", 0, 0.0) else None
    except Exception:
        out["shares"] = None
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def load_holdings() -> List[Dict]:
    sheet = _get_sheet()
    if sheet is not None:
        try:
            data = sheet.get_all_records()
            if data:
                return [_normalize(d) for d in data][:MAX_HOLDINGS]
        except Exception:
            pass
    if HOLDINGS_FILE.exists():
        try:
            data = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
            return [_normalize(d) for d in data][:MAX_HOLDINGS]
        except Exception:
            return []
    return []


def save_holdings(items: List[Dict]) -> bool:
    items = items[:MAX_HOLDINGS]
    items = [_normalize(d) for d in items]
    try:
        HOLDINGS_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    sheet = _get_sheet()
    if sheet is not None:
        try:
            sheet.clear()
            cols = ["stock_id", "name", "entry_price", "shares", "note", "added_date"]
            sheet.append_row(cols)
            for d in items:
                sheet.append_row([str(d.get(c, "")) for c in cols])
            return True
        except Exception:
            pass
    return True


def add_holding(stock_id: str, name: str = "", entry_price: Optional[float] = None,
                shares: Optional[int] = None, note: str = "") -> bool:
    items = load_holdings()
    sid = str(stock_id).strip().upper()
    if not sid:
        return False
    found = False
    for i in items:
        if str(i.get("stock_id", "")).upper() == sid:
            i["name"] = name or i.get("name", "")
            if entry_price is not None:
                i["entry_price"] = entry_price
            if shares is not None:
                i["shares"] = shares
            if note:
                i["note"] = note
            found = True
            break
    if not found:
        if len(items) >= MAX_HOLDINGS:
            return False
        items.append({
            "stock_id": sid,
            "name": name,
            "entry_price": entry_price,
            "shares": shares,
            "note": note,
            "added_date": dt.date.today().strftime("%Y-%m-%d"),
        })
    return save_holdings(items)


def remove_holding(stock_id: str) -> bool:
    items = load_holdings()
    sid = str(stock_id).strip().upper()
    items = [i for i in items if str(i.get("stock_id", "")).upper() != sid]
    save_holdings(items)
    return True
