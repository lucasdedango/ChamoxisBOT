param([string]$RepositoryRoot = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
Set-Location $RepositoryRoot
$env:PLEX_ENV_FILE = 'modules/plex/.env'
& '.\.venv-plex\Scripts\python.exe' -m modules.plex.main
exit $LASTEXITCODE
