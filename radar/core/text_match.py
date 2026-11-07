"""text_match.py — 新聞 / 題材關鍵字比對 (詞界 + 去除重疊)。

為什麼: 多個模組原本用 `kw in text` 子字串比對, 英文短詞大量誤中:
  "deal" ⊂ ideal / dealer、"settled" ⊂ unsettled、"approval" ⊂ disapproval、
  "ipo" ⊂ shipowner、"oil" ⊂ turmoil、"ai" ⊂ retail / chain、"ev" ⊂ development、
  發布者白名單 "ft" ⊂ microsoft …
英文 (ASCII) 關鍵字用詞界比對; 中文維持子字串比對。

詞尾: 所有英文關鍵字都允許複數 (s / es); 5 個字母以上的再允許常見詞形變化
(upgrade → upgraded、drill → drilling 不到 5 字母所以另外列、robot → robotics …)。
短詞 (ai / ev / ipo / oil / ft) 只允許複數, 才不會又變成子字串誤判。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, List

_SHORT_SUFFIX = r"(?:s|es)?"
_LONG_SUFFIX = r"(?:s|es|d|ed|ing|ings|ics?|al|er|ers)?"


@lru_cache(maxsize=2048)
def _pattern(kw: str) -> "re.Pattern":
    k = kw.lower()
    last_word = re.split(r"[^a-z0-9]+", k)[-1] if k else ""
    suffix = _LONG_SUFFIX if len(last_word) >= 5 else _SHORT_SUFFIX
    # 字尾是英數字才接詞形變化 (例 "x.com" / "s&p" 結尾不接)
    if not k or not k[-1].isalnum():
        suffix = ""
    # "upgrade" + "d" → upgraded: 以 e 結尾時 -ed / -ing 會吞掉 e, 補上 (?:e?) 的變體
    stem = re.escape(k)
    if k.endswith("e") and len(last_word) >= 5:
        stem = re.escape(k[:-1]) + r"(?:e|(?=ing))"
    return re.compile(r"(?<![a-z0-9])" + stem + suffix + r"(?![a-z0-9])")


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


def _norm(k: str) -> str:
    n = k.lower().strip()
    # 只對 4 個字母以上的英文詞去複數 (避免 "us" → "u"、"bus" → "bu")
    if n.isascii() and len(n) > 3 and n.endswith("s") and not n.endswith("ss"):
        n = n[:-2] if n.endswith("es") and len(n) > 4 and n[-3] in "sxz" else n[:-1]
    return n


def distinct_concepts(found: Iterable[str]) -> List[str]:
    """把重疊的命中合併成「概念」: tariff / tariffs / china tariff → 只算 china tariff 一個。

    用在「命中 ≥ N 個關鍵字」的門檻, 避免同一個詞的變體把門檻灌滿。
    包含關係用詞界判斷 ("rate" 不會被 "corporate tax" 吃掉)。
    """
    norm: List[str] = []
    for k in found:
        n = _norm(k)
        if n and n not in norm:
            norm.append(n)
    return [n for n in norm
            if not any(n != o and kw_hit(n, o) for o in norm)]
