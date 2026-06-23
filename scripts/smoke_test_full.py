"""
smoke_test_full.py — 部署前自我檢查 (不發真 TG).

涵蓋四層, 任何一層失敗都回非 0 exit code (可接 CI / pre-push hook):

  [1] 靜態 local-import 檢查 (零相依, 一定會跑)
      解析每個 .py, 確認 `from <本地模組> import <名稱>` 的 <名稱> 真的存在.
      → 這層就能抓到像 `_get_model` 這種「被 8 個模組 import 但 ai_analyzer 沒定義」的 bug,
        而且不用裝 streamlit/pandas/yfinance 也能在 CI 跑.

  [2] 模組匯入 (需相依套件; 缺套件時整層 SKIP)
      逐一 import 所有模組, 抓 SyntaxError / 缺套件 / module-level 例外.

  [3] notifier fmt_* 空輸入 (需相依)
      用空/None 輸入呼叫每個 fmt_* / build_* , 確認不丟例外 (測 graceful 空路徑).

  [4] 排程 handler 覆蓋
      market_open_alert.yml 可能 emit 的每個 market 值, 在 market_open_alert.py 都有 handler.

用法:
    python scripts/smoke_test_full.py            # 全跑
    python scripts/smoke_test_full.py --static   # 只跑 [1][4] (零相依, CI 友善)
"""
from __future__ import annotations

import ast
import os
import sys
import glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# [1] 靜態 local-import 符號檢查
# ---------------------------------------------------------------------------
def _module_defined_names(path: str) -> set:
    """回一個模組 top-level 定義/匯入的所有名稱 (給「被 import 的名稱是否存在」比對)."""
    names: set = set()
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except Exception:
        return names
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, ast.Tuple):
                    names.update(e.id for e in t.elts if isinstance(e, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    names.add(a.asname or a.name)
    # 條件式定義 (try/except, if) 內的 def/class 也算 — 掃一層內層
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def check_local_imports() -> list:
    """回 [(file, lineno, 'from mod import name')] 找不到的清單."""
    py_files = (glob.glob(os.path.join(ROOT, "*.py")) +
                glob.glob(os.path.join(ROOT, "scripts", "*.py")))
    local_mods = {}
    for f in py_files:
        base = os.path.splitext(os.path.basename(f))[0]
        local_mods.setdefault(base, f)
    # 也支援 scripts.xxx 形式
    cache: dict = {}

    def defined(mod: str):
        if mod not in cache:
            path = local_mods.get(mod)
            cache[mod] = _module_defined_names(path) if path else None
        return cache[mod]

    problems = []
    for f in py_files:
        try:
            tree = ast.parse(open(f, encoding="utf-8").read(), filename=f)
        except Exception as e:
            problems.append((f, 0, f"SyntaxError: {e}"))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            mod = node.module.split(".")[-1]  # scripts.market_open_alert → market_open_alert
            target = defined(mod)
            if target is None:
                continue  # 非本地模組 (第三方/標準庫) → 跳過
            for a in node.names:
                if a.name != "*" and a.name not in target:
                    problems.append((os.path.relpath(f, ROOT), node.lineno,
                                     f"from {node.module} import {a.name}  ← {mod} 沒有定義 {a.name}"))
    return problems


# ---------------------------------------------------------------------------
# [4] 排程 handler 覆蓋
# ---------------------------------------------------------------------------
def check_schedule_coverage() -> list:
    """market_open_alert.yml 可能 emit 的 market 值, 在 market_open_alert.py 都要有 handler."""
    import re
    yml = os.path.join(ROOT, ".github", "workflows", "market_open_alert.yml")
    handler = os.path.join(ROOT, "scripts", "market_open_alert.py")
    problems = []
    try:
        yml_txt = open(yml, encoding="utf-8").read()
        h_txt = open(handler, encoding="utf-8").read()
    except Exception as e:
        return [("schedule", 0, f"讀檔失敗: {e}")]
    emitted = set(re.findall(r"market=([a-z_0-9]+)", yml_txt))
    # handler 認得的 market: 找 market == "xxx"
    handled = set(re.findall(r'market\s*==\s*["\']([a-z_0-9]+)["\']', h_txt))
    for m in sorted(emitted):
        if m not in handled:
            problems.append((".github/workflows/market_open_alert.yml", 0,
                             f"market='{m}' 被 emit 但 market_open_alert.py 沒有對應 handler"))
    return problems


# ---------------------------------------------------------------------------
# [5] 重複貼上手誤 (複用 find_dup_lines)
# ---------------------------------------------------------------------------
def check_dup_paste() -> list:
    """抓「複製貼上沒清乾淨」: 連續重複行 / 重複 if 區塊 (零相依)."""
    try:
        import find_dup_lines  # 同在 scripts/, 已加進 sys.path
    except Exception as e:
        return [("find_dup_lines", 0, f"無法載入重複偵測器: {e}")]
    problems = []
    files = (glob.glob(os.path.join(ROOT, "*.py")) +
             glob.glob(os.path.join(ROOT, "scripts", "*.py")))
    for f in files:
        if os.path.basename(f) in ("find_dup_lines.py",):
            continue
        for ln, kind, detail in find_dup_lines.scan_file(f):
            problems.append((os.path.relpath(f, ROOT), ln, f"[{kind}] {detail}"))
    return problems


# ---------------------------------------------------------------------------
# [2] 模組匯入
# ---------------------------------------------------------------------------
def check_imports() -> list:
    problems = []
    for f in glob.glob(os.path.join(ROOT, "*.py")):
        mod = os.path.splitext(os.path.basename(f))[0]
        if mod.startswith("_") or mod == "app":  # app.py 是 streamlit script, 直接 import 會跑 UI
            continue
        try:
            __import__(mod)
        except Exception as e:
            problems.append((mod, 0, f"{type(e).__name__}: {e}"))
    return problems


# ---------------------------------------------------------------------------
# [3] notifier fmt_* 空輸入
# ---------------------------------------------------------------------------
def check_notifier_fmts() -> list:
    import inspect
    problems = []
    try:
        import notifier
    except Exception as e:
        return [("notifier", 0, f"import 失敗, 跳過 fmt_* 測試: {e}")]
    import pandas as pd
    empty_df = pd.DataFrame()
    # 各參數型別給安全空值
    def _arg(name: str):
        n = name.lower()
        if any(k in n for k in ("df", "frame", "sectors", "combined")):
            return empty_df
        if any(k in n for k in ("list", "alerts", "picks", "breaches", "data_list")):
            return []
        if any(k in n for k in ("dict", "data", "fg", "info", "acc", "cycle", "advice",
                                  "crash", "prediction", "accuracy", "pulse", "diff")):
            return {}
        if "str" in n or "text" in n or "label" in n or "name" in n or "id" in n:
            return ""
        return None
    for fname, fn in inspect.getmembers(notifier, inspect.isfunction):
        if not (fname.startswith("fmt_") or fname.startswith("build_")):
            continue
        if fn.__module__ != "notifier":
            continue
        try:
            sig = inspect.signature(fn)
            kwargs = {}
            args = []
            for p in sig.parameters.values():
                if p.default is not inspect.Parameter.empty:
                    continue  # 用預設
                if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                args.append(_arg(p.name))
            res = fn(*args)
            if res is not None and not isinstance(res, (str, dict)):
                problems.append((f"notifier.{fname}", 0, f"回傳型別非 str/dict: {type(res)}"))
        except Exception as e:
            problems.append((f"notifier.{fname}", 0, f"空輸入丟例外: {type(e).__name__}: {e}"))
    return problems


# ---------------------------------------------------------------------------
def main() -> int:
    static_only = "--static" in sys.argv
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "scripts"))  # 讓 [5] 能 import find_dup_lines
    total = 0

    def report(title, problems):
        nonlocal total
        print(f"\n=== {title} ===")
        if not problems:
            print("  ✅ PASS")
            return
        total += len(problems)
        for f, ln, msg in problems:
            loc = f"{f}:{ln}" if ln else f
            print(f"  ❌ {loc}  {msg}")

    report("[1] 靜態 local-import 符號檢查", check_local_imports())
    report("[4] 排程 handler 覆蓋", check_schedule_coverage())
    report("[5] 重複貼上手誤 (append/if 區塊)", check_dup_paste())

    if not static_only:
        report("[2] 模組匯入", check_imports())
        report("[3] notifier fmt_* 空輸入", check_notifier_fmts())
    else:
        print("\n(--static: 跳過 [2][3] 需相依套件的層)")

    print("\n" + ("=" * 50))
    if total == 0:
        print("✅ smoke test 全數通過")
        return 0
    print(f"❌ 共 {total} 個問題 — 見上方")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
