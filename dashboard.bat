@echo off
cd /d "%~dp0"
if not exist ".venv-manager\Scripts\python.exe" (
  echo Environnement Python absent. Creez .venv-manager comme indique dans docs\windows.md.
  pause
  exit /b 1
)
".venv-manager\Scripts\python.exe" -m dashboard.main
pause
