"""punctual_scheduler.py — 在 GitHub Actions 裡常駐的準時排程器 (不需要 cron-job.org 或個人 token)。

為什麼: GitHub `schedule:` 在這個 repo 常態延遲 4~7 小時、還會丟掉約一半的執行;
workflow_dispatch 則幾乎立即開跑。這支程式在一個 workflow run 裡常駐 (~5h45m),
到了 scripts/punctual_slots.json 的每個時間點就用 GITHUB_TOKEN 觸發對應的 workflow
(workflow_dispatch 是 GITHUB_TOKEN 少數「可以」觸發新 run 的事件)。

接力: 開跑時先觸發一個新的自己 → 在同一個 concurrency 組裡排隊 (pending),
本輪結束的瞬間下一輪就開始, 中間沒有空窗。新一輪開始時會補觸發「最近 10 分鐘內」
該觸發但可能落在交接空檔的 slot (一天一次的推播有 push_dedup 擋重複, monitor 重跑一次無害)。

用法:
  python scripts/punctual_scheduler.py --plan-hours 24        # 只印出未來 24 小時的觸發計畫 (不呼叫 API)
  python scripts/punctual_scheduler.py --run-minutes 345      # 常駐模式 (workflow 裡用)
環境變數 (workflow 自動提供): GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_REF_NAME
只用標準函式庫, workflow 不需要 pip install。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

SLOTS_FILE = Path(__file__).with_name("punctual_slots.json")
SELF_WORKFLOW = "punctual_scheduler.yml"
CATCH_UP_MIN = 10        # 新一輪開跑時, 補觸發這麼多分鐘內「剛過」的 slot
STOP_BEFORE_END_SEC = 90  # 在時限前這麼多秒停止, 讓下一輪接手

UTC = dt.timezone.utc


def load_slots(path: Path = SLOTS_FILE) -> List[Dict]:
    return json.loads(path.read_text(encoding="utf-8"))["slots"]


def _slot_hm(slot: Dict) -> Tuple[List[int], List[int]]:
    if slot.get("time"):
        hh, mm = (int(x) for x in slot["time"].split(":"))
        return [hh], [mm]
    return list(slot["hours"]), list(slot["minutes"])


def fire_times(slots: List[Dict], start: dt.datetime, end: dt.datetime) -> List[Tuple[dt.datetime, Dict]]:
    """[start, end) 內所有應觸發的 (UTC 時間, slot), 依時間排序。wdays: 0=週日 ... 6=週六 (當地日期)。"""
    out = []
    for s in slots:
        tz = ZoneInfo(s["tz"])
        hours, minutes = _slot_hm(s)
        wdays = set(s.get("wdays") or range(7))
        d0 = start.astimezone(tz).date() - dt.timedelta(days=1)
        d1 = end.astimezone(tz).date() + dt.timedelta(days=1)
        d = d0
        while d <= d1:
            if (d.weekday() + 1) % 7 in wdays:
                for h in hours:
                    for m in minutes:
                        local = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
                        t = local.astimezone(UTC)
                        if start <= t < end:
                            out.append((t, s))
            d += dt.timedelta(days=1)
    out.sort(key=lambda x: (x[0], x[1]["name"]))
    return out


def dispatch(repo: str, token: str, workflow: str, ref: str, inputs: Dict) -> Tuple[bool, str]:
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    body = json.dumps({"ref": ref, "inputs": inputs}).encode()
    last = ""
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "stock-dashboard-punctual-scheduler",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 204:
                    return True, "204"
                last = f"HTTP {r.status}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code in (401, 403, 404, 422):
                break  # 權限 / 參數錯, 重試沒用
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    return False, last


def _fmt(t: dt.datetime) -> str:
    return (t + dt.timedelta(hours=8)).strftime("%m-%d %H:%M 台北")


def print_plan(items: List[Tuple[dt.datetime, Dict]]) -> None:
    for t, s in items:
        print(f"  {_fmt(t)}  {s['name']:<18} → {s['workflow']} {s.get('inputs') or ''}", flush=True)


def run(run_minutes: int, repo: str, token: str, ref: str, now_fn=lambda: dt.datetime.now(UTC),
        sleep_fn=time.sleep, dispatch_fn=dispatch, self_chain: bool = True,
        after: Optional[dt.datetime] = None) -> int:
    """after: 上一輪負責到的時間 (接力時由上一輪傳入); 從這個時間之後的 slot 才由本輪負責,
    避免交接時重複觸發。沒有 (第一輪 / 斷鏈後重啟) 就補最近 CATCH_UP_MIN 分鐘。"""
    slots = load_slots()
    started = now_fn()
    deadline = started + dt.timedelta(minutes=run_minutes) - dt.timedelta(seconds=STOP_BEFORE_END_SEC)

    if self_chain:
        ok, info = dispatch_fn(repo, token, SELF_WORKFLOW, ref,
                               {"reason": "chain", "after": deadline.isoformat(timespec="seconds")})
        print(f"[scheduler] 排下一輪接力: ok={ok} {info}", flush=True)

    window_start = started - dt.timedelta(minutes=CATCH_UP_MIN)
    if after is not None and started - dt.timedelta(hours=1) <= after <= started:
        window_start = after  # 上一輪做到 after 為止, 從這裡接手 (含交接空檔)
    plan = fire_times(slots, window_start, deadline)
    print(f"[scheduler] 本輪 {_fmt(started)} ~ {_fmt(deadline)}, 共 {len(plan)} 次觸發:", flush=True)
    print_plan(plan)

    fails = 0
    for t, s in plan:
        wait = (t - now_fn()).total_seconds()
        if wait > 0:
            sleep_fn(wait)
        if now_fn() > deadline:
            break
        inputs = {**(s.get("inputs") or {}), "source": "punctual"}
        ok, info = dispatch_fn(repo, token, s["workflow"], ref, inputs)
        late = (now_fn() - t).total_seconds()
        print(f"[scheduler] {_fmt(t)} {s['name']} → ok={ok} {info} (差 {late:+.0f}s)", flush=True)
        fails += 0 if ok else 1
    # 剩餘時間睡到時限, 讓 concurrency 組裡排隊的下一輪在本輪結束時才接手 (避免提早空轉)
    rest = (deadline - now_fn()).total_seconds()
    if rest > 0:
        sleep_fn(rest)
    print(f"[scheduler] 本輪結束, 失敗 {fails} 次", flush=True)
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-minutes", type=int, default=345)
    ap.add_argument("--plan-hours", type=float, help="只印出未來 N 小時的觸發計畫, 不呼叫 API")
    ap.add_argument("--no-chain", action="store_true", help="不排下一輪接力 (測試用)")
    args = ap.parse_args()

    if args.plan_hours:
        now = dt.datetime.now(UTC)
        print_plan(fire_times(load_slots(), now, now + dt.timedelta(hours=args.plan_hours)))
        return 0

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    ref = os.environ.get("GITHUB_REF_NAME", "") or "main"
    if not (token and repo):
        print("需要 GITHUB_TOKEN 與 GITHUB_REPOSITORY (在 GitHub Actions 內自動提供)")
        return 2
    after = None
    if os.environ.get("SCHEDULER_AFTER"):
        try:
            after = dt.datetime.fromisoformat(os.environ["SCHEDULER_AFTER"])
            after = after if after.tzinfo else after.replace(tzinfo=UTC)
        except ValueError:
            after = None
    return run(args.run_minutes, repo, token, ref, self_chain=not args.no_chain, after=after)


if __name__ == "__main__":
    sys.exit(main())
