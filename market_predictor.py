"""
market_predictor.py
大盤盤型預測 (開高走高 / 開高走低 / 開低走高 / 開低走低 / 平盤震盪)。

訊號（開盤後 30 分鐘可取得）：
  1. Gap %      = (今日開盤 - 昨日收盤) / 昨日收盤
  2. Drift %    = (目前價 - 今日開盤) / 今日開盤
  3. Range %    = (今日高 - 今日低) / 今日開盤
  4. Vol Ratio  = 今日前 30 分鐘量 / 過去 5 日平均前 30 分鐘量

準確率追蹤：
  - 每次預測存 Google Sheets (若有設) 或本機 CSV
  - 每次新預測時，評估「過去 1-30 天有預測但還沒比對的紀錄」是否正確
  - 顯示 rolling 30 天準確率
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

import data_sources as ds


PREDICTIONS_LOCAL_CSV = "predictions_history.csv"
PREDICTION_COLUMNS = [
    "date", "market", "predicted_pattern", "predicted_bias", "confidence",
    "open", "current", "prev_close", "gap_pct", "drift_pct", "range_pct", "vol_ratio",
    "actual_pattern", "actual_close", "actual_high", "actual_low", "evaluated", "correct",
]


# ---------------------------------------------------------------------------
# 共用 pattern 判斷邏輯
# ---------------------------------------------------------------------------
def _classify_pattern(gap_pct: float, drift_pct: float, vol_ratio: float = 1.0) -> Dict:
    g_strong = abs(gap_pct) >= 0.5
    d_strong = abs(drift_pct) >= 0.3

    if gap_pct >= 0.3 and drift_pct >= 0.2:
        pattern = "開高走高"
        bias = "看多"
        confidence = "高" if (g_strong and d_strong and vol_ratio > 1.2) else "中"
        explanation = "開盤跳空向上後持續走強，買盤積極，整日易維持強勢。"
    elif gap_pct >= 0.3 and drift_pct <= -0.2:
        pattern = "開高走低"
        bias = "偏空"
        confidence = "高" if (g_strong and d_strong) else "中"
        explanation = "開高被賣壓出貨，多單套牢，注意尾盤可能再殺。"
    elif gap_pct <= -0.3 and drift_pct >= 0.2:
        pattern = "開低走高"
        bias = "偏多"
        confidence = "中"
        explanation = "開低後出現買盤承接，可能 V 型反彈，但要看是否突破前一日收盤。"
    elif gap_pct <= -0.3 and drift_pct <= -0.2:
        pattern = "開低走低"
        bias = "看空"
        confidence = "高" if (g_strong and d_strong and vol_ratio > 1.2) else "中"
        explanation = "開低後賣壓持續，恐慌或續弱，整日易維持弱勢。"
    else:
        pattern = "平盤震盪"
        bias = "中性"
        confidence = "低"
        explanation = "缺乏明顯方向，可能是觀望或盤整，等待午盤後方向。"

    return {
        "pattern": pattern,
        "bias": bias,
        "confidence": confidence,
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# 從 yfinance intraday 抽訊號
# ---------------------------------------------------------------------------
def _extract_signals(symbol: str) -> Optional[Dict]:
    """從 yfinance 5 分鐘 K 抽出 gap / drift / vol_ratio 等訊號。"""
    df = ds.fetch_yf_history(symbol, period="5d", interval="5m")
    if df.empty:
        return None

    # 尋找日期欄位 (yfinance 在不同模式下可能叫 Datetime 或 Date)
    date_col = None
    for c in ["Datetime", "Date"]:
        if c in df.columns:
            date_col = c
            break
    if date_col is None:
        return None

    df = df.copy()
    df["dt"] = pd.to_datetime(df[date_col])
    df["d"] = df["dt"].dt.date

    today = df["d"].max()
    today_bars = df[df["d"] == today].sort_values("dt")
    prev_bars = df[df["d"] < today].sort_values("dt")
    if today_bars.empty or prev_bars.empty:
        return None

    today_open = float(today_bars.iloc[0]["Open"])
    current_price = float(today_bars.iloc[-1]["Close"])
    today_high = float(today_bars["High"].max())
    today_low = float(today_bars["Low"].min())
    prev_close = float(prev_bars.iloc[-1]["Close"])

    gap_pct = (today_open - prev_close) / prev_close * 100 if prev_close else 0
    drift_pct = (current_price - today_open) / today_open * 100 if today_open else 0
    range_pct = (today_high - today_low) / today_open * 100 if today_open else 0

    today_vol = float(today_bars["Volume"].sum())
    # 用昨日跟前天的「前 30 分鐘量」平均當基準 (6 根 5 分鐘 = 30 分鐘)
    avg_first_30min_vol = 0.0
    distinct_prev = sorted(prev_bars["d"].unique(), reverse=True)[:5]
    samples = []
    for d in distinct_prev:
        first_30 = prev_bars[prev_bars["d"] == d].head(6)
        samples.append(float(first_30["Volume"].sum()))
    if samples:
        avg_first_30min_vol = sum(samples) / len(samples)
    vol_ratio = today_vol / avg_first_30min_vol if avg_first_30min_vol > 0 else 1.0

    return {
        "today": today,
        "open": today_open,
        "current": current_price,
        "prev_close": prev_close,
        "high": today_high,
        "low": today_low,
        "gap_pct": gap_pct,
        "drift_pct": drift_pct,
        "range_pct": range_pct,
        "vol_ratio": vol_ratio,
    }


# ---------------------------------------------------------------------------
# 對外: 預測 TW / US 大盤
# ---------------------------------------------------------------------------
def predict_tw_pattern() -> Dict:
    sig = _extract_signals("^TWII")
    if not sig:
        return {"error": "無法取得加權指數即時資料 (盤前/休市?)"}
    pat = _classify_pattern(sig["gap_pct"], sig["drift_pct"], sig["vol_ratio"])
    return {
        **pat,
        **{k: round(v, 2) if isinstance(v, float) else v for k, v in sig.items()},
        "market": "TW",
    }


def predict_us_pattern() -> Dict:
    """美股用 SPY (S&P500 ETF) 當大盤代理。"""
    sig = _extract_signals("SPY")
    if not sig:
        return {"error": "無法取得 SPY 即時資料 (盤前/休市?)"}
    pat = _classify_pattern(sig["gap_pct"], sig["drift_pct"], sig["vol_ratio"])
    return {
        **pat,
        **{k: round(v, 2) if isinstance(v, float) else v for k, v in sig.items()},
        "market": "US",
    }


# ---------------------------------------------------------------------------
# 持久化 (Google Sheets / Local CSV)
# ---------------------------------------------------------------------------
def _get_predictions_sheet():
    """嘗試取得專門存 predictions 的 Google Sheets worksheet (sheet2)."""
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
        # 用第二個 worksheet 存 predictions，避免跟 tracker 撞
        try:
            ws = wb.worksheet("predictions")
        except Exception:
            ws = wb.add_worksheet(title="predictions", rows=1000, cols=len(PREDICTION_COLUMNS))
            ws.append_row(PREDICTION_COLUMNS)
        return ws
    except Exception:
        return None


def save_prediction(prediction: Dict) -> Dict:
    """把預測寫入 storage。回傳 {ok, backend, msg}."""
    if prediction.get("error"):
        return {"ok": False, "msg": prediction["error"]}

    today = (prediction.get("today") or dt.date.today())
    if isinstance(today, dt.date):
        today_str = today.strftime("%Y-%m-%d")
    else:
        today_str = str(today)
    row = {
        "date": today_str,
        "market": prediction.get("market", "TW"),
        "predicted_pattern": prediction.get("pattern", ""),
        "predicted_bias": prediction.get("bias", ""),
        "confidence": prediction.get("confidence", ""),
        "open": prediction.get("open"),
        "current": prediction.get("current"),
        "prev_close": prediction.get("prev_close"),
        "gap_pct": prediction.get("gap_pct"),
        "drift_pct": prediction.get("drift_pct"),
        "range_pct": prediction.get("range_pct"),
        "vol_ratio": prediction.get("vol_ratio"),
        "actual_pattern": "", "actual_close": "", "actual_high": "", "actual_low": "",
        "evaluated": False, "correct": "",
    }

    sheet = _get_predictions_sheet()
    if sheet is not None:
        try:
            existing = sheet.get_all_records()
            # 同 (date, market) 已存則跳過
            for r in existing:
                if str(r.get("date")) == today_str and str(r.get("market")) == row["market"]:
                    return {"ok": True, "backend": "Google Sheets", "msg": "今日已記錄"}
            sheet.append_row([str(row[c]) if row[c] not in (None, "") else "" for c in PREDICTION_COLUMNS])
            return {"ok": True, "backend": "Google Sheets"}
        except Exception as e:
            pass

    # Fallback: local CSV
    new_df = pd.DataFrame([row], columns=PREDICTION_COLUMNS)
    if os.path.exists(PREDICTIONS_LOCAL_CSV):
        try:
            old = pd.read_csv(PREDICTIONS_LOCAL_CSV)
            if not old.empty:
                dup = (old["date"].astype(str) == today_str) & (old["market"] == row["market"])
                if dup.any():
                    return {"ok": True, "backend": "Local CSV", "msg": "今日已記錄"}
            df = pd.concat([old, new_df], ignore_index=True)
        except Exception:
            df = new_df
    else:
        df = new_df
    try:
        df.to_csv(PREDICTIONS_LOCAL_CSV, index=False)
        return {"ok": True, "backend": "Local CSV"}
    except Exception as e:
        return {"ok": False, "msg": f"寫入失敗: {e}"}


def load_predictions() -> pd.DataFrame:
    sheet = _get_predictions_sheet()
    if sheet is not None:
        try:
            data = sheet.get_all_records()
            if data:
                return pd.DataFrame(data)
        except Exception:
            pass
    if os.path.exists(PREDICTIONS_LOCAL_CSV):
        try:
            return pd.read_csv(PREDICTIONS_LOCAL_CSV)
        except Exception:
            return pd.DataFrame(columns=PREDICTION_COLUMNS)
    return pd.DataFrame(columns=PREDICTION_COLUMNS)


# ---------------------------------------------------------------------------
# 評估過去預測是否正確
# ---------------------------------------------------------------------------
def _actual_day_pattern(symbol: str, target_date: dt.date) -> Optional[Dict]:
    """根據 yfinance 日線抓那一天的 OHLC，反推實際 pattern。"""
    df = ds.fetch_yf_history(symbol, period="2mo", interval="1d")
    if df.empty:
        return None
    date_col = None
    for c in ["Date", "Datetime"]:
        if c in df.columns:
            date_col = c
            break
    if date_col is None:
        return None
    df = df.copy()
    df["d"] = pd.to_datetime(df[date_col]).dt.date
    target_row = df[df["d"] == target_date]
    if target_row.empty:
        return None
    prev_row = df[df["d"] < target_date].sort_values("d").tail(1)
    if prev_row.empty:
        return None

    o = float(target_row.iloc[0]["Open"])
    h = float(target_row.iloc[0]["High"])
    l = float(target_row.iloc[0]["Low"])
    c = float(target_row.iloc[0]["Close"])
    pc = float(prev_row.iloc[0]["Close"])

    gap = (o - pc) / pc * 100 if pc else 0
    drift_close = (c - o) / o * 100 if o else 0  # 整日從開盤到收盤
    info = _classify_pattern(gap, drift_close, vol_ratio=1.0)
    return {
        "actual_pattern": info["pattern"],
        "actual_close": round(c, 2),
        "actual_high": round(h, 2),
        "actual_low": round(l, 2),
        "actual_drift_pct": round(drift_close, 2),
    }


def evaluate_pending_predictions(symbol_for_market: Optional[Dict] = None) -> int:
    """評估有預測但 evaluated=False 的紀錄。回傳新評估的筆數。"""
    if symbol_for_market is None:
        symbol_for_market = {"TW": "^TWII", "US": "SPY"}

    df = load_predictions()
    if df.empty:
        return 0
    if "evaluated" not in df.columns:
        return 0

    df_eval = df.copy()
    df_eval["evaluated"] = df_eval["evaluated"].astype(str).str.lower().isin(["true", "1", "yes"])

    # 強制這些欄位為 object 型別 (避免 pandas LossySetitemError)
    for _col in ["actual_pattern", "actual_close", "actual_high", "actual_low", "correct"]:
        if _col in df_eval.columns:
            df_eval[_col] = df_eval[_col].astype(object)
        else:
            df_eval[_col] = ""

    pending = df_eval[~df_eval["evaluated"]]
    if pending.empty:
        return 0

    today = dt.date.today()
    updates = 0
    for idx, row in pending.iterrows():
        pred_date_str = str(row["date"])
        try:
            pred_date = dt.datetime.strptime(pred_date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        # 必須是「過去」的日期才能評估
        if pred_date >= today:
            continue
        symbol = symbol_for_market.get(row["market"], "^TWII")
        actual = _actual_day_pattern(symbol, pred_date)
        if not actual:
            continue
        df_eval.at[idx, "actual_pattern"] = actual["actual_pattern"]
        df_eval.at[idx, "actual_close"] = actual["actual_close"]
        df_eval.at[idx, "actual_high"] = actual["actual_high"]
        df_eval.at[idx, "actual_low"] = actual["actual_low"]
        df_eval.at[idx, "evaluated"] = True
        df_eval.at[idx, "correct"] = (str(row["predicted_pattern"]) == actual["actual_pattern"])
        updates += 1

    if updates == 0:
        return 0

    # 寫回
    sheet = _get_predictions_sheet()
    if sheet is not None:
        try:
            sheet.clear()
            sheet.append_row(PREDICTION_COLUMNS)
            for _, r in df_eval.iterrows():
                sheet.append_row([str(r[c]) if r[c] not in (None, "") else "" for c in PREDICTION_COLUMNS])
            return updates
        except Exception:
            pass
    try:
        df_eval[PREDICTION_COLUMNS].to_csv(PREDICTIONS_LOCAL_CSV, index=False)
    except Exception:
        pass
    return updates


def accuracy_stats(market: str = "TW", lookback_days: int = 30) -> Dict:
    """rolling N 天準確率。"""
    df = load_predictions()
    if df.empty:
        return {}
    df = df.copy()
    df = df[df["market"] == market]
    if df.empty:
        return {}
    df["evaluated"] = df["evaluated"].astype(str).str.lower().isin(["true", "1", "yes"])
    df = df[df["evaluated"]]
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    cutoff = pd.Timestamp(dt.date.today() - dt.timedelta(days=lookback_days))
    df = df[df["date"] >= cutoff]
    if df.empty:
        return {}
    df["correct"] = df["correct"].astype(str).str.lower().isin(["true", "1", "yes"])
    n = len(df)
    correct = int(df["correct"].sum())
    return {
        "market": market,
        "n": n,
        "correct": correct,
        "accuracy_pct": round(correct / n * 100, 1),
    }
