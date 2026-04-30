"""
holiday_check.py
台股 / 美股 / 日股 / 韓股假日檢測，避免假日空跑推播。

自動化策略：
  1. 優先使用 pandas_market_calendars (社群維護，自動年度更新)
  2. 該套件失敗 / 不可用時，fallback 到寫死的清單

使用方式:
  if holiday_check.is_market_closed_today("TW"):
      print("台股休市，跳過推播")
      sys.exit(0)
"""

from __future__ import annotations

import datetime as dt
import functools
from typing import Optional, Set

# ---------------------------------------------------------------------------
# 嘗試載入 pandas_market_calendars (自動化來源)
# ---------------------------------------------------------------------------
try:
    import pandas_market_calendars as mcal  # type: ignore
    import pandas as pd  # noqa
    PMC_AVAILABLE = True
except Exception:
    PMC_AVAILABLE = False


# pandas_market_calendars 的交易所 ISO 代碼
EXCHANGE_MAP = {
    "TW": "XTAI",  # Taiwan Stock Exchange (TWSE)
    "US": "NYSE",  # 紐約證交所
    "JP": "XTKS",  # 東京證交所 (JPX)
    "KR": "XKRX",  # 韓國交易所 (KRX)
}


# ---------------------------------------------------------------------------
# Fallback 假日清單 (pmc 失敗時用)
# 萬一 pmc 故障 / 沒裝，用這份保底
# ---------------------------------------------------------------------------
TW_HOLIDAYS_FALLBACK: Set[str] = {
    # 2026
    "2026-01-01",
    "2026-02-13", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-27", "2026-02-28",
    "2026-04-03", "2026-04-06",
    "2026-05-01",
    "2026-06-19",
    "2026-09-25",
    "2026-10-09",
}
US_HOLIDAYS_FALLBACK: Set[str] = {
    "2026-01-01", "2026-01-19", "2026-02-16",
    "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}
JP_HOLIDAYS_FALLBACK: Set[str] = {
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-05-04", "2026-05-05",
    "2026-12-31",
}
KR_HOLIDAYS_FALLBACK: Set[str] = {
    "2026-01-01", "2026-02-16", "2026-02-17",
    "2026-05-05", "2026-06-06",
    "2026-09-24", "2026-09-25",
    "2026-12-25",
}

_FALLBACK_MAP = {
    "TW": TW_HOLIDAYS_FALLBACK,
    "US": US_HOLIDAYS_FALLBACK,
    "JP": JP_HOLIDAYS_FALLBACK,
    "KR": KR_HOLIDAYS_FALLBACK,
}


# ---------------------------------------------------------------------------
# 自動化檢測 (有 cache，避免重複呼叫)
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=512)
def _is_open_via_pmc(exchange_code: str, date_str: str) -> Optional[bool]:
    """用 pandas_market_calendars 查指定日期該交易所是否開盤.
    回傳 True/False，無法判斷則 None.
    """
    if not PMC_AVAILABLE:
        return None
    try:
        import pandas as pd
        cal = mcal.get_calendar(exchange_code)
        target = pd.Timestamp(date_str)
        schedule = cal.schedule(start_date=target, end_date=target)
        return not schedule.empty
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 對外 API
# ---------------------------------------------------------------------------
def is_market_closed_today(market: str, today: Optional[dt.date] = None) -> bool:
    """檢查當日 market 是否休市.
    market: 'TW' | 'US' | 'JP' | 'KR'
    """
    if today is None:
        today = dt.date.today()

    # 1. 週末直接 True
    if today.weekday() >= 5:
        return True

    market_upper = market.upper()
    date_str = today.strftime("%Y-%m-%d")

    # 2. 優先 pandas_market_calendars 自動判斷
    exchange = EXCHANGE_MAP.get(market_upper)
    if exchange:
        result = _is_open_via_pmc(exchange, date_str)
        if result is not None:
            return not result  # pmc 說「open=True」→ 「closed=False」

    # 3. Fallback 到寫死清單
    return date_str in _FALLBACK_MAP.get(market_upper, set())


def market_status_summary(today: Optional[dt.date] = None) -> dict:
    """回傳所有市場的開休市狀態 (debug 用)."""
    if today is None:
        today = dt.date.today()
    return {
        market: ("休市" if is_market_closed_today(market, today) else "開盤")
        for market in EXCHANGE_MAP.keys()
    }


def get_data_source_info() -> str:
    """回傳目前實際使用的資料源 (debug 用)."""
    return (
        f"pandas_market_calendars: {'✓ 自動化' if PMC_AVAILABLE else '✗ 未安裝 → 使用寫死 fallback'}"
    )
