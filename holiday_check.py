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
    # 2026 (台灣證交所)
    "2026-01-01",                                                    # 元旦
    "2026-02-13", "2026-02-16", "2026-02-17", "2026-02-18",
    "2026-02-19", "2026-02-20", "2026-02-27", "2026-02-28",          # 春節+228連假
    "2026-04-03", "2026-04-06",                                       # 兒童節+清明
    "2026-05-01",                                                     # 勞動節
    "2026-06-19",                                                     # 端午節
    "2026-09-25",                                                     # 中秋節
    "2026-10-09",                                                     # 雙十節
    # 2027
    "2027-01-01",
    "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11",
    "2027-02-12", "2027-02-15", "2027-02-26",
    "2027-04-02", "2027-04-05",
    "2027-05-01",  # ← 這就是去年同 user 抱怨「5/1 還跳警報」的 root cause
    "2027-06-09",
    "2027-09-15",
    "2027-10-08", "2027-10-11",
}
US_HOLIDAYS_FALLBACK: Set[str] = {
    # 2026 (NYSE)
    "2026-01-01",   # New Year
    "2026-01-19",   # MLK Day
    "2026-02-16",   # Presidents Day
    "2026-04-03",   # Good Friday
    "2026-05-25",   # Memorial Day
    "2026-06-19",   # Juneteenth
    "2026-07-03",   # Independence Day (observed)
    "2026-09-07",   # Labor Day
    "2026-11-26",   # Thanksgiving
    "2026-12-25",   # Christmas
    # 2027
    "2027-01-01",
    "2027-01-18",
    "2027-02-15",
    "2027-03-26",
    "2027-05-31",
    "2027-06-18",
    "2027-07-05",
    "2027-09-06",
    "2027-11-25",
    "2027-12-24",
}
JP_HOLIDAYS_FALLBACK: Set[str] = {
    # 2026 (東証 — JPX)
    "2026-01-01", "2026-01-02", "2026-01-03",   # 元旦休市
    "2026-01-12",                                # 成人之日
    "2026-02-11",                                # 建國記念日
    "2026-02-23",                                # 天皇誕生日
    "2026-03-20",                                # 春分之日
    "2026-04-29",                                # 昭和之日
    "2026-05-04", "2026-05-05", "2026-05-06",    # 黃金週 (5/3 落在週日)
    "2026-07-20",                                # 海之日
    "2026-08-11",                                # 山之日
    "2026-09-21", "2026-09-22", "2026-09-23",    # 敬老/秋分
    "2026-11-03",                                # 文化之日
    "2026-11-23",                                # 勤労感謝
    "2026-12-31",                                # 大納會 (年終休市)
}
KR_HOLIDAYS_FALLBACK: Set[str] = {
    # 2026 (KRX 韓國交易所)
    "2026-01-01",                                # 新年
    "2026-02-16", "2026-02-17", "2026-02-18",    # 春節 (설날)
    "2026-03-02",                                # 三一節 (3/1 落在週日 → 振替)
    "2026-05-01",                                # 勞動節 ← 之前漏了
    "2026-05-05",                                # 어린이날
    "2026-05-25",                                # 釋迦誕辰 (依國曆變動)
    "2026-06-03",                                # 地方選舉日 (4 年一次)
    "2026-06-06",                                # 顯忠日
    "2026-08-17",                                # 光復節振替
    "2026-09-24", "2026-09-25", "2026-09-26",    # 추석
    "2026-10-05",                                # 開天節振替
    "2026-10-09",                                # 한글날
    "2026-12-25",                                # 聖誕節
    "2026-12-31",                                # 年終休市
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
