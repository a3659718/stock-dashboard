"""
find_dup_lines.py — 抓「複製貼上沒清乾淨」的重複手誤.

針對專案裡反覆出現的 bug 模式 (ipo / smart_money / actionable_picks 都中過):
  A) 連續兩行內容完全相同 (e.g. 同一個 f-string append 貼兩次)
  B) 連續兩行的「if 條件 + 內文」兩行區塊, 緊接著又原樣重複一次

只掃會造成「輸出重複」的行 (append / f-string / if 區塊), 避開 pass/else/括號等正常重複.

用法: python scripts/find_dup_lines.py
回傳非 0 = 有疑似重複 (可接 CI)。
"""
from __future__ import annotations

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 正常會連續重複、要忽略的行
_IGNORE = {"", "pass", "continue", "break", "else:", "try:", "return", "return None",
           ")", "(", "[", "]", "}", "{", "})", "],", "],)", "else :"}


def _meaningful(s: str) -> bool:
    s = s.strip()
    if s in _IGNORE or s.startswith("#"):
        return False
    if len(s) < 10:
        return False
    # 只關心「會產出內容 / 條件」的行
    return (".append(" in s or "f\"" in s or "f'" in s or
            s.startswith(("if ", "elif ", "lines.append", "out.append", "parts.append")))


def scan_file(path: str) -> list:
    try:
        raw = open(path, encoding="utf-8").read().splitlines()
    except Exception:
        return []
    stripped = [ln.strip() for ln in raw]
    hits = []
    n = len(raw)

    # A) 連續兩行完全相同
    for i in range(n - 1):
        s = stripped[i]
        if _meaningful(s) and s == stripped[i + 1]:
            hits.append((i + 1, "連續重複行", s[:80]))

    # B) 兩行區塊 (if 條件 + 內文) 緊接著原樣重複
    for i in range(n - 3):
        a0, a1, b0, b1 = stripped[i], stripped[i + 1], stripped[i + 2], stripped[i + 3]
        if (a0.startswith(("if ", "elif ")) and a0.endswith(":")
                and a0 == b0 and a1 == b1 and _meaningful(a1)):
            hits.append((i + 1, "重複 if 區塊", f"{a0[:50]} → {a1[:50]}"))
    return hits


def main() -> int:
    files = (glob.glob(os.path.join(ROOT, "*.py")) +
             glob.glob(os.path.join(ROOT, "scripts", "*.py")))
    total = 0
    for f in sorted(files):
        hits = scan_file(f)
        if not hits:
            continue
        rel = os.path.relpath(f, ROOT)
        for ln, kind, detail in hits:
            total += 1
            print(f"  ❌ {rel}:{ln}  [{kind}]  {detail}")
    print("\n" + "=" * 50)
    if total == 0:
        print("✅ 沒有發現重複貼上手誤")
        return 0
    print(f"⚠️  發現 {total} 處疑似重複 — 請逐一確認")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
