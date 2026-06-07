"""
crypto_picker.py — DEPRECATED (用戶要求取消加密貨幣)

保留檔案避免 import error, 所有 function 回空.
"""
from __future__ import annotations
from typing import Dict, List


def get_crypto_picks(top_n: int = 5) -> Dict:
    return {"picks": [], "deprecated": True}


def fmt_crypto_picks_tg(data: Dict) -> str:
    return ""
