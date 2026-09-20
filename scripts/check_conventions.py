#!/usr/bin/env python3
"""
check_conventions.py — 專案慣例的機械化防線 (2026-09-20)

為什麼有這支
------------
這個專案反覆出現的不是「寫錯一行」, 而是同一類錯誤在不同檔案重複長出來:

  1. 跨模組用 dict.get("key") 傳資料, key 打錯就靜默拿預設值。
     實際踩過: threshold_bucket_size / twii_close / main_broker_net_pct /
     target_pct / stop_pct / last_pct / rs_vs_market —— 全部都是
     「功能寫好了但永遠不會執行」, 而且外觀跟正常一模一樣。

  2. 警報模組的 claim/rollback 約定只實作一半 —— 有 mark_alerts_sent() 卻沒有
     unmark_alerts_sent(), 於是送出失敗時 claim 不會回滾, 該則警報永久消失。

  3. 新增 cron 卻忘了在 workflow 的 case "$SCHED" 加對應分支 → 該 slot 靜默落空
     或被 fallback 表路由到錯的 handler。

  4. 裸 except: pass —— 讓上面三類全部變成「安靜地給出錯的答案」。

這支腳本用 baseline 模式: 記錄目前的違規數量, CI 只在「變更糟」時失敗。
既有的技術債不會擋住 merge, 但不准再長新的。

用法
----
    python scripts/check_conventions.py              # 檢查 (CI 用, 變糟才 exit 1)
    python scripts/check_conventions.py --report     # 印出完整違規清單
    python scripts/check_conventions.py --update-baseline   # 修完之後更新基準線

baseline 檔: scripts/.conventions_baseline.json (請一起 commit)
"""
from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".conventions_baseline.json")
SKIP_DIRS = {"__pycache__", ".git", ".github", "node_modules"}


def _is_skipped(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return any(p in SKIP_DIRS or p.startswith("_backup_") for p in parts)


def py_files() -> list:
    out = []
    for root, dirs, fs in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith("_backup_")]
        for f in fs:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                if not _is_skipped(p):
                    out.append(p)
    return sorted(out)


def rel(p: str) -> str:
    return os.path.relpath(p, ROOT).replace("\\", "/")


def _read(p: str) -> str:
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 檢查 1: .get("key") / ["key"] 讀取了一個全 repo 從來沒有人寫入的 key
# ---------------------------------------------------------------------------
# 外部 API 的欄位名當然不會在 repo 裡被「寫入」, 所以需要一份 allowlist。
# 新增一個確定來自外部 schema 的 key 時, 加進這裡就好 —— 但請先確認它真的是外部的,
# 不要用來掩蓋「內部 key 打錯字」。
EXTERNAL_KEY_PREFIXES = ("http://", "https://")
EXTERNAL_KEYS = {
    # FinMind
    "MarginPurchaseTodayBalance", "margin_purchase_today_balance",
    "ShortSaleTodayBalance", "short_sale_today_balance",
    "SecuritiesLendingBalance", "securities_lending_balance",
    "ShortSaleBalance", "short_sale_balance", "securities_lending_short_sale",
    "balance", "industry_category", "futures_id", "contract_date", "contract_type",
    "open_interest_balance_long", "open_interest_balance_short",
    "institutional_investors", "option_id", "contract_id",
    "open_interest", "open_interest_total", "call_put", "Revenue", "PER",
    # yfinance
    "shortName", "Earnings Date",
    # Finnhub
    "epsEstimate", "economicCalendar", "earningsCalendar", "ipoCalendar",
    "filedDate", "filingDate", "form", "filerUrl", "reportUrl", "majorDevelopment",
    "numberOfShares", "totalSharesValue", "exchange", "share",
    "transactionPrice", "transactionCode", "transactionDate",
    # RSS / feedparser / Truth Social / Yahoo
    "published_parsed", "updated_parsed", "reblog", "publish_date", "pubDate",
    "displayTime", "displayName", "provider", "content", "description", "datetime",
    # 環境變數 / 其他
    "GITHUB_EVENT_NAME",
}


def check_orphan_keys() -> list:
    written, read = collections.defaultdict(set), collections.defaultdict(set)

    def s(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    for p in py_files():
        try:
            tree = ast.parse(_read(p))
        except Exception:
            continue
        r = rel(p)
        for n in ast.walk(tree):
            if isinstance(n, ast.Dict):
                for k in n.keys:
                    v = s(k)
                    if v:
                        written[v].add(r)
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Subscript):
                        v = s(t.slice)
                        if v:
                            written[v].add(r)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr in ("setdefault", "update", "pop") and n.args:
                    v = s(n.args[0])
                    if v:
                        written[v].add(r)
                if n.func.attr == "get" and n.args:
                    # 設定檔讀取 (CFG.get / ORD.get / config.get ...) 的 key 來自使用者的
                    # .toml/.ini, 不會在 repo 裡被「寫入」, 一律跳過。
                    recv = n.func.value
                    recv_name = recv.id if isinstance(recv, ast.Name) else (
                        recv.attr if isinstance(recv, ast.Attribute) else "")
                    if re.fullmatch(r"(?i)(cfg|conf|config|ord|settings|opts|options|env|_cfg|_conf)",
                                    recv_name or ""):
                        continue
                    v = s(n.args[0])
                    if v:
                        read[v].add(f"{r}:{n.lineno}")
            if isinstance(n, ast.Subscript) and isinstance(getattr(n, "ctx", None), ast.Load):
                v = s(n.slice)
                if v:
                    read[v].add(f"{r}:{n.lineno}")

    out = []
    for k, locs in read.items():
        if k in written or k in EXTERNAL_KEYS:
            continue
        if k.startswith(EXTERNAL_KEY_PREFIXES) or len(k) > 60:
            continue
        out.append(f"{k}  @ {sorted(locs)[0]}")
    return sorted(out)


# ---------------------------------------------------------------------------
# 檢查 2: 有 mark_* 就必須有對應的 unmark_*
# ---------------------------------------------------------------------------
def check_claim_rollback() -> list:
    out = []
    for p in py_files():
        try:
            tree = ast.parse(_read(p))
        except Exception:
            continue
        marks = [n.name for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name.startswith("mark_")]
        unmarks = [n.name for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name.startswith("unmark_")]
        if marks and not unmarks:
            out.append(f"{rel(p)}: 有 {', '.join(marks)} 但沒有任何 unmark_* → "
                       f"送出失敗時 claim 不會回滾")
        # check_* 函式內部就寫 state = 檢查即 claim, 呼叫端無從回滾
        for n in tree.body:
            if isinstance(n, ast.FunctionDef) and n.name.startswith("check"):
                if n.name.startswith("check_and_push"):
                    continue  # 這類自己送自己存, 慣例上是 send-then-claim
                saves = [c for c in ast.walk(n)
                         if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                         and c.func.attr in ("save_monitor_state", "save_watchlist")]
                if saves and not unmarks:
                    out.append(f"{rel(p)}:{n.name}(): check_* 內部就寫 state 且模組沒有 "
                               f"unmark_* → 被下游去重濾掉或送出失敗時額度已被燒掉")
    return sorted(set(out))


# ---------------------------------------------------------------------------
# 檢查 3: 每條 cron 都必須在 case "$SCHED" 有對應分支
# ---------------------------------------------------------------------------
def check_cron_coverage() -> list:
    out = []
    wf_dir = os.path.join(ROOT, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return out
    for fn in sorted(os.listdir(wf_dir)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        txt = _read(os.path.join(wf_dir, fn))
        crons = re.findall(r"^\s*-\s*cron:\s*['\"]([^'\"]+)['\"]", txt, re.M)
        if not crons:
            continue
        if 'case "$SCHED"' not in txt:
            continue  # 沒用 SCHED router 的 workflow 不適用
        arms = set()
        for m in re.finditer(r'^\s*("(?:[^"]+)"(?:\|"[^"]+")*)\)', txt, re.M):
            for a in m.group(1).split("|"):
                arms.add(a.strip().strip('"'))
        for c in crons:
            if c not in arms:
                out.append(f"{fn}: cron '{c}' 在 case \"$SCHED\" 沒有對應分支 → "
                           f"會落到 fallback 表 (分鐘值可能已過期) 而被路由到錯的 handler")
    return sorted(set(out))


# ---------------------------------------------------------------------------
# 檢查 4: 裸 except (區塊裡只有 pass/continue, 沒有任何 log)
# ---------------------------------------------------------------------------
def count_silent_except() -> int:
    total = 0
    for p in py_files():
        try:
            tree = ast.parse(_read(p))
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.ExceptHandler):
                if all(isinstance(b, (ast.Pass, ast.Continue)) or
                       (isinstance(b, ast.Return) and
                        (b.value is None or isinstance(b.value, ast.Constant)))
                       for b in n.body):
                    total += 1
    return total


# ---------------------------------------------------------------------------
CHECKS = {
    "orphan_keys": ("讀取了沒有生產者的 dict key (功能永遠拿預設值)", check_orphan_keys),
    "claim_rollback": ("claim 沒有對應的 rollback (警報會永久消失)", check_claim_rollback),
    "cron_coverage": ("cron 沒有對應的 router 分支 (slot 靜默落空)", check_cron_coverage),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="印出完整違規清單")
    ap.add_argument("--update-baseline", action="store_true", help="把現況寫成新的基準線")
    args = ap.parse_args()

    current = {name: fn() for name, (_, fn) in CHECKS.items()}
    current_silent = count_silent_except()

    if args.update_baseline:
        data = {"counts": {k: len(v) for k, v in current.items()},
                "items": current,
                "silent_except": current_silent}
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"baseline 已更新 → {rel(BASELINE)}")
        for k, v in current.items():
            print(f"  {k}: {len(v)}")
        print(f"  silent_except: {current_silent}")
        return 0

    base = {"items": {k: [] for k in CHECKS}, "silent_except": 10 ** 9}
    if os.path.exists(BASELINE):
        try:
            with open(BASELINE, encoding="utf-8") as f:
                base = json.load(f)
        except Exception as e:
            print(f"[check_conventions] baseline 讀取失敗, 視為全新: {e}")

    failed = False
    for name, (desc, _) in CHECKS.items():
        known = set(base.get("items", {}).get(name, []))
        now = current[name]
        new = [x for x in now if x not in known]
        fixed = [x for x in known if x not in now]
        status = "❌" if new else "✅"
        print(f"{status} {name}: {len(now)} 項 (基準 {len(known)}), 新增 {len(new)}, 已修 {len(fixed)}  — {desc}")
        if args.report:
            for x in now:
                print(f"      {'NEW ' if x in new else '    '}{x}")
        else:
            for x in new:
                print(f"      NEW  {x}")
        if new:
            failed = True

    base_silent = base.get("silent_except", 10 ** 9)
    if current_silent > base_silent:
        print(f"❌ silent_except: {current_silent} (基準 {base_silent}) — "
              f"新增了 {current_silent - base_silent} 個「只有 pass 的 except」。"
              f"至少 print 一行, 否則這個失敗永遠不會有人知道。")
        failed = True
    else:
        print(f"✅ silent_except: {current_silent} (基準 {base_silent})")

    if failed:
        print("\n違規變多了。修掉新增項, 或在確認是誤報後更新 baseline "
              "(python scripts/check_conventions.py --update-baseline)。")
        return 1
    print("\n所有慣例檢查通過 (既有技術債見 baseline, 沒有變更糟)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
