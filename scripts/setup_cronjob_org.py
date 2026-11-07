"""setup_cronjob_org.py — 把 punctual_slots.json 註冊成 cron-job.org 的雲端準時鬧鐘。

為什麼: GitHub Actions 的 `schedule:` 在這個 repo 實測延遲 4~6.5 小時 (08:00 晨報 14:30 才到)。
workflow_dispatch (API 觸發) 幾乎立即開跑, 所以讓 cron-job.org 在準點打 GitHub API 觸發
workflow —— 運算與 secrets 仍全部在 GitHub, cron-job.org 只負責「準時按鈴」, 電腦不用開。
GitHub 上原本的 cron 保留當備援; 重複由 push_dedup (16 小時窗) 擋, 過時內容由
radar/core/schedule_guard 擋。

事前準備 (只要做一次):
  1. GitHub Fine-grained PAT: Settings → Developer settings → Fine-grained tokens →
     Repository access 只選 stock-dashboard → Permissions: Actions = Read and write。
  2. cron-job.org 免費註冊 → Settings → API → 建立 API key。

用法 (金鑰用環境變數傳入, 不寫進任何檔案):
  # PowerShell
  $env:CRONJOB_API_KEY="..."; $env:GH_DISPATCH_TOKEN="github_pat_..."
  python scripts/setup_cronjob_org.py --repo a3659718/stock-dashboard --dry-run   # 先看會建什麼
  python scripts/setup_cronjob_org.py --repo a3659718/stock-dashboard             # 建立 / 更新
  python scripts/setup_cronjob_org.py --delete                                    # 移除本腳本建的全部 job

  重跑是安全的: 以 title「stock-dashboard:<slot>」辨識, 已存在就更新, 不會重複建立。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

API = "https://api.cron-job.org"
PREFIX = "stock-dashboard:"
SLOTS_FILE = Path(__file__).with_name("punctual_slots.json")


def load_slots() -> list:
    return json.loads(SLOTS_FILE.read_text(encoding="utf-8"))["slots"]


def build_job(slot: dict, repo: str, ref: str, gh_token: str) -> dict:
    hh, mm = (int(x) for x in slot["time"].split(":"))
    return {
        "title": PREFIX + slot["name"],
        "url": f"https://api.github.com/repos/{repo}/actions/workflows/{slot['workflow']}/dispatches",
        "enabled": True,
        "saveResponses": True,
        "requestMethod": 1,  # POST
        "requestTimeout": 30,
        "schedule": {
            "timezone": slot["tz"],
            "expiresAt": 0,
            "hours": [hh],
            "minutes": [mm],
            "mdays": [-1],
            "months": [-1],
            "wdays": slot["wdays"],
        },
        "extendedData": {
            "headers": {
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            # source=punctual: 準時觸發仍會檢查「同 slot 是否已推過」, 不會跟同時間的 cron 重複推
            "body": json.dumps({"ref": ref, "inputs": {**(slot.get("inputs") or {}), "source": "punctual"}}),
        },
        # GitHub 成功回 204; 失敗時寄信通知 (cron-job.org 帳號信箱)
        "notification": {"onFailure": True, "onSuccess": False, "onDisable": True},
    }


def _api(method: str, path: str, key: str, **kw) -> requests.Response:
    r = requests.request(method, API + path, timeout=30,
                         headers={"Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json"}, **kw)
    if r.status_code >= 400:
        raise SystemExit(f"cron-job.org {method} {path} → HTTP {r.status_code}: {r.text[:300]}")
    return r


def existing_jobs(key: str) -> dict:
    jobs = _api("GET", "/jobs", key).json().get("jobs", [])
    return {j["title"]: j["jobId"] for j in jobs if str(j.get("title", "")).startswith(PREFIX)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", help="GitHub repo, 例: a3659718/stock-dashboard")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--dry-run", action="store_true", help="只印出要建立的排程, 不呼叫 API")
    ap.add_argument("--delete", action="store_true", help="刪除本腳本建立的全部 job")
    args = ap.parse_args()

    slots = load_slots()
    if args.dry_run:
        for s in slots:
            print(f"{PREFIX + s['name']:<34} {s['tz']:<17} {s['time']}  wdays={s['wdays']}  "
                  f"{s['workflow']} {s.get('inputs') or ''}")
        print(f"\n共 {len(slots)} 個 (dry-run, 沒有呼叫任何 API)")
        return 0

    key = os.environ.get("CRONJOB_API_KEY", "").strip()
    if not key:
        print("請先設定環境變數 CRONJOB_API_KEY (cron-job.org → Settings → API)")
        return 2
    have = existing_jobs(key)

    if args.delete:
        for title, jid in have.items():
            _api("DELETE", f"/jobs/{jid}", key)
            print(f"deleted {title}")
        print(f"刪除 {len(have)} 個")
        return 0

    gh_token = os.environ.get("GH_DISPATCH_TOKEN", "").strip()
    if not (args.repo and gh_token):
        print("需要 --repo 與環境變數 GH_DISPATCH_TOKEN (Fine-grained PAT, Actions: Read and write)")
        return 2

    for s in slots:
        job = build_job(s, args.repo, args.ref, gh_token)
        if job["title"] in have:
            _api("PATCH", f"/jobs/{have[job['title']]}", key, data=json.dumps({"job": job}))
            print(f"updated {job['title']}")
        else:
            _api("PUT", "/jobs", key, data=json.dumps({"job": job}))
            print(f"created {job['title']}  (建立 API 限 5 次/分鐘, 等 13 秒…)", flush=True)
            time.sleep(13)
    print(f"\n完成 {len(slots)} 個。到 cron-job.org 對任一 job 按「Test run」, "
          "GitHub Actions 頁面應立刻出現一個 workflow_dispatch run (HTTP 204 = 成功)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
