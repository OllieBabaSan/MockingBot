$ErrorActionPreference = "Stop"
$env:MOCKINGBOT_DASHBOARD_MODE = "paper"
$env:MOCKINGBOT_DATA_DIR = Join-Path $PSScriptRoot "MockingBot_Data"
$env:MOCKINGBOT_DASHBOARD_PORT = "8765"

Push-Location $PSScriptRoot
try {
    python .\MockingBot_Dashboard.py
}
finally {
    Pop-Location
}
