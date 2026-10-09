@echo off
cd /d "%~dp0"
start "ChamoxisBOT - Plex" cmd /k "set PLEX_ENV_FILE=modules/plex/.env&& .venv-plex\Scripts\python.exe -m modules.plex.main"
start "ChamoxisBOT - Manager" cmd /k "set MANAGER_ENV_FILE=manager/.env&& .venv-manager\Scripts\python.exe -m manager.main"
