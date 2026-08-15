@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ==========================================
echo   Deploy stock_dashboard to GitHub
echo ==========================================
echo.

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto NOREPO

echo [1] This folder IS a git repo. Committing + pushing...
echo.
git add -A
git status --short
echo.
git commit -m "fix: notifier fmt_speculation_picks/_split_tg_msg + fmt_watchlist_alert(lines->body) + vol_breakout/chip_anomaly rollback + dt.timezone"
git push
if errorlevel 1 (
  echo.
  echo push failed with default remote, trying: git push origin HEAD
  git push origin HEAD
)
echo.
echo ==========================================
echo   DONE - go to GitHub - Actions and check CI Smoke
echo ==========================================
pause
exit /b

:NOREPO
echo This folder is NOT a git repo yet ^(no .git^).
echo Paste your GitHub repo URL to connect it.
echo   ^(GitHub repo page -^> green Code button -^> HTTPS URL, ends with .git^)
echo.
set /p URL="Repo URL: "
if "%URL%"=="" (
  echo No URL entered. Aborting.
  pause
  exit /b
)
git init
git remote add origin %URL%
git fetch origin
git reset --mixed origin/main
if errorlevel 1 (
  echo.
  echo main branch not found, trying master...
  git reset --mixed origin/master
)
git add -A
git commit -m "fix: notifier fmt_speculation_picks/_split_tg_msg + fmt_watchlist_alert(lines->body) + vol_breakout/chip_anomaly rollback + dt.timezone"
git push -u origin HEAD
echo.
echo ==========================================
echo   DONE - go to GitHub - Actions and check CI Smoke
echo ==========================================
pause
