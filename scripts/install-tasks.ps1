# Register the agent's two loops with Windows Task Scheduler.
#
# Task Scheduler is the supervisor, not a daemon we wrote: it already restarts a
# crashed process and starts one at logon, which is the whole job. A `--daemon`
# subcommand would be ~60 lines re-implementing that badly.
#
# The one thing it must NOT do is restart a rejected password into a loop -
# `--channel` exits 2 on ChannelRefused, and RestartCount stops after 3 tries so
# a permanent failure stops rather than spinning.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1 -Remove

[CmdletBinding()]
param(
    [switch]$Remove,
    [string]$ProjectDir,
    [string]$Python
)

$ErrorActionPreference = "Stop"

# NOT param defaults: $PSScriptRoot is empty during param binding on PS 5.1.
if (-not $ProjectDir) { $ProjectDir = Split-Path -Parent $PSScriptRoot }
if (-not $Python)     { $Python = (Get-Command python).Source }
$tasks = @("PersonalAgent Channel", "PersonalAgent Worker")

if ($Remove) {
    foreach ($name in $tasks) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "removed  $name"
        }
    }
    return
}

if (-not (Test-Path (Join-Path $ProjectDir "agent\cli.py"))) {
    throw "not a Noesis checkout: $ProjectDir"
}
if (-not (Test-Path (Join-Path $ProjectDir ".env"))) {
    Write-Warning ".env not found - the channel needs AGENT_EMAIL_USER / _PASSWORD / _ALLOW"
}

# Verify BEFORE registering. A task that starts and immediately dies looks
# identical in the UI to one that is working.
Write-Host "checking preconditions..."
& $Python -m agent --doctor
if ($LASTEXITCODE -ne 0) {
    throw "--doctor reported FAIL; fix that before registering the tasks"
}

foreach ($spec in @(
    @{ Name = $tasks[0]; Arg = "--channel" },
    @{ Name = $tasks[1]; Arg = "--worker"  }
)) {
    $action = New-ScheduledTaskAction -Execute $Python `
        -Argument "-m agent $($spec.Arg)" -WorkingDirectory $ProjectDir

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    # RestartCount 3, not indefinite: a rejected password or a missing image is
    # permanent, and an unbounded restart is how a broken thing looks healthy.
    # ExecutionTimeLimit 0 = never kill a long-running agent turn.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 3 `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $spec.Name -Action $action `
        -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "registered  $($spec.Name)  ->  python -m agent $($spec.Arg)"
}

Write-Host ""
Write-Host "Both start at logon. To start them now without logging out:"
Write-Host "  Start-ScheduledTask -TaskName '$($tasks[0])'"
Write-Host "  Start-ScheduledTask -TaskName '$($tasks[1])'"
Write-Host ""
Write-Host "Watch them:   Get-ScheduledTask -TaskName 'PersonalAgent*' | Get-ScheduledTaskInfo"
Write-Host "Stop one:     Stop-ScheduledTask -TaskName '$($tasks[0])'"
Write-Host "Remove both:  powershell -File scripts\install-tasks.ps1 -Remove"
