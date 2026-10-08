param([string]$RepositoryRoot = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
Set-Location $RepositoryRoot
function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $Executable (exit $LASTEXITCODE)" }
}
Invoke-Checked -Executable 'py' -Arguments @('-3.12', '-m', 'venv', '.venv-manager')
Invoke-Checked -Executable 'py' -Arguments @('-3.12', '-m', 'venv', '.venv-plex')
Invoke-Checked -Executable '.\.venv-manager\Scripts\python.exe' -Arguments @('-m', 'pip', 'install', '-c', 'constraints.txt', '-r', 'manager/requirements.txt', '-r', 'tests/requirements.txt')
Invoke-Checked -Executable '.\.venv-plex\Scripts\python.exe' -Arguments @('-m', 'pip', 'install', '-c', 'constraints.txt', '-r', 'modules/plex/requirements.txt')
foreach ($Pair in @(@('manager/.env.example', 'manager/.env'), @('modules/plex/.env.example', 'modules/plex/.env'), @('config/services.example.json', 'config/services.json'))) {
    if (-not (Test-Path $Pair[1])) { Copy-Item $Pair[0] $Pair[1] }
}
Invoke-Checked -Executable '.\.venv-manager\Scripts\python.exe' -Arguments @('-m', 'unittest', 'discover', '-s', 'tests', '-v')
Write-Host 'Configure the two local .env files and services.json before starting. Downloads are disabled by default.'
