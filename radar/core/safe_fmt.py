"""
safe_fmt.py — 全專案共用的「安全格式化」工具.

動機
----
專案裡反覆出現 `f"{x.get('k'):.2f}"` 這種寫法, 一旦欄位是 None / NaN / 字串 '—',
`:.2f` 就丟 TypeError, 在 TG 推播會炸掉整封、在 Streamlit 會讓整個分頁變空白。
這個 bug 類別已在多個檔案各別修過好幾次。改用這裡的 helper 統一處理, 就能一勞永逸:
任何「壞值」一律安全地顯示成 dash，永不丟例外。

用法
----
    from radar.core.safe_fmt import fmt_num, fmt_pct, fmt_price, is_num

    fmt_num(p.get("vol_ratio"))            # 12.34  / —
    fmt_pct(p.get("today_pct"))            # +1.20% / —
    fmt_price(p.get("current"), "$")       # $23.10 / —
    fmt_num(None)                          # —
"""
from __future__ import annotations

from typing import Any

DASH = "—"


def is_num(v: Any) -> bool:
    """v 是否為「可格式化的有限數字」(排除 None / NaN / inf / bool / 字串)."""
    if v is None or isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        try:
            f = float(v)
            return f == f and f not in (float("inf"), float("-inf"))  # f==f 排除 NaN
        except Exception:
            return False
    return False


def fmt_num(v: Any, decimals: int = 2, dash: str = DASH, thousands: bool = False) -> str:
    """數字 → 字串; 壞值回 dash. e.g. fmt_num(1234.5, thousands=True) -> '1,234.50'."""
    if not is_num(v):
        return dash
    spec = f",.{decimals}f" if thousands else f".{decimals}f"
    return format(float(v), spec)


def fmt_pct(v: Any, decimals: int = 2, sign: bool = True, dash: str = DASH,
            suffix: str = "%") -> str:
    """百分比 → 字串; 壞值回 dash. e.g. fmt_pct(1.2) -> '+1.20%'."""
    if not is_num(v):
        return dash
    spec = f"+.{decimals}f" if sign else f".{decimals}f"
    return format(float(v), spec) + suffix


def fmt_price(v: Any, prefix: str = "", decimals: int = 2, dash: str = DASH,
              suffix: str = "") -> str:
    """價格 → 字串; 壞值回 dash. e.g. fmt_price(23.1, '$') -> '$23.10'."""
    if not is_num(v):
        return dash
    return f"{prefix}{format(float(v), f'.{decimals}f')}{suffix}"


def fmt_int(v: Any, dash: str = DASH, thousands: bool = True) -> str:
    """整數 → 字串; 壞值回 dash."""
    if not is_num(v):
        return dash
    return format(int(round(float(v))), "," if thousands else "d")


def first_num(*vals: Any, default: Any = None):
    """回第一個是有效數字的值; 都不是就回 default (給 dict.get 多 key fallback 用)."""
    for v in vals:
        if is_num(v):
            return v
    return default


if __name__ == "__main__":
    # 自測
    cases = [
        (fmt_num(None), "—"),
        (fmt_num(float("nan")), "—"),
        (fmt_num("—"), "—"),
        (fmt_num(12.345), "12.35"),
        (fmt_num(1234.5, thousands=True), "1,234.50"),
        (fmt_pct(None), "—"),
        (fmt_pct(1.2), "+1.20%"),
        (fmt_pct(-3.0, sign=False), "-3.00%"),
        (fmt_pct(0.0), "+0.00%"),
        (fmt_price(None, "$"), "—"),
        (fmt_price(23.1, "$"), "$23.10"),
        (fmt_int(None), "—"),
        (fmt_int(1234567), "1,234,567"),
        (fmt_num(True), "—"),          # bool 不算數字
        (str(first_num(None, "x", 5)), "5"),
    ]
    fails = [(got, exp) for got, exp in cases if got != exp]
    if fails:
        print("FAIL:", fails)
        raise SystemExit(1)
    print(f"safe_fmt self-test PASS ({len(cases)} cases)")
