$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
python .\MockingBot.py start-live
exit $LASTEXITCODE
