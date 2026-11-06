"""text_match.py — 新聞 / 題材關鍵字比對 (詞界 + 去除重疊)。

為什麼: 多個模組原本用 `kw in text` 子字串比對, 英文短詞大量誤中:
  "deal" ⊂ ideal / dealer、"settled" ⊂ unsettled、"approval" ⊂ disapproval、
  "ipo" ⊂ shipowner、"oil" ⊂ turmoil、"ai" ⊂ retail / chain、"ev" ⊂ development、
  發布者白名單 "ft" ⊂ microsoft …
英文 (ASCII) 關鍵字一律用詞界比對 (允許複數 s / es); 中文維持子字串比對。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, List


@lru_cache(maxsize=2048)
def _pattern(kw: str) -> "re.Pattern":
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?:s|es)?(?![a-z0-9])")


def kw_hit(kw: str, text: str) -> bool:
    """kw 是否出現在 text 裡。ASCII 關鍵字用詞界比對; 其他 (中文) 用子字串。"""
    if not kw or not text:
        return False
    if kw.isascii():
        return bool(_pattern(kw).search(text.lower()))
    return kw in text


def hits(keywords: Iterable[str], text: str) -> List[str]:
    """回傳命中的關鍵字 (保持 keywords 原順序)。"""
    return [k for k in keywords if kw_hit(k, text)]


def distinct_concepts(found: Iterable[str]) -> List[str]:
    """把重疊的命中合併成「概念」: tariff / tariffs / china tariff → 只算 china tariff 一個。

    用在「命中 ≥ N 個關鍵字」的門檻, 避免同一個詞的變體把門檻灌滿。
    """
    norm = []
    for k in found:
        n = k.lower().strip()
        n = re.sub(r"(?:es|s)$", "", n) if n.isascii() else n
        if n not in norm:
            norm.append(n)
    return [n for n in norm if not any(n != o and n in o for o in norm)]
