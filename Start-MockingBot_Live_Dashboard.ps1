$ErrorActionPreference = "Stop"
$env:MOCKINGBOT_DASHBOARD_MODE = "live"
$env:MOCKINGBOT_DATA_DIR = Join-Path $PSScriptRoot "MockingBot_Main_Live_Test_Data"
$env:MOCKINGBOT_DASHBOARD_PORT = "8766"

Push-Location $PSScriptRoot
try {
    python .\MockingBot_Dashboard.py
}
finally {
    Pop-Location
}
