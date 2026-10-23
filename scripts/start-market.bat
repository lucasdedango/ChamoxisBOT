@echo off
cd /d "%~dp0.."
set "MARKET_ENV_FILE=modules/market/.env"
if not exist ".venv-market\Scripts\python.exe" (
  echo Environnement Market absent. Consulte docs\market.md pour l'installation.
  pause
  exit /b 1
)
".venv-market\Scripts\python.exe" -m modules.market.main
if errorlevel 1 pause
