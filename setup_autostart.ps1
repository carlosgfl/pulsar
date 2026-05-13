# Registers Pulsar capture as a Task Scheduler daemon for the current user.
# Run once from an elevated PowerShell prompt (right-click → Run as Administrator).
# After setup, the daemon starts automatically at every logon — no CMD window.

$projectDir = $PSScriptRoot
$pythonw    = Join-Path $projectDir "env\Scripts\pythonw.exe"
$script     = Join-Path $projectDir "capture.py"
$taskName   = "PulsarCapture"

if (-not (Test-Path $pythonw)) {
    Write-Error "pythonw.exe not found at $pythonw — make sure the venv is set up."
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute    $pythonw `
    -Argument   "`"$script`"" `
    -WorkingDirectory $projectDir

# Trigger: at logon of the current user
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName   $taskName `
    -Action     $action `
    -Trigger    $trigger `
    -Settings   $settings `
    -Description "Pulsar activity capture — background screenshot daemon" `
    -Force | Out-Null

Write-Host "Registered task '$taskName'."
Write-Host ""
Write-Host "Starting now..."
Start-ScheduledTask -TaskName $taskName
Write-Host "Pulsar is running in the background. Logs: $projectDir\logs\capture.log"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Get-ScheduledTask  -TaskName $taskName   # check status"
Write-Host "  Stop-ScheduledTask -TaskName $taskName   # stop"
Write-Host "  Start-ScheduledTask -TaskName $taskName  # start"
Write-Host "  .\remove_autostart.ps1                   # uninstall"
