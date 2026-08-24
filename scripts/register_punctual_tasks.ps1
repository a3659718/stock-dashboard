<#
register_punctual_tasks.ps1 — 把「準時鬧鐘」註冊進 Windows 工作排程器

跑法 (用「以系統管理員身分執行」開 PowerShell):
    cd C:\Users\user\Desktop\Project\stock_dashboard\scripts
    Set-ExecutionPolicy -Scope Process Bypass -Force
    .\register_punctual_tasks.ps1

移除全部:
    .\register_punctual_tasks.ps1 -Remove

原理:
  時間到 → 本機打 GitHub API 的 workflow_dispatch → GitHub 幾乎立刻開跑 → 準時推播。
  GitHub 上原本的 cron 建議「保留」當保險 (電腦沒開機時仍會推,只是會晚),
  重複推播由 push_dedup 的 claim 擋掉 (需要一併部署 market_open_alert.py 的 dedup 修正)。

  每則都比目標時間提早 5 分鐘觸發 —— workflow 要 checkout + pip install + 抓資料,
  大約 2~4 分鐘才會真的送出 Telegram。
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$prefix = 'StockDashboard_'
$script = Join-Path $PSScriptRoot 'gh_dispatch.ps1'

if (-not (Test-Path $script)) { throw "找不到 $script" }

# 名稱, 觸發時間(本機時區,請確認電腦是 UTC+8), workflow 檔名, input 名, input 值, 美東季節
$jobs = @(
  @{ n = 'morning_brief';     t = '07:55'; wf = 'morning_brief.yml';    in = '';       v = '';                  s = 'Any' },
  @{ n = 'pre_market_815';    t = '08:10'; wf = 'pre_market.yml';       in = 'slot';   v = 'pre_market_815';    s = 'Any' },
  @{ n = 'pre_market_830';    t = '08:25'; wf = 'pre_market.yml';       in = 'slot';   v = 'pre_market_830';    s = 'Any' },
  @{ n = 'tw_open';           t = '09:27'; wf = 'market_open_alert.yml'; in = 'market'; v = 'tw_open';          s = 'Any' },
  @{ n = 'tw_mid';            t = '10:57'; wf = 'market_open_alert.yml'; in = 'market'; v = 'tw_mid';           s = 'Any' },
  @{ n = 'tw_close';          t = '14:58'; wf = 'market_open_alert.yml'; in = 'market'; v = 'tw_close';         s = 'Any' },
  @{ n = 'tw_foreign_chips';  t = '16:27'; wf = 'market_open_alert.yml'; in = 'market'; v = 'tw_foreign_chips'; s = 'Any' },
  # 美股 — 台北時間隨美東夏令/冬令位移,兩條都註冊,腳本自己判斷季節、不對就 skip
  @{ n = 'us_emerging_dst';   t = '18:57'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_emerging';      s = 'Dst' },
  @{ n = 'us_emerging_std';   t = '19:57'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_emerging';      s = 'Std' },
  @{ n = 'us_buy_picks_dst';  t = '20:27'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_buy_picks';     s = 'Dst' },
  @{ n = 'us_buy_picks_std';  t = '21:27'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_buy_picks';     s = 'Std' },
  @{ n = 'us_open_dst';       t = '21:57'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_open';          s = 'Dst' },
  @{ n = 'us_open_std';       t = '22:57'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_open';          s = 'Std' },
  @{ n = 'us_mid_dst';        t = '23:27'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_mid';           s = 'Dst' },
  @{ n = 'us_mid_std';        t = '00:27'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_mid';           s = 'Std' },
  @{ n = 'us_close_dst';      t = '05:57'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_close';         s = 'Dst' },
  @{ n = 'us_close_std';      t = '06:57'; wf = 'market_open_alert.yml'; in = 'market'; v = 'us_close';         s = 'Std' }
)

if ($Remove) {
  Get-ScheduledTask | Where-Object { $_.TaskName -like "$prefix*" } | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
    Write-Host "removed $($_.TaskName)"
  }
  Write-Host "`n全部移除完成。"
  return
}

foreach ($j in $jobs) {
  $name = $prefix + $j.n
  $args = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Workflow $($j.wf)"
  if ($j.in) { $args += " -InputName $($j.in) -InputValue $($j.v)" }
  if ($j.s -ne 'Any') { $args += " -UsSeason $($j.s)" }

  $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $args
  $trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At ([datetime]::ParseExact($j.t, 'HH:mm', $null))
  # 關鍵: 電腦剛開機/剛喚醒而錯過時間 → 補跑一次 (但盤前訊息自己會標「補發」)
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

  Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -Description "stock_dashboard 準時觸發: $($j.wf) $($j.v)" -Force | Out-Null
  Write-Host ("registered {0,-38} {1}  {2} {3}" -f $name, $j.t, $j.wf, $j.v)
}

Write-Host "`n完成。驗證: Get-ScheduledTask | Where TaskName -like '$prefix*'"
Write-Host "先手動測一次: .\gh_dispatch.ps1 -Workflow pre_market.yml -InputName slot -InputValue pre_market_830 -WhatIfOnly"
