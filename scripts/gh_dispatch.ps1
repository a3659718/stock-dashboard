<#
gh_dispatch.ps1 — 準時觸發 GitHub Actions workflow (workflow_dispatch)

為什麼需要這支:
  GitHub Actions 的 `schedule:` (cron) 在共用 runner 上是「盡力而為」,官方明講高負載
  時可能延遲甚至跳過。實測延遲 30 分鐘 ~ 2 小時是常態 → 台股盤前 08:30 的推播
  10 點多才收到。這在 yml 裡怎麼調分鐘都救不了,因為延遲發生在「GitHub 的排程佇列」。

  但 workflow_dispatch (API 手動觸發) 走的是另一條路徑,幾乎是即時排隊執行。
  所以解法是:把「準時的鬧鐘」搬到你自己的電腦 (Windows 工作排程器),
  時間到就打 API 叫 GitHub 跑 —— 運算與 secrets 仍然全部留在 GitHub,本機只負責按鈴。

設定:
  在 %USERPROFILE%\.stock_dashboard_dispatch.json 放一個檔案:
    {
      "repo":  "你的帳號/你的repo名稱",
      "token": "ghp_xxx  (Fine-grained PAT, 只需該 repo 的 Actions: Read and write)",
      "ref":   "main"
    }
  這個檔案不要放進專案資料夾(會被 commit)。建立 PAT: GitHub → Settings →
  Developer settings → Personal access tokens → Fine-grained tokens。

用法:
  .\gh_dispatch.ps1 -Workflow morning_brief.yml
  .\gh_dispatch.ps1 -Workflow pre_market.yml -InputName slot -InputValue pre_market_830
  .\gh_dispatch.ps1 -Workflow market_open_alert.yml -InputName market -InputValue tw_open
  .\gh_dispatch.ps1 -Workflow market_open_alert.yml -InputName market -InputValue us_open -UsSeason Dst
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Workflow,
  [string]$InputName,
  [string]$InputValue,
  # Any = 一律觸發; Dst = 只在美東夏令時觸發; Std = 只在美東冬令時觸發
  [ValidateSet('Any', 'Dst', 'Std')][string]$UsSeason = 'Any',
  # 台股/美股休市日仍然會觸發 — workflow 內部自己有 holiday_check 會 silent skip
  [switch]$WhatIfOnly
)

$ErrorActionPreference = 'Stop'
$logDir = Join-Path $PSScriptRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'dispatch.log'

function Write-Log([string]$msg) {
  $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
  Write-Host $line
  Add-Content -Path $logFile -Value $line -Encoding UTF8
}

# --- 美東夏令/冬令判斷 (us_open/us_mid/us_close 的台北時間會隨之位移) ---
if ($UsSeason -ne 'Any') {
  $etZone = [System.TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time')
  $isDst = $etZone.IsDaylightSavingTime([System.DateTimeOffset]::UtcNow)
  if (($UsSeason -eq 'Dst' -and -not $isDst) -or ($UsSeason -eq 'Std' -and $isDst)) {
    Write-Log "SKIP  $Workflow ($InputValue) — 目前美東 DST=$isDst,不是這條排程的季節"
    exit 0
  }
}

# --- 讀設定 ---
$cfgPath = Join-Path $env:USERPROFILE '.stock_dashboard_dispatch.json'
if (-not (Test-Path $cfgPath)) {
  Write-Log "ERROR 找不到設定檔 $cfgPath — 請先照檔頭說明建立 (repo / token / ref)"
  exit 1
}
$cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $cfg.repo -or -not $cfg.token) {
  Write-Log "ERROR 設定檔缺 repo 或 token"
  exit 1
}
$ref = if ($cfg.ref) { $cfg.ref } else { 'main' }

# --- 組 payload ---
$body = @{ ref = $ref }
if ($InputName) { $body.inputs = @{ $InputName = $InputValue } }
$json = $body | ConvertTo-Json -Depth 5 -Compress
$uri = "https://api.github.com/repos/$($cfg.repo)/actions/workflows/$Workflow/dispatches"

if ($WhatIfOnly) {
  Write-Log "DRYRUN POST $uri  $json"
  exit 0
}

# --- 送出 (3 次重試,網路抖動不該讓當天的推播消失) ---
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$headers = @{
  Authorization          = "Bearer $($cfg.token)"
  Accept                 = 'application/vnd.github+json'
  'X-GitHub-Api-Version' = '2022-11-28'
  'User-Agent'           = 'stock-dashboard-punctual-trigger'
}
for ($i = 1; $i -le 3; $i++) {
  try {
    Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body $json -ContentType 'application/json' | Out-Null
    Write-Log "OK    dispatch $Workflow $InputName=$InputValue (ref=$ref)"
    exit 0
  }
  catch {
    $msg = $_.Exception.Message
    Write-Log "RETRY $i/3 $Workflow $InputValue — $msg"
    if ($i -lt 3) { Start-Sleep -Seconds (5 * $i) }
  }
}
Write-Log "FAIL  dispatch $Workflow $InputValue — 三次都失敗,今天這則只能等 GitHub cron 補推"
exit 1
