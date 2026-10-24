# Author: Kai0421
<#
Registers a Task Scheduler task that runs the engine
scheduler every 5 minutes (the engine decides in New York time which job is due).

Run from an elevated PowerShell (Run as administrator) so the task can run hidden, with no
console window popping up every 5 minutes:
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1                   install / reinstall
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -Uninstall
  powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1 -OvernightShadow  research-only shadow log (docs/19-overnight-hold.md)
  ... -HealthReport       weekday 08:00 health report to Slack (reads only, scripts/health_report.py)
  ... -SentimentCollect   Stocktwits collector every 30 min (docs/20-sentiment.md); most runs end without calling Claude
  ... -CouncilShadow      Council of Ricks shadow log every 30 min (docs/21-sentiment-council.md), no orders
Add -Interactive to run only while you are signed in (no admin needed, a window flashes each run).

Never run this and the GitHub Actions engine (or any other scheduler) at the same time.
#>
param(
    [switch]$Uninstall,
    [switch]$OvernightShadow,
    [switch]$SentimentCollect,
    [switch]$CouncilShadow,
    [switch]$HealthReport,
    [switch]$Interactive
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$folder = "\StockTradingBot\"
if ($OvernightShadow) {
    $name = "overnight-shadow"; $minutes = 30
    $cmd = Join-Path $repo "scripts\run_overnight_shadow.cmd"; $cmdArgs = ""
} elseif ($SentimentCollect) {
    $name = "sentiment-collect"; $minutes = 30
    $cmd = Join-Path $repo "scripts\run_sentiment_collect.cmd"; $cmdArgs = ""
} elseif ($CouncilShadow) {
    $name = "council-shadow"; $minutes = 30
    $cmd = Join-Path $repo "scripts\run_council_shadow.cmd"; $cmdArgs = ""
} elseif ($HealthReport) {
    $name = "health-report"; $minutes = 0
    $cmd = Join-Path $repo "scripts\run_health_report.cmd"; $cmdArgs = ""
} else {
    $name = "tick"; $minutes = 5
    $cmd = Join-Path $repo "scripts\run_job.cmd"; $cmdArgs = " tick"
}

Unregister-ScheduledTask -TaskPath $folder -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
if ($Uninstall) {
    Write-Host "uninstalled $folder$name"
    exit 0
}

New-Item -ItemType Directory -Force -Path (Join-Path $repo "logs") | Out-Null
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$cmd`"$cmdArgs`"" -WorkingDirectory $repo
if ($HealthReport) {
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 8:00AM
} else {
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $minutes)
}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$logon = if ($Interactive) { "Interactive" } else { "S4U" }
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType $logon -RunLevel Limited
Register-ScheduledTask -TaskPath $folder -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal | Out-Null
$when = if ($HealthReport) { "weekdays 08:00 local time" } else { "every $minutes minutes" }
Write-Host "installed $folder$name ($when, logs in $repo\logs\)"
