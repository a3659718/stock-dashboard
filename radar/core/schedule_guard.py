"""schedule_guard.py — 排程「遲到太久」守衛。

問題 (2026-11 實測): GitHub Actions 的 `schedule:` 在這個 repo 常態延遲 4~6.5 小時,
例如 08:02 的晨報實際 14:30 才開跑、08:17 的盤前推播 14:57 才送出。延遲發生在
GitHub 的排程佇列, yml 怎麼調分鐘都救不了。後果不只是「晚」, 而是「錯」:
盤前 / 開盤選股在收盤後才送到, 內容仍寫著「開盤前」, 會誤導決策。

做法:
  - 只管 GitHub cron 觸發的 run (GITHUB_EVENT_NAME == "schedule")。
    workflow_dispatch (手動、本機工作排程器、cron-job.org 等外部準時觸發) 一律放行。
  - 由 workflow 把觸發的 cron 字串放進 SCHEDULE_CRON 環境變數, 這裡反推「原本該跑的時間」,
    算出遲到幾分鐘; 超過該 slot 的容忍上限就跳過 (exit 0, 不推播)。
  - 只套在「有時效性」的 slot; 盤後總結、心跳、週末摘要這類晚到仍有價值的不擋。
  - 設環境變數 STALE_GUARD=0 可整個關掉。

真正的解法是外部準時觸發 (見 README「推播延遲」一節), 這個守衛是防止「過時資訊」的保險。
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Optional

# slot → 可容忍的最大遲到分鐘數 (超過就不推)。依「這則推播何時會失去意義」訂。
STALE_LIMIT_MIN = {
    "pre_market_815": 40,    # 08:17 → 08:57 前送到才趕得上 09:00 開盤
    "pre_market_830": 30,    # 08:33 → 09:03
    "morning_brief": 55,     # 08:02 → 08:57
    "morning_recap": 90,     # 07:32 → 09:02
    "tw_open": 60,           # 09:32 → 10:32
    "tw_mid": 90,            # 11:02 → 12:32 (13:30 收盤前)
    "us_emerging": 150,      # 美股開盤前 ~2.5hr → 開盤前
    "us_buy_picks": 55,      # 美股開盤前 ~58 分 → 開盤前
    "us_open": 120,          # 10:02 ET → 12:02 ET
    "us_mid": 180,           # 11:32 ET → 14:32 ET (收盤前)
}


def _field_match(field: str, value: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            return True
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part == "*":
            lo, hi = 0, 10 ** 6
        elif "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(part)
        if lo <= value <= hi and (value - lo) % step == 0:
            return True
    return False


def cron_matches(cron: str, t: dt.datetime) -> bool:
    """支援 GitHub Actions 用到的 5 欄 cron (數字 / 逗號 / 範圍 / * / 步進), 時間為 UTC。"""
    f = cron.split()
    if len(f) != 5:
        raise ValueError(f"不是 5 欄 cron: {cron!r}")
    minute, hour, dom, month, dow = f
    cron_dow = (t.weekday() + 1) % 7  # Python: 週一=0; cron: 週日=0
    return (_field_match(minute, t.minute) and _field_match(hour, t.hour)
            and _field_match(dom, t.day) and _field_match(month, t.month)
            and (_field_match(dow, cron_dow) or (cron_dow == 0 and _field_match(dow, 7))))


def intended_fire_time(cron: str, now_utc: dt.datetime,
                       lookback_hours: int = 30) -> Optional[dt.datetime]:
    """cron 在 now 之前 (含) 最近一次應該觸發的時間; lookback 內找不到回 None。"""
    t = now_utc.replace(second=0, microsecond=0)
    for _ in range(lookback_hours * 60 + 1):
        if cron_matches(cron, t):
            return t
        t -= dt.timedelta(minutes=1)
    return None


def lateness_minutes(cron: str, now_utc: Optional[dt.datetime] = None) -> Optional[float]:
    now_utc = now_utc or dt.datetime.utcnow()
    fire = intended_fire_time(cron, now_utc)
    if fire is None:
        return None
    return (now_utc - fire).total_seconds() / 60


def intended_market_date(market: str, now_utc: Optional[dt.datetime] = None,
                         env: Optional[dict] = None) -> Optional[dt.date]:
    """GitHub cron run 「原本該跑的那一刻」在該市場的當地日期; 非 cron run 回 None。

    為什麼: 假日檢查用「實際開跑時間」的日期。cron 延遲數小時時會跨過當地午夜 ——
    例如 22:32 TPE 的 holiday_news 晚 2 小時跑就被當成隔天、週五 18:02 ET 的 us_close
    晚 6 小時就被當成週六休市而整個跳過。改用排定時間的日期判斷。
    """
    env = os.environ if env is None else env
    try:
        if env.get("GITHUB_EVENT_NAME") != "schedule":
            return None
        cron = (env.get("SCHEDULE_CRON") or "").strip()
        if not cron:
            return None
        fire = intended_fire_time(cron, now_utc or dt.datetime.utcnow())
        if fire is None:
            return None
        if market.upper() == "US":
            from zoneinfo import ZoneInfo
            return fire.replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
        return (fire + dt.timedelta(hours=8)).date()  # TW / JP / KR 皆以台北日期為準
    except Exception as e:
        print(f"[schedule_guard] intended_market_date failed (non-fatal): {e}", flush=True)
        return None


def check_stale(slot: str, now_utc: Optional[dt.datetime] = None,
                env: Optional[dict] = None) -> Optional[str]:
    """回傳「跳過原因」字串 (該跳過時) 或 None (照常執行)。不會 raise。"""
    env = os.environ if env is None else env
    try:
        if str(env.get("STALE_GUARD", "1")).strip() == "0":
            return None
        if env.get("GITHUB_EVENT_NAME") != "schedule":
            return None
        limit = STALE_LIMIT_MIN.get(slot)
        cron = (env.get("SCHEDULE_CRON") or "").strip()
        if limit is None or not cron:
            return None
        late = lateness_minutes(cron, now_utc)
        if late is None or late <= limit:
            return None
        return (f"GitHub 排程延遲 {late:.0f} 分鐘 (cron '{cron}'), 超過 {slot} 的容忍上限 "
                f"{limit} 分 → 內容已過時, 本次不推播。請改用外部準時觸發 (README「推播延遲」)。")
    except Exception as e:  # 守衛本身出錯絕不能擋推播
        print(f"[schedule_guard] check failed (non-fatal, 照常執行): {e}", flush=True)
        return None
