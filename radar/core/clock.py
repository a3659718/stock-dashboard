"""
clock.py
全專案共用的「市場時區」時鐘 (audit #16)。

背景: GitHub Actions 與 Streamlit Cloud 都跑在 UTC。`dt.date.today()` /
naive `dt.datetime.now()` 拿到的是 UTC 日期/時間, 台北 00:00-08:00 這段會
「還停在昨天」—— 顯示日期、持有天數、每日狀態換日、FinMind 查詢區間都會錯一天。

規則:
  - 台股 / 使用者看到的日期 / 每日狀態換日  → tpe_today() / tpe_now()
  - 美股交易日 (美股收盤總結、美股假日、財報日、美股盤中狀態) → et_today() / et_now()
  - 依市場切換 → market_today(market)  ("US" → 美東, 其餘 TW/JP/KR → 台北)
  - 刻意要 UTC (跟 utcnow 時間戳比較) → utc_now(), 並在呼叫端註明

tpe_now() / et_now() 回傳 **帶 tzinfo 的 aware datetime**。若呼叫端要跟舊有的
naive datetime 相減/比較, 請用 tpe_now_naive() / et_now_naive()。

測試: 改 `clock._utc_now` (monkeypatch) 即可固定全部函式的「現在」。
"""

from __future__ import annotations

import datetime as dt

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover — Python < 3.9
    ZoneInfo = None  # type: ignore

_UTC = dt.timezone.utc
_TPE_FIXED = dt.timezone(dt.timedelta(hours=8), "Asia/Taipei")  # 台灣無夏令時間, 固定 +8


def _load_zone(name: str):
    """載入 IANA 時區; tzdata 缺失時回傳 None (呼叫端走 fallback)。"""
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


TPE_TZ = _load_zone("Asia/Taipei") or _TPE_FIXED
ET_TZ = _load_zone("America/New_York")  # None → 用 _et_offset_fallback()


def _utc_now() -> dt.datetime:
    """唯一的「現在」來源 (aware UTC)。測試 monkeypatch 這支即可。"""
    return dt.datetime.now(_UTC)


def _as_utc(now: dt.datetime | None) -> dt.datetime:
    """把傳入的時間正規化成 aware UTC; naive 一律視為 UTC (跟 utcnow() 慣例一致)。"""
    if now is None:
        now = _utc_now()
    if now.tzinfo is None:
        return now.replace(tzinfo=_UTC)
    return now.astimezone(_UTC)


def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    first_sun = d + dt.timedelta(days=(6 - d.weekday()) % 7)
    return first_sun + dt.timedelta(weeks=n - 1)


def _et_offset_fallback(now_utc: dt.datetime) -> dt.timezone:
    """tzdata 不可用時的美東時差: 3 月第二個週日 02:00 ~ 11 月第一個週日 02:00 為夏令 (UTC-4)。"""
    y = now_utc.year
    # 夏令起: 當地 02:00 EST = 07:00 UTC; 夏令止: 當地 02:00 EDT = 06:00 UTC
    start = dt.datetime.combine(_nth_sunday(y, 3, 2), dt.time(7), _UTC)
    end = dt.datetime.combine(_nth_sunday(y, 11, 1), dt.time(6), _UTC)
    if start <= now_utc < end:
        return dt.timezone(dt.timedelta(hours=-4), "EDT")
    return dt.timezone(dt.timedelta(hours=-5), "EST")


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------
def utc_now(now: dt.datetime | None = None) -> dt.datetime:
    """aware UTC 現在時間 (刻意要 UTC 的地方用這個, 讓意圖明確)。"""
    return _as_utc(now)


def tpe_now(now: dt.datetime | None = None) -> dt.datetime:
    """台北現在時間 (aware, UTC+8)。"""
    return _as_utc(now).astimezone(TPE_TZ)


def tpe_today(now: dt.datetime | None = None) -> dt.date:
    """台北今天日期 — 台股 / 使用者顯示 / 每日狀態換日一律用這個。"""
    return tpe_now(now).date()


def et_now(now: dt.datetime | None = None) -> dt.datetime:
    """美東現在時間 (aware, 自動處理夏令/冬令)。"""
    u = _as_utc(now)
    return u.astimezone(ET_TZ if ET_TZ is not None else _et_offset_fallback(u))


def et_today(now: dt.datetime | None = None) -> dt.date:
    """美東今天日期 — 美股交易日相關邏輯 (收盤總結、假日、財報日) 用這個。"""
    return et_now(now).date()


def tpe_now_naive(now: dt.datetime | None = None) -> dt.datetime:
    """台北現在時間 (naive) — 給要跟舊有 naive datetime 相減/比較的呼叫端。"""
    return tpe_now(now).replace(tzinfo=None)


def et_now_naive(now: dt.datetime | None = None) -> dt.datetime:
    """美東現在時間 (naive)。"""
    return et_now(now).replace(tzinfo=None)


def market_today(market: str, now: dt.datetime | None = None) -> dt.date:
    """依市場選日期基準: "US" → 美東日期, 其餘 (TW/JP/KR/None) → 台北日期。"""
    return et_today(now) if str(market or "").upper() == "US" else tpe_today(now)


def tpe_today_str(now: dt.datetime | None = None) -> str:
    """台北今天 'YYYY-MM-DD' (每日狀態 key 最常用的格式)。"""
    return tpe_today(now).strftime("%Y-%m-%d")


def et_today_str(now: dt.datetime | None = None) -> str:
    """美東今天 'YYYY-MM-DD'。"""
    return et_today(now).strftime("%Y-%m-%d")


def tpe_date_from_ts(ts: float) -> dt.date:
    """Unix timestamp → 台北日期 (取代 naive dt.datetime.fromtimestamp, 那個會用伺服器時區)。"""
    return dt.datetime.fromtimestamp(float(ts), _UTC).astimezone(TPE_TZ).date()


__all__ = [
    "TPE_TZ", "ET_TZ",
    "utc_now", "tpe_now", "tpe_today", "et_now", "et_today",
    "tpe_now_naive", "et_now_naive", "market_today",
    "tpe_today_str", "et_today_str", "tpe_date_from_ts",
]
