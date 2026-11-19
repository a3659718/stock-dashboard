<#
setup_cronjob_org.ps1 — 互動式設定 cron-job.org 準時觸發 (金鑰只在這個視窗的記憶體裡, 不寫檔、不留紀錄)

用法 (在專案資料夾):
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_cronjob_org.ps1

需要先準備好:
  1. cron-job.org API key   : https://console.cron-job.org/settings  → API → Create API key
  2. GitHub Fine-grained PAT: https://github.com/settings/personal-access-tokens/new
       Repository access = Only select repositories → stock-dashboard
       Permissions → Repository permissions → Actions = Read and write
#>
param(
  [string]$Repo = 'a3659718/stock-dashboard',
  [switch]$Delete
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

function Read-Secret([string]$prompt) {
  $sec = Read-Host -Prompt $prompt -AsSecureString
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr).Trim() }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host "`n=== cron-job.org 準時觸發設定 ($Repo) ===`n"
Write-Host "輸入時畫面不會顯示字元, 直接貼上後按 Enter 即可。`n"
$env:CRONJOB_API_KEY = Read-Secret 'cron-job.org API key'
try {
  if ($Delete) {
    py -3 scripts\setup_cronjob_org.py --delete
  } else {
    $env:GH_DISPATCH_TOKEN = Read-Secret 'GitHub Fine-grained token (github_pat_...)'
    py -3 scripts\setup_cronjob_org.py --repo $Repo
  }
  $code = $LASTEXITCODE
} finally {
  Remove-Item Env:CRONJOB_API_KEY -ErrorAction SilentlyContinue
  Remove-Item Env:GH_DISPATCH_TOKEN -ErrorAction SilentlyContinue
}
if ($code -eq 0) { Write-Host "`n完成。可以關閉這個分頁。" -ForegroundColor Green }
else { Write-Host "`n沒有完成 (exit $code)。看上面的錯誤訊息。" -ForegroundColor Yellow }
