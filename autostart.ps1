<#
.SYNOPSIS
  P4: register the paper-trial processes with Windows Task Scheduler so they
  start at logon and restart themselves when they die.

.DESCRIPTION
  The 2026-07-23..25 BASE trial ran 58 hours wall-clock with 14.18h dark
  (24.5%), including one unbroken 10.74h gap. During that gap the `looong`
  position was held 13.4h against an 8h max_hold - the exit machine simply was
  not running. Uptime is not a nice-to-have for a strategy whose entire risk
  control is a stop and a time exit.

  Each task runs one strategy runner. Restart-on-failure is set to retry every
  minute, indefinitely, so a crashed runner is back inside 60s instead of
  waiting for someone to notice.

.PARAMETER Repo
  Repo root holding run.py and the configs. Defaults to this script's folder.

.PARAMETER Python
  Python executable. Defaults to whatever `python` resolves to.

.PARAMETER Remove
  Unregister the tasks instead of creating them.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File autostart.ps1
  powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$Repo = $PSScriptRoot,
    [string]$Python = "",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$prefix = "memebot"

# One entry per long-running process. Keep the names stable - they are the
# handle used to stop/start an individual strategy later.
$jobs = @(
    @{ Name = "asym";    Args = "run.py --config config.asym.json" }
    @{ Name = "bounce";  Args = "run.py --config config.bounce.json" }
    @{ Name = "holder";  Args = "run.py --config config.holder.json" }
    @{ Name = "dash";    Args = "dashboard/server.py" }
    @{ Name = "pages";   Args = "pages_publish.py --loop" }
)

if ($Remove) {
    foreach ($j in $jobs) {
        $n = "$prefix-$($j.Name)"
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "removed $n"
        }
    }
    return
}

if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "python not found on PATH; pass -Python <path to python.exe>" }
    $Python = $cmd.Source
}
if (-not (Test-Path (Join-Path $Repo "run.py"))) {
    throw "run.py not found in $Repo - pass -Repo <repo root>"
}

Write-Host "repo:   $Repo"
Write-Host "python: $Python`n"

foreach ($j in $jobs) {
    $name = "$prefix-$($j.Name)"
    $action = New-ScheduledTaskAction -Execute $Python -Argument $j.Args -WorkingDirectory $Repo
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    # StartWhenAvailable catches a machine that was asleep at the trigger time;
    # ExecutionTimeLimit 0 stops Scheduler killing a healthy long-running bot.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartInterval (New-TimeSpan -Minutes 1) `
        -RestartCount 999 -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Description "memebot paper trial: $($j.Args)" | Out-Null
    Write-Host "registered $name  ->  $Python $($j.Args)"
}

Write-Host "`nStart them now without logging out:"
foreach ($j in $jobs) { Write-Host "  Start-ScheduledTask -TaskName $prefix-$($j.Name)" }
Write-Host "`nCheck status:  Get-ScheduledTask -TaskName '$prefix-*' | Get-ScheduledTaskInfo"
