"""
scripts/smoke_test_full.py — 回歸防線 smoke test.

這支腳本原本在 OPTIMIZATION_PLAN.md 裡被記成「已完成」, 但實際檔案一直不存在 —
導致 ci_smoke.yml 的兩個 job (static / full) 從頭到尾都是找不到檔案而失敗, 且
scripts/daily_selfcheck.py 的「程式完整性」探針也一直是失敗狀態 (ModuleNotFoundError)。
這裡把它補上, 對齊 ci_smoke.yml 跟 daily_selfcheck.py 兩邊原本就假設好的介面。

兩種執行模式:
  python scripts/smoke_test_full.py --static   零相依, 秒級, 不 import 任何本地模組
      1) check_local_imports()    — AST 靜態解析, 抓兩種真實發生過的回歸:
           a) `from X import Y` 裡 Y 根本不在 X 模組裡定義 (例如 X 被改名/砍掉某個
              function, 但呼叫端沒跟著改 — 這正是 "_get_model 缺失讓 8 個 Gemini
              功能默默失效" 這類問題的成因, 且不需要真的 import/裝套件就能抓到)
           b) `import X` / `from X import ...` 指到一個本地檔名, 但那個 .py 檔案
              已經不存在 (檔案被刪除/改名, 呼叫端沒跟上)
      2) check_schedule_coverage() — 純文字/regex 解析 4 個 workflow yml 實際會
         dispatch 出的 market slot 字串, 跟 scripts/market_open_alert.py 裡
         `if market == "..."` 的 handler 分支比對, 抓「yml 說要推某個 slot,
         但 dispatch 腳本沒有對應 handler」這種排程孤兒。
      3) 重複貼上檢查 — 抓同一段 20 行以上完全重複的程式碼 (複製貼上忘記改的典型錯誤來源)

  python scripts/smoke_test_full.py            完整版, 需要先 pip install -r requirements.txt
      在 --static 的基礎上, 再加:
      4) 實際 import 專案內所有本地模組, 抓真正的 ImportError / SyntaxError (需要
         真裝好的相依套件, 因為很多模組 top-level 就 `import streamlit` 等)
      5) 對 notifier.py 裡每個 fmt_*() 格式化函式餵「空輸入」(空 list / 空 dict),
         確認不會整個 crash — 這類函式一崩, 對應那個推播就整封送不出去。

回傳: exit code 0 = 全過, 非 0 = 有發現問題 (印在 stdout, 每行一個問題)。
check_local_imports() / check_schedule_coverage() 回傳 list[tuple(file, kind, message)],
給 daily_selfcheck.py 直接用 (它會讀 imp[0][2] 當作訊息摘要)。
"""
from __future__ import annotations

import ast
import os
import re
import sys
from typing import Dict, List, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 這個專案常用、但不是本地模組的第三方/標準庫套件 — 靜態檢查時要跳過, 不然會
# 一堆誤報 (零相依模式下沒有安裝套件, 沒辦法用「裝了沒」來判斷)。
_KNOWN_EXTERNAL = {
    "streamlit", "pandas", "numpy", "requests", "yfinance", "plotly",
    "google", "dotenv", "bs4", "pytz", "PIL", "matplotlib", "scipy",
    "sklearn", "gspread", "oauth2client", "telegram", "finmind", "FinMind",
    "openpyxl", "lxml", "html5lib", "urllib3", "certifi", "charset_normalizer",
    "idna", "yaml", "toml", "click", "altair", "pyarrow", "tenacity",
    "cachetools", "google_auth_oauthlib", "googleapiclient",
    # 實際存在於 requirements.txt, 之前跑一次 --static 有誤報, 補進允許清單:
    "feedparser", "pandas_market_calendars", "starlette",
    # signal_tracker.py 的 Windows file-lock fallback 用, 見 requirements.txt 補記說明:
    "portalocker",
}


def _iter_repo_py_files() -> List[str]:
    files = []
    for base, dirnames, filenames in os.walk(ROOT):
        # 跳過 venv / git / cache 等非本專案程式碼目錄
        dirnames[:] = [d for d in dirnames if d not in (
            ".git", "__pycache__", "venv", ".venv", "node_modules", ".streamlit",
        )]
        for fn in filenames:
            if fn.endswith(".py"):
                files.append(os.path.join(base, fn))
    return files


def _module_stem(path: str) -> str:
    """檔案路徑 → 可以被 import 的模組名 (只看檔名, 不含資料夾, 因為 daily_selfcheck.py
    有把 ROOT 和 ROOT/scripts 都塞進 sys.path, 兩層資料夾內的檔案都用單一檔名互相 import)."""
    return os.path.splitext(os.path.basename(path))[0]


def _build_module_symbol_map(py_files: List[str]) -> Dict[str, Set[str]]:
    """module_stem -> 這個模組頂層定義的名字集合 (function / class / 變數賦值).
    有 `from X import *` 或 parse 失敗的模組, 對應到 None 當作「無法確定, 跳過檢查」."""
    result: Dict[str, Set[str]] = {}
    for path in py_files:
        stem = _module_stem(path)
        if stem in result and result[stem] is None:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            tree = ast.parse(src, filename=path)
        except Exception:
            result[stem] = None
            continue
        names: Set[str] = set()
        has_star_import = False
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
                    elif isinstance(t, (ast.Tuple, ast.List)):
                        for el in t.elts:
                            if isinstance(el, ast.Name):
                                names.add(el.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.ImportFrom) and any(
                a.name == "*" for a in node.names
            ):
                has_star_import = True
        result[stem] = None if has_star_import else names
    return result


def check_local_imports() -> List[Tuple[str, str, str]]:
    """回傳 [(file, kind, message), ...]; kind in {"missing_module", "missing_symbol"}."""
    problems: List[Tuple[str, str, str]] = []
    py_files = _iter_repo_py_files()
    local_stems = {_module_stem(p) for p in py_files}
    symbol_map = _build_module_symbol_map(py_files)

    for path in py_files:
        rel = os.path.relpath(path, ROOT)
        try:
            with open(path, "r", encoding="utf-8") as f:
                src = f.read()
            tree = ast.parse(src, filename=path)
        except Exception as e:
            problems.append((rel, "parse_error", f"{rel}: 無法解析 (SyntaxError?) {e}"))
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # 相對 import, 不在這裡檢查
                mod = node.module or ""
                top = mod.split(".")[0]
                if not top or top in _KNOWN_EXTERNAL:
                    continue
                if top not in local_stems:
                    continue  # 不認識的名字, 可能是尚未列進允許清單的第三方套件, 不誤判
                target_names = symbol_map.get(top)
                if target_names is None:
                    continue  # 目標模組解析失敗或有 star import, 無法確定, 跳過
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    if alias.name not in target_names:
                        problems.append((
                            rel, "missing_symbol",
                            f"{rel}: `from {mod} import {alias.name}` — "
                            f"但 {top}.py 裡目前沒有定義 {alias.name} (可能被改名/刪除)",
                        ))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if not top or top in _KNOWN_EXTERNAL:
                        continue
                    # 只有「看起來像是曾經存在的本地模組」才檢查 — 用簡單啟發式:
                    # 若這個名字從沒在任何本地檔案出現過 (含它自己), 就無從判斷, 跳過。
                    if top not in local_stems and re.match(r"^[a-z_][a-z0-9_]*$", top):
                        # 進一步排除常見標準庫模組 (避免誤判 os/sys/re 等)
                        if top in sys.stdlib_module_names:
                            continue
                        problems.append((
                            rel, "missing_module",
                            f"{rel}: `import {top}` — 找不到本地檔案 {top}.py, 也非已知套件",
                        ))

        # 同檔案內同名 top-level function/class 被定義兩次 — 稽核這次連續在
        # notifier.py (_split_tg_msg / _ai_verdict 的前身)、entry_evaluator.py
        # (_ai_verdict)、scripts/tg_callback_listener.py (_reply) 抓到 3 次同一種
        # 「後面的定義把前面的完全蓋掉, 前面那份從頭到尾沒人真的呼叫得到」的死碼,
        # 這種問題不需要真的 import/裝套件就能靜態抓到, 順便併進「程式完整性」檢查,
        # 讓 daily_selfcheck.py 也能自動盯著, 以後同一類回歸不用等人工複查才發現。
        seen_defs: Dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in seen_defs:
                    problems.append((
                        rel, "duplicate_definition",
                        f"{rel}: `{node.name}` 在第 {seen_defs[node.name]} 行和第 {node.lineno} 行"
                        f"各定義一次 — 後面那份會完全蓋掉前面, 前面那份是死碼 (從頭到尾不會被呼叫到)",
                    ))
                seen_defs[node.name] = node.lineno
    return problems


def check_schedule_coverage() -> List[Tuple[str, str, str]]:
    """比對 4 個 workflow yml 實際會 dispatch 的 market slot, 跟
    scripts/market_open_alert.py 裡的 `if market == "..."` handler 是否對得上."""
    problems: List[Tuple[str, str, str]] = []
    dispatch_path = os.path.join(ROOT, "scripts", "market_open_alert.py")
    if not os.path.exists(dispatch_path):
        return [("scripts/market_open_alert.py", "missing_file", "dispatch 腳本不存在")]

    with open(dispatch_path, "r", encoding="utf-8") as f:
        dispatch_src = f.read()
    handlers = set(re.findall(r'market\s*==\s*"([a-z_0-9]+)"', dispatch_src))

    workflow_dir = os.path.join(ROOT, ".github", "workflows")
    if not os.path.isdir(workflow_dir):
        return problems

    for fn in sorted(os.listdir(workflow_dir)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        wf_path = os.path.join(workflow_dir, fn)
        with open(wf_path, "r", encoding="utf-8") as f:
            wf_src = f.read()
        # router 輸出 `market=xxx` 或直接呼叫 `... market_open_alert.py xxx` 這兩種寫法都抓
        slots = set(re.findall(r"market=([a-z_0-9]+)", wf_src))
        slots |= set(re.findall(r"market_open_alert\.py\s+([a-z_0-9]+)", wf_src))
        for slot in sorted(slots):
            if slot in ("monitor", "heartbeat"):
                # 這兩個是固定會有 handler 的常駐 slot, 且部份 workflow 用變數組出來,
                # 已在別處驗證過, 這裡跳過避免誤判 shell 變數殘留字串。
                if slot not in handlers:
                    problems.append((fn, "missing_handler", f"{fn}: slot `{slot}` 沒有對應 handler (常駐 slot 異常, 需人工確認)"))
                continue
            if slot not in handlers:
                problems.append((
                    fn, "missing_handler",
                    f"{fn}: 排程會 dispatch `{slot}`, 但 market_open_alert.py 沒有 "
                    f'`if market == "{slot}"` 的 handler',
                ))
    return problems


def check_duplicate_blocks(min_lines: int = 20) -> List[Tuple[str, str, str]]:
    """簡單抓「同一個檔案內, 連續 N 行以上完全重複」的複製貼上痕跡 (跨檔案比對成本較高,
    這裡只做同檔內偵測, 已足夠抓最常見的複製貼上忘記改的情況)."""
    problems: List[Tuple[str, str, str]] = []
    for path in _iter_repo_py_files():
        rel = os.path.relpath(path, ROOT)
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.rstrip() for l in f.readlines()]
        except Exception:
            continue
        seen: Dict[tuple, int] = {}
        i = 0
        n = len(lines)
        while i + min_lines <= n:
            block = tuple(lines[i:i + min_lines])
            if all(not l.strip() for l in block):
                i += 1
                continue
            if block in seen:
                problems.append((
                    rel, "duplicate_block",
                    f"{rel}: 第 {seen[block]+1} 行跟第 {i+1} 行起有 {min_lines}+ 行完全重複 "
                    "(疑似複製貼上忘記改)",
                ))
                i += min_lines
            else:
                seen[block] = i
                i += 1
    return problems


def check_full_imports() -> List[Tuple[str, str, str]]:
    """完整模式限定: 真的 import 每個本地模組 (需要裝好 requirements.txt), 抓真正的
    ImportError / 執行期例外 (static 模式的 AST 檢查抓不到的深層問題)。"""
    problems: List[Tuple[str, str, str]] = []
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    for path in _iter_repo_py_files():
        rel = os.path.relpath(path, ROOT)
        if rel.startswith("scripts" + os.sep) or os.sep + "scripts" + os.sep in path:
            # scripts/ 底下多半是 CLI entry point (跑了會直接發推播/打 API), 不安全 import 執行,
            # 只針對非 scripts/ 的共用模組做完整 import 測試。
            continue
        stem = _module_stem(path)
        if stem in ("app", "__init__"):
            continue
        try:
            import importlib
            importlib.import_module(stem)
        except Exception as e:
            problems.append((rel, "import_error", f"{rel}: import 失敗 — {type(e).__name__}: {e}"))
    return problems


def check_notifier_empty_input() -> List[Tuple[str, str, str]]:
    """完整模式限定: 對 notifier.py 裡每個 fmt_*() 函式餵空輸入, 確認不會 crash."""
    problems: List[Tuple[str, str, str]] = []
    try:
        sys.path.insert(0, ROOT)
        import notifier
    except Exception as e:
        return [("notifier.py", "import_error", f"notifier.py import 失敗, 跳過空輸入測試: {e}")]

    import inspect
    for name, fn in vars(notifier).items():
        if not (name.startswith("fmt_") and callable(fn)):
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        args = []
        for p in sig.parameters.values():
            if p.default is not inspect.Parameter.empty:
                continue
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            ann = str(p.annotation)
            if "dict" in ann.lower() or "Dict" in ann:
                args.append({})
            elif "list" in ann.lower() or "List" in ann:
                args.append([])
            elif "str" in ann:
                args.append("")
            else:
                args.append(None)
        try:
            fn(*args)
        except Exception as e:
            problems.append((
                "notifier.py", "fmt_empty_input_crash",
                f"notifier.{name}() 餵空輸入會 crash — {type(e).__name__}: {e}",
            ))
    return problems


def main() -> int:
    static_only = "--static" in sys.argv
    problems: List[Tuple[str, str, str]] = []
    # duplicate_block 只是「值得注意」的程式碼重複觀察, 不是真的會讓 import/排程/推播
    # 壞掉的問題 (跟其他幾種 kind 性質不同) — 印出來給人看, 但不擋 CI merge / 不算
    # daily_selfcheck 的異常, 避免每次都因為既有的、無害的重複程式碼卡住。
    advisory_only = {"duplicate_block"}

    problems += check_local_imports()
    problems += check_schedule_coverage()
    problems += check_duplicate_blocks()

    if not static_only:
        problems += check_full_imports()
        problems += check_notifier_empty_input()

    blocking = [p for p in problems if p[1] not in advisory_only]
    advisory = [p for p in problems if p[1] in advisory_only]

    if not problems:
        print(f"[smoke_test_full] 全過 ({'static' if static_only else 'full'} 模式), 0 個問題")
        return 0

    if advisory:
        print(f"[smoke_test_full] {len(advisory)} 項僅供參考 (不擋 CI):")
        for _file, kind, msg in advisory:
            print(f"  [{kind}] {msg}")

    if not blocking:
        print("[smoke_test_full] 沒有會擋 merge 的問題, 通過")
        return 0

    print(f"[smoke_test_full] 發現 {len(blocking)} 個問題 ({'static' if static_only else 'full'} 模式):")
    for _file, kind, msg in blocking:
        print(f"  [{kind}] {msg}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
