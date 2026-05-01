"""
watchlist_store.py
自選股持久化 + monitor state 管理。

儲存策略:
  1. 優先 Google Sheets (跨環境共享, 永久保存)
  2. Fallback: local JSON (Streamlit Cloud / GitHub Actions ephemeral)

資料格式:
  watchlist.json: [{stock_id, name, market, entry_price, added_date}, ...]
  monitor_state.json: {
    "watchlist_alerts": {stock_id: {last_pct: int, base_price: float}},
    "index_alerts": {symbol: {last_level: float, last_direction: str}},
    "crypto_alerts": {symbol: {last_pct: float, base_price: float, base_date: str}},
  }
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st

import data_sources as ds


WATCHLIST_FILE = Path("watchlist.json")
MONITOR_STATE_FILE = Path("monitor_state.json")
MAX_WATCHLIST = 15


# ---------------------------------------------------------------------------
# Watchlist (15 檔上限)
# ---------------------------------------------------------------------------
def load_watchlist() -> List[Dict]:
    """讀取自選股. 優先 Google Sheets, fallback local JSON."""
    sheet = _get_sheet("watchlist")
    if sheet is not None:
        try:
            data = sheet.get_all_records()
            if data:
                return [_normalize_item(d) for d in data][:MAX_WATCHLIST]
        except Exception:
            pass
    # Fallback: local
    if WATCHLIST_FILE.exists():
        try:
            data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            return [_normalize_item(d) for d in data][:MAX_WATCHLIST]
        except Exception:
            return []
    return []


def save_watchlist(items: List[Dict]) -> bool:
    """儲存自選股. 同步到 Sheets + local."""
    items = items[:MAX_WATCHLIST]
    items = [_normalize_item(d) for d in items]

    # Local
    try:
        WATCHLIST_FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    # Sheets
    sheet = _get_sheet("watchlist")
    if sheet is not None:
        try:
            sheet.clear()
            cols = ["stock_id", "name", "market", "entry_price", "added_date"]
            sheet.append_row(cols)
            for d in items:
                sheet.append_row([str(d.get(c, "")) for c in cols])
            return True
        except Exception:
            pass
    return True


def add_to_watchlist(stock_id: str, name: str = "", market: str = "TW",
                      entry_price: Optional[float] = None) -> bool:
    """加 1 檔. 已存在則更新."""
    items = load_watchlist()
    sid = str(stock_id).strip().upper()
    if not sid:
        return False
    # 已存在則更新
    found = False
    for i in items:
        if str(i.get("stock_id", "")).upper() == sid:
            i["name"] = name or i.get("name", "")
            if entry_price is not None:
                i["entry_price"] = entry_price
            found = True
            break
    if not found:
        if len(items) >= MAX_WATCHLIST:
            return False  # 滿了
        items.append({
            "stock_id": sid,
            "name": name,
            "market": market.upper(),
            "entry_price": entry_price,
            "added_date": dt.date.today().strftime("%Y-%m-%d"),
        })
    return save_watchlist(items)


def remove_from_watchlist(stock_id: str) -> bool:
    items = load_watchlist()
    sid = str(stock_id).strip().upper()
    items = [i for i in items if str(i.get("stock_id", "")).upper() != sid]
    save_watchlist(items)
    # 同時清掉這檔的 alert state
    state = load_monitor_state()
    if "watchlist_alerts" in state and sid in state["watchlist_alerts"]:
        del state["watchlist_alerts"][sid]
        save_monitor_state(state)
    return True


def _normalize_item(d: Dict) -> Dict:
    out = {
        "stock_id": str(d.get("stock_id", "")).strip().upper(),
        "name": str(d.get("name", "")).strip(),
        "market": str(d.get("market", "TW")).strip().upper(),
        "entry_price": d.get("entry_price"),
        "added_date": str(d.get("added_date", "")).strip(),
    }
    try:
        out["entry_price"] = float(out["entry_price"]) if out["entry_price"] not in (None, "") else None
    except Exception:
        out["entry_price"] = None
    return out


# ---------------------------------------------------------------------------
# Monitor state
# ---------------------------------------------------------------------------
def load_monitor_state() -> Dict:
    """讀全部 monitor state. 結構:
    {
      "watchlist_alerts": {sid: {last_pct: int, base_price: float}},
      "index_alerts": {sym: {last_level: float, last_direction: str, last_alert: str}},
      "crypto_alerts": {sym: {last_pct: float, base_price: float, base_date: str}},
    }
    """
    sheet = _get_sheet("monitor_state")
    if sheet is not None:
        try:
            cell = sheet.acell("A1").value
            if cell:
                return json.loads(cell)
        except Exception:
            pass
    if MONITOR_STATE_FILE.exists():
        try:
            return json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"watchlist_alerts": {}, "index_alerts": {}, "crypto_alerts": {}}


def save_monitor_state(state: Dict) -> None:
    """儲存 monitor state."""
    blob = json.dumps(state, ensure_ascii=False, indent=2)
    try:
        MONITOR_STATE_FILE.write_text(blob, encoding="utf-8")
    except Exception:
        pass
    sheet = _get_sheet("monitor_state")
    if sheet is not None:
        try:
            sheet.clear()
            sheet.update_acell("A1", blob)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Google Sheets backend (optional)
# ---------------------------------------------------------------------------
def _get_sheet(name: str):
    """取得 worksheet by name; 不存在則建立."""
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore
    except Exception:
        return None
    sa_raw = ds._secret("GCP_SERVICE_ACCOUNT_JSON")
    sheet_id = ds._secret("GOOGLE_SHEETS_ID")
    if not (sa_raw and sheet_id):
        return None
    try:
        info = json.loads(sa_raw) if isinstance(sa_raw, str) else dict(sa_raw)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        wb = gc.open_by_key(sheet_id)
        try:
            return wb.worksheet(name)
        except Exception:
            return wb.add_worksheet(title=name, rows=200, cols=10)
    except Exception:
        return None
