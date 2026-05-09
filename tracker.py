"""
tracker.py
推薦股追蹤：

每次掃描完，把命中清單存成 snapshot (CSV)。
過幾天回頭看「當時推薦的股票」現在表現如何。

儲存位置：
  - 預設: app 工作目錄下的 tracking_history.csv (Streamlit Cloud 重啟會清空)
  - 推薦: 上傳到 Google Sheets (持久化, 需設 GOOGLE_SHEETS_ID + GCP_SERVICE_ACCOUNT_JSON)
  - Fallback: 提供 CSV 下載按鈕，使用者自行保存

CSV 欄位:
  snapshot_date, stock_id, stock_name, market, hits_label, hit_count,
  base_price, vol_ratio, today_pct, invtrust_today, invtrust_5d, capital_ratio
"""

from __future__ import annotations

import datetime as dt
import io
import os
from typing import Optional

import pandas as pd
import streamlit as st

import data_sources as ds

LOCAL_CSV = "tracking_history.csv"  # 相對 app 根目錄
SNAPSHOT_COLUMNS = [
    "snapshot_date", "stock_id", "stock_name", "market",
    "hits_label", "hit_count",
    "base_price", "vol_ratio", "today_pct",
    "invtrust_today", "invtrust_5d", "capital_ratio",
]


# ---------------------------------------------------------------------------
# Google Sheets backend (optional)
# ---------------------------------------------------------------------------
def _get_gsheet_client():
    """若 secrets 有設 GCP_SERVICE_ACCOUNT_JSON 與 GOOGLE_SHEETS_ID，就回傳 sheet。"""
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
        import json
        info = json.loads(sa_raw) if isinstance(sa_raw, str) else dict(sa_raw)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        return gc.open_by_key(sheet_id).sheet1
    except Exception:
        return None


def has_gsheets_config() -> bool:
    return bool(ds._secret("GCP_SERVICE_ACCOUNT_JSON") and ds._secret("GOOGLE_SHEETS_ID"))


# ---------------------------------------------------------------------------
# 寫入 / 讀取 snapshot
# ---------------------------------------------------------------------------
def _row_from_combined(row: dict, snapshot_date: str) -> dict:
    """從 tw_screener 的 combined row 抽出追蹤需要的欄位。"""
    return {
        "snapshot_date": snapshot_date,
        "stock_id": row.get("stock_id", ""),
        "stock_name": row.get("stock_name", ""),
        "market": row.get("market", ""),
        "hits_label": row.get("hits_label", ""),
        "hit_count": int(row.get("hit_count", 0) or 0),
        "base_price": row.get("現價"),
        "vol_ratio": row.get("量比"),
        "today_pct": row.get("今日%"),
        "invtrust_today": row.get("投信今日(張)"),
        "invtrust_5d": row.get("投信5日(張)"),
        "capital_ratio": row.get("投本比%"),
    }


def save_snapshot(combined_df: pd.DataFrame, snapshot_date: Optional[str] = None) -> dict:
    """把這次掃描結果存成 snapshot。回傳寫入結果。"""
    if combined_df is None or combined_df.empty:
        return {"ok": False, "msg": "無資料"}
    if snapshot_date is None:
        snapshot_date = dt.date.today().strftime("%Y-%m-%d")

    rows = [_row_from_combined(r, snapshot_date) for _, r in combined_df.iterrows()]
    new_df = pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS)

    # 1) 嘗試 Google Sheets
    sheet = _get_gsheet_client()
    if sheet is not None:
        try:
            existing = sheet.get_all_records()
            if not existing:
                # 寫表頭
                sheet.append_row(SNAPSHOT_COLUMNS)
            for _, r in new_df.iterrows():
                sheet.append_row([str(r[c]) if r[c] is not None else "" for c in SNAPSHOT_COLUMNS])
            return {"ok": True, "backend": "Google Sheets", "rows": len(new_df)}
        except Exception as e:
            # 失敗就退到 local
            pass

    # 2) Local CSV (Streamlit Cloud 會在 reboot 後消失，但 session 內可用)
    if os.path.exists(LOCAL_CSV):
        try:
            old = pd.read_csv(LOCAL_CSV)
            combined = pd.concat([old, new_df], ignore_index=True)
        except Exception:
            combined = new_df
    else:
        combined = new_df
    try:
        combined.to_csv(LOCAL_CSV, index=False)
        return {"ok": True, "backend": "Local CSV", "rows": len(new_df)}
    except Exception as e:
        return {"ok": False, "msg": f"寫入失敗: {e}"}


def load_history() -> pd.DataFrame:
    """讀全部 snapshot 歷史。優先 Google Sheets。"""
    sheet = _get_gsheet_client()
    if sheet is not None:
        try:
            data = sheet.get_all_records()
            if data:
                df = pd.DataFrame(data)
                return df
        except Exception:
            pass
    if os.path.exists(LOCAL_CSV):
        try:
            return pd.read_csv(LOCAL_CSV)
        except Exception:
            return pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    return pd.DataFrame(columns=SNAPSHOT_COLUMNS)


def import_history_from_csv(file_bytes: bytes) -> dict:
    """讓使用者從電腦上傳之前下載的 CSV 還原歷史.

    自動 fallback 多種 encoding (utf-8-sig / utf-8 / big5 / cp950 / gb18030)
    以容忍 Excel 另存的 CSV (Excel 中文預設 cp950 / Big5).
    """
    encodings_to_try = ["utf-8-sig", "utf-8", "big5", "cp950", "gb18030"]
    last_err = None
    for enc in encodings_to_try:
        try:
            df = pd.read_csv(io.BytesIO(file_bytes), encoding=enc)
            # 內部統一寫 utf-8 (沒 BOM)
            df.to_csv(LOCAL_CSV, index=False, encoding="utf-8")
            return {"ok": True, "rows": len(df), "encoding_detected": enc}
        except UnicodeDecodeError as e:
            last_err = e
            continue
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}
    return {"ok": False, "msg": f"所有 encoding 都失敗 (試過 {encodings_to_try}): {last_err}"}


# ---------------------------------------------------------------------------
# 表現追蹤：對每個 snapshot row 算「現在價格 vs 當時 base_price」
# ---------------------------------------------------------------------------
def evaluate_history_performance(history: pd.DataFrame, days_window: int = 30) -> pd.DataFrame:
    """
    對歷史 snapshot 計算後續表現。
    僅回顧最近 days_window 天內的 snapshot，避免重複呼叫 API。
    """
    if history is None or history.empty:
        return pd.DataFrame()
    df = history.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce")
    cutoff = pd.Timestamp(dt.date.today() - dt.timedelta(days=days_window))
    df = df[df["snapshot_date"] >= cutoff]
    if df.empty:
        return df

    # 為每個獨特 stock_id 抓最新價格 (一次抓一支)
    unique_ids = df["stock_id"].astype(str).unique().tolist()
    today = dt.date.today()
    end = today.strftime("%Y-%m-%d")
    start = (today - dt.timedelta(days=days_window + 10)).strftime("%Y-%m-%d")

    daily = ds._fetch_universe("TaiwanStockPrice", unique_ids, start, end, max_workers=5)
    if daily.empty:
        return df
    if "max" in daily.columns and "high" not in daily.columns:
        daily = daily.rename(columns={"max": "high", "min": "low"})

    # 每檔最新收盤
    last_close = (
        daily.sort_values("date")
        .groupby("stock_id")["close"]
        .last()
        .astype(float)
        .to_dict()
    )

    df["current_price"] = df["stock_id"].astype(str).map(last_close)
    df["base_price"] = pd.to_numeric(df["base_price"], errors="coerce")
    df["return%"] = ((df["current_price"] - df["base_price"]) / df["base_price"] * 100).round(2)
    df = df.sort_values(["snapshot_date", "stock_id"], ascending=[False, True]).reset_index(drop=True)

    # 額外算「持有日數」
    df["持有天"] = (pd.Timestamp(today) - df["snapshot_date"]).dt.days
    return df


def history_summary(perf_df: pd.DataFrame) -> dict:
    """整體勝率 / 平均報酬。"""
    if perf_df is None or perf_df.empty:
        return {}
    valid = perf_df[perf_df["return%"].notna()]
    if valid.empty:
        return {}
    return {
        "n_picks": len(valid),
        "win_rate": round(float((valid["return%"] > 0).mean() * 100), 1),
        "avg_return": round(float(valid["return%"].mean()), 2),
        "median_return": round(float(valid["return%"].median()), 2),
        "best": round(float(valid["return%"].max()), 2),
        "worst": round(float(valid["return%"].min()), 2),
    }


def csv_for_download() -> bytes:
    """產生目前 history CSV bytes 供下載.

    用 utf-8-sig (含 BOM), Excel 開繁中 CSV 才不會亂碼.
    """
    df = load_history()
    return df.to_csv(index=False).encode("utf-8-sig")
