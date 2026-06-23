#!/usr/bin/env bash
# deploy_to_github.sh — 一鍵 commit + push 所有 local 改動到 GitHub
#
# 用途: 把本地改動推到 GitHub, 讓 GH Actions cron 用最新 code
# 用法:
#   cd C:\Users\user\Desktop\Project\stock_dashboard
#   bash scripts/deploy_to_github.sh
#   或者: scripts/deploy_to_github.sh "額外的 commit 訊息"

set -e

cd "$(dirname "$0")/.."

# 預設 commit 訊息
DEFAULT_MSG="chore: hybrid alerts + cron drift + timezone fix + tx_futures + alert_priority + 川普白名單"
MSG="${1:-$DEFAULT_MSG}"

echo "=========================================="
echo "  Deploy to GitHub"
echo "=========================================="
echo ""

# 1. 看看有沒改動
CHANGES=$(git status --porcelain | wc -l)
if [ "$CHANGES" -eq 0 ]; then
    echo "✓ 沒有任何改動, 不用 push"
    exit 0
fi

echo "📝 偵測到 $CHANGES 個改動:"
git status --short | head -20
if [ "$CHANGES" -gt 20 ]; then
    echo "  ... 還有 $((CHANGES - 20)) 個"
fi
echo ""

# 2. 確認
read -p "確定要 push 上 GitHub? [y/N]: " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
    echo "✗ 取消"
    exit 0
fi

# 3. add + commit + push
echo ""
echo "→ git add -A"
git add -A

echo "→ git commit -m \"$MSG\""
git commit -m "$MSG"

echo "→ git push"
git push

echo ""
echo "=========================================="
echo "✅ Deploy 完成"
echo "=========================================="
echo ""
echo "下一步: 去 GitHub → Actions → 手動觸發一次 pre_market_815 確認"
echo "  URL: https://github.com/<your_repo>/actions/workflows/market_open_alert.yml"
