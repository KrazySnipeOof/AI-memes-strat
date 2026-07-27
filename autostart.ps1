<#
.SYNOPSIS
  Register the paper-trial processes with Windows Task Scheduler so they start
  at logon and restart themselves when they die.

.DESCRIPTION
  The 2026-07-23..25 BASE trial ran 58 hours wall-clock with 14.18h dark
  (24.5%), including one unbroken 10.74h gap. During that gap the `looong`
  position was held 13.4h against an 8h max_hold - the exit machine simply was
  not running. On 2026-07-26 HOMERUN died at 14:49 and sat dead ~9h unnoticed.
  Uptime is not a nice-to-have for a strategy whose entire risk control is a
  stop and a time exit.

  Each task runs one long-running process. Restart-on-failure retries every
  minute, indefinitely, so a crashed runner is back inside 60s.

  NOTE ON WORKTREE RUNNERS: bounce and holder are launched from the
  paper-bounce-holder worktree with `--workdir <main repo>` so their sqlite and
  logs land in the main tree next to the others. Deleting that worktree kills
  them. After PR #1 merges, drop the -WorkingDirectory override and the
  --workdir flag for those two.

.PARAMETER Repo
  Repo root holding run.py and the configs. Defaults to this script's folder.

.PARAMETER Python
  Python executable. Defaults to whatever `python` resolves to.

.PARAMETER Remove
  Unregister the tasks instead of creating them.

.PARAMETER StartNow
  Also start every task immediately after registering.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File autostart.ps1
  powershell -ExecutionPolicy Bypass -File autostart.ps1 -StartNow
  powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$Repo = $PSScriptRoot,
    [string]$Python = "",
    [switch]$Remove,
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"
$prefix = "memebot"

# $PSScriptRoot can come back empty (or as the profile dir) when this file is
# invoked via a nested `powershell -File`, so fall back to the script's real
# path and only then to the cwd.
if (-not $Repo -or -not (Test-Path (Join-Path $Repo "run.py"))) {
    $self = $MyInvocation.MyCommand.Path
    if ($self) { $Repo = Split-Path -Parent $self }
}
if (-not $Repo -or -not (Test-Path (Join-Path $Repo "run.py"))) { $Repo = (Get-Location).Path }
$worktree = Join-Path $Repo ".claude\worktrees\paper-bounce-holder"

# One entry per long-running process. Names are stable - they are the handle
# used to stop/start an individual strategy later. Cwd defaults to $Repo.
$jobs = @(
    @{ Name = "asym";    Args = "run.py --config config.asym.json" }
    @{ Name = "homerun"; Args = "run.py --config config.homerun.json" }
    @{ Name = "grinder"; Args = "run.py --config config.grinder.json" }
    @{ Name = "bounce";  Args = "run.py --config config.bounce.json --workdir `"$Repo`""; Cwd = $worktree }
    @{ Name = "holder";  Args = "run.py --config config.holder.json --workdir `"$Repo`""; Cwd = $worktree }
    @{ Name = "dash";    Args = "dashboard/server.py --config config.asym.json" }
    @{ Name = "pages";   Args = "pages_publish.py --loop 5" }
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
if (-not (Test-Path $worktree)) {
    Write-Warning "worktree not found: $worktree - bounce/holder tasks will fail until it exists"
}

Write-Host "repo:   $Repo"
Write-Host "python: $Python`n"

foreach ($j in $jobs) {
    $name = "$prefix-$($j.Name)"
    $cwd = if ($j.Cwd) { $j.Cwd } else { $Repo }
    $action = New-ScheduledTaskAction -Execute $Python -Argument $j.Args -WorkingDirectory $cwd
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

if ($StartNow) {
    Write-Host ""
    foreach ($j in $jobs) {
        $name = "$prefix-$($j.Name)"
        try { Start-ScheduledTask -TaskName $name; Write-Host "started $name" }
        catch { Write-Warning "could not start ${name}: $_" }
    }
} else {
    Write-Host "`nStart them now without logging out:"
    foreach ($j in $jobs) { Write-Host "  Start-ScheduledTask -TaskName $prefix-$($j.Name)" }
}

Write-Host "`nCheck status:  Get-ScheduledTask -TaskName '$prefix-*' | Get-ScheduledTaskInfo"
