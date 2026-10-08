param([string]$RepositoryRoot = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
Set-Location $RepositoryRoot
$env:MANAGER_ENV_FILE = 'manager/.env'
& '.\.venv-manager\Scripts\python.exe' -m manager.main
exit $LASTEXITCODE
