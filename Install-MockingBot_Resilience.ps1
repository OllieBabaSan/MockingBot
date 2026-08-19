$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$resultPath = Join-Path $root "MockingBot_Resilience_Install.log"
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $isAdmin) {
    throw "This installer requires Administrator privileges."
}

try {
    $policyPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"
    New-Item -Path $policyPath -Force | Out-Null
    New-ItemProperty -Path $policyPath -Name "NoAutoUpdate" `
        -PropertyType DWord -Value 0 -Force | Out-Null
    New-ItemProperty -Path $policyPath -Name "AUOptions" `
        -PropertyType DWord -Value 2 -Force | Out-Null
    New-ItemProperty -Path $policyPath -Name "NoAutoRebootWithLoggedOnUsers" `
        -PropertyType DWord -Value 1 -Force | Out-Null
    New-ItemProperty -Path $policyPath -Name "AlwaysAutoRebootAtScheduledTime" `
        -PropertyType DWord -Value 0 -Force | Out-Null

    & gpupdate.exe /target:computer /force | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "gpupdate failed with exit code $LASTEXITCODE"
    }

    & (Join-Path $root "Register-MockingBot_Autostart.ps1")
    if ($LASTEXITCODE -notin 0, $null) {
        throw "Autostart registration failed with exit code $LASTEXITCODE"
    }

    @(
        "Installed: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        "AUOptions=2"
        "NoAutoUpdate=0"
        "NoAutoRebootWithLoggedOnUsers=1"
        "AlwaysAutoRebootAtScheduledTime=0"
        "ScheduledTask=MockingBot Supervisor"
    ) | Set-Content -LiteralPath $resultPath -Encoding utf8
}
catch {
    "FAILED: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $($_.Exception.Message)" |
        Set-Content -LiteralPath $resultPath -Encoding utf8
    throw
}
