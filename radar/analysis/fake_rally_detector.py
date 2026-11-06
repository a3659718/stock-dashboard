"""
fake_rally_detector.py — 假反彈 / 量縮上漲 偵測

當推播個股漲幅 ≥ 3% 但量比 < 0.8x → 警示為「弱反彈」.
通常代表沒有承接量, 容易回吐.

也可偵測「量縮上漲」: 連 3 日漲但量遞減 → 動能枯竭.

API:
  classify_rally(today_pct, vol_ratio, close_series=None, volume_series=None) -> dict
  flag_fake_rally(picks: List[Dict]) -> List[Dict]  # 對每檔加 rally_quality / fake_rally_warning
"""
from __future__ import annotations

from typing import Dict, List, Optional


# 門檻 (可調)
RALLY_PCT_THRESHOLD = 3.0          # 漲幅 ≥ 此值才檢查
WEAK_VOL_RATIO = 0.8                # 量比 < 此值 = 弱
NORMAL_VOL_RATIO = 1.2              # 量比 ≥ 此值 = 正常承接
STRONG_VOL_RATIO = 2.0              # 量比 ≥ 此值 = 強勢


def classify_rally(today_pct: float, vol_ratio: float,
                    close_series=None, volume_series=None) -> Dict:
    """分類今日漲跌品質.

    回傳:
      quality: "strong" / "normal" / "weak" / "fake_rally" / "n/a"
      emoji: 對應 emoji
      warning: 警示文字 (若 fake_rally)
      momentum_decay: 動能枯竭警告 (連續 3 日漲但量遞減)
    """
    out = {
        "quality": "n/a",
        "emoji": "",
        "warning": "",
        "momentum_decay": False,
        "momentum_decay_msg": "",
    }
    if today_pct is None or vol_ratio is None:
        return out
    try:
        tp = float(today_pct)
        vr = float(vol_ratio)
    except (TypeError, ValueError):
        return out

    # 主分類
    if tp < RALLY_PCT_THRESHOLD:
        # 沒漲多, 不分類
        if vr >= STRONG_VOL_RATIO and tp > 0:
            out.update(quality="absorb", emoji="🕵️",
                        warning=f"量比 {vr:.2f}x 但僅漲 {tp:+.2f}% (吸籌?)")
        return out

    if vr < WEAK_VOL_RATIO:
        # 漲 ≥3% 但量縮 → 假反彈
        out.update(
            quality="fake_rally",
            emoji="⚠️",
            warning=f"漲 {tp:+.2f}% 但量比僅 {vr:.2f}x (<{WEAK_VOL_RATIO}x), 弱反彈可能回吐",
        )
    elif vr < NORMAL_VOL_RATIO:
        out.update(
            quality="weak",
            emoji="🟡",
            warning=f"漲 {tp:+.2f}% 但量比 {vr:.2f}x 偏弱, 留意承接",
        )
    elif vr >= STRONG_VOL_RATIO:
        out.update(quality="strong", emoji="🔥",
                    warning=f"漲 {tp:+.2f}% + 量比 {vr:.2f}x 強勢")
    else:
        out.update(quality="normal", emoji="✅",
                    warning="量價配合正常")

    # 進階: 動能枯竭偵測 (需要 close/vol series)
    if close_series is not None and volume_series is not None:
        try:
            c_tail = close_series.tail(3).tolist() if hasattr(close_series, 'tail') else close_series[-3:]
            v_tail = volume_series.tail(3).tolist() if hasattr(volume_series, 'tail') else volume_series[-3:]
            if len(c_tail) >= 3 and len(v_tail) >= 3:
                # 連 3 日漲
                ups = all(c_tail[i] > c_tail[i-1] for i in range(1, 3))
                # 量遞減
                vol_dec = v_tail[2] < v_tail[1] < v_tail[0]
                if ups and vol_dec:
                    out["momentum_decay"] = True
                    out["momentum_decay_msg"] = (
                        f"連 3 日漲但量遞減 ({v_tail[0]:.0f}→{v_tail[1]:.0f}→{v_tail[2]:.0f}), "
                        "動能枯竭警示"
                    )
        except Exception:
            pass

    return out


def flag_fake_rally(picks: List[Dict],
                     pct_field: str = "today_pct",
                     vol_field: str = "vol_ratio") -> List[Dict]:
    """對 picks list 中每檔, 加 rally_quality / fake_rally_warning 欄位.

    回傳: 同樣 list, 已加標欄位. 不過濾, 留給呼叫端決定.
    """
    out = []
    for p in picks:
        if not isinstance(p, dict):
            out.append(p)
            continue
        c = classify_rally(p.get(pct_field), p.get(vol_field))
        p2 = dict(p)
        p2["rally_quality"] = c["quality"]
        p2["rally_emoji"] = c["emoji"]
        p2["rally_warning"] = c["warning"]
        if c["momentum_decay"]:
            p2["momentum_decay"] = True
            p2["momentum_decay_msg"] = c["momentum_decay_msg"]
        out.append(p2)
    return out


def is_fake_rally(today_pct: Optional[float], vol_ratio: Optional[float]) -> bool:
    """快速判斷單檔是否為假反彈."""
    c = classify_rally(today_pct, vol_ratio)
    return c["quality"] == "fake_rally"
