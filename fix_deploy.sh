#!/usr/bin/env bash
# fix_deploy.sh — 診斷 git 狀態並把「目前正確的本地檔案」推上 GitHub
#
# 為什麼需要它:
#   Cowork 只掛載 stock_dashboard 這層, 看不到上層的 .git, 也沒有網路 / 你的
#   GitHub 登入, 所以沒辦法幫你 push。這支腳本要在「你自己電腦的終端機」跑
#   (Git Bash 或 WSL), 那裡才有完整檔案樹 + 網路 + git 帳密。
#
# 用法 (在這個資料夾裡):
#   bash fix_deploy.sh
#   # 若顯示「不是 git repo」, 設定 repo 網址再重跑一次:
#   REPO_URL=https://github.com/<帳號>/<repo>.git bash fix_deploy.sh

cd "$(dirname "$0")"

MSG="fix: 補回 notifier fmt_speculation_picks/_split_tg_msg + 修 fmt_watchlist_alert(lines→body); vol_breakout/chip_anomaly 送出失敗回滾 claim; 修 dt.timezone NameError"

echo "=========================================="
echo "  1. 檢查是不是 git repo"
echo "=========================================="
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "✓ 在 git repo 內 — repo 根目錄: $(git rev-parse --show-toplevel)"
    echo
    echo "  notifier.py 在 git 裡最後一次 commit:"
    git log --oneline -1 -- notifier.py 2>/dev/null || echo "  ⚠️ notifier.py 從未被 commit 進這個 repo!"
    echo
    echo "=========================================="
    echo "  2. add + commit + push 目前所有改動"
    echo "=========================================="
    git add -A
    echo "本次要 push 的改動:"
    git status --short | head -30
    echo
    if git diff --cached --quiet; then
        echo "(沒有新改動 — 可能已經 commit 過, 直接嘗試 push)"
    else
        git commit -m "$MSG"
    fi
    if ! git push; then
        echo "→ 沒有 upstream, 改用 git push origin HEAD"
        git push origin HEAD
    fi
    echo
    echo "✅ 完成 — 去 GitHub → Actions 看 CI Smoke 是否轉綠"
else
    echo "✗ 這裡不是 git repo (沒有 .git)。"
    if [ -z "$REPO_URL" ]; then
        echo
        echo "  請先設定你的 GitHub repo 網址再重跑一次, 例如:"
        echo "    REPO_URL=https://github.com/你的帳號/stock_dashboard.git bash fix_deploy.sh"
        echo
        echo "  (repo 網址可在 GitHub repo 頁面右上角 <> Code 綠色按鈕裡複製)"
        exit 1
    fi
    echo "== 用 $REPO_URL 初始化並接上遠端 =="
    git init
    git remote add origin "$REPO_URL"
    git fetch origin
    DEF=$(git remote show origin 2>/dev/null | sed -n 's/.*HEAD branch: //p')
    DEF=${DEF:-main}
    echo "遠端預設分支: $DEF"
    # 把遠端歷史接進來, 但「工作目錄的檔案 (你的正確版本) 保持不動」當成待 commit 的改動
    git reset --mixed "origin/$DEF"
    git add -A
    git commit -m "$MSG"
    git push origin "HEAD:$DEF"
    echo
    echo "✅ 完成 — 去 GitHub → Actions 看 CI Smoke 是否轉綠"
fi
