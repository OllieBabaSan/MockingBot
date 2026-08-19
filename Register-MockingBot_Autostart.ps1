$ErrorActionPreference = "Stop"

$taskName = "MockingBot Supervisor"
$scriptPath = Join-Path $PSScriptRoot "Start-MockingBot_Supervisor.ps1"
$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $isAdmin) {
    throw "Run this registration script from PowerShell as Administrator."
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $scriptPath
)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description (
        "Keeps MockingBot Paper, Live, and both local dashboards running after user logon."
    ) -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "Registered and started scheduled task: $taskName"
