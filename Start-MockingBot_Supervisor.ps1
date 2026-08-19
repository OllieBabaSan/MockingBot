$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$logPath = Join-Path $root "MockingBot_Supervisor.log"
$mutexCreated = $false
$mutex = [Threading.Mutex]::new($true, "Local\MockingBotSupervisor", [ref]$mutexCreated)

if (-not $mutexCreated) {
    exit 0
}

function Write-SupervisorLog {
    param([Parameter(Mandatory)][string]$Message)

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
}

function Test-EngineRunning {
    param([Parameter(Mandatory)][string]$LockPath)

    if (-not (Test-Path -LiteralPath $LockPath)) {
        return $false
    }
    try {
        $owner = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json
        $process = Get-Process -Id ([int]$owner.pid) -ErrorAction Stop
        return $process.ProcessName -match '^python'
    }
    catch {
        return $false
    }
}

function Test-ListeningPort {
    param([Parameter(Mandatory)][int]$Port)

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(1000)) {
            return $false
        }
        $client.EndConnect($pending)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Start-HiddenPowerShell {
    param([Parameter(Mandatory)][string]$ScriptPath)

    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath `
        -WorkingDirectory $root -WindowStyle Hidden
}

try {
    if ((Test-Path -LiteralPath $logPath) -and
        (Get-Item -LiteralPath $logPath).Length -gt 1MB) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.previous" -Force
    }

    $python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $python = (Get-Command python.exe -ErrorAction Stop).Source
    }

    $paperLock = Join-Path $root "MockingBot_Data\mockingbot.instance.lock"
    $liveLock = Join-Path $root "MockingBot_Main_Live_Test_Data\mockingbot.instance.lock"
    $mainScript = Join-Path $root "MockingBot.py"
    $liveScript = Join-Path $root "Start-MockingBot_Live.ps1"
    $paperDashboard = Join-Path $root "Start-MockingBot_Paper_Dashboard.ps1"
    $liveDashboard = Join-Path $root "Start-MockingBot_Live_Dashboard.ps1"

    Write-SupervisorLog "Supervisor started for $env:USERDOMAIN\$env:USERNAME"
    while ($true) {
        try {
            if (-not (Test-EngineRunning -LockPath $paperLock)) {
                Write-SupervisorLog "Paper engine absent; starting"
                Start-Process -FilePath $python -ArgumentList $mainScript `
                    -WorkingDirectory $root -WindowStyle Hidden
            }
            if (-not (Test-EngineRunning -LockPath $liveLock)) {
                Write-SupervisorLog "Live engine absent; starting through preflight gate"
                Start-HiddenPowerShell -ScriptPath $liveScript
            }
            if (-not (Test-ListeningPort -Port 8765)) {
                Write-SupervisorLog "Paper dashboard absent; starting"
                Start-HiddenPowerShell -ScriptPath $paperDashboard
            }
            if (-not (Test-ListeningPort -Port 8766)) {
                Write-SupervisorLog "Live dashboard absent; starting"
                Start-HiddenPowerShell -ScriptPath $liveDashboard
            }
        }
        catch {
            Write-SupervisorLog "Recovery cycle failed: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 60
    }
}
finally {
    Write-SupervisorLog "Supervisor stopped"
    if ($mutexCreated) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
