param([string]$RepositoryRoot = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
Set-Location $RepositoryRoot
$env:MARKET_ENV_FILE = 'modules/market/.env'
& '.\.venv-market\Scripts\python.exe' -m modules.market.main
exit $LASTEXITCODE
