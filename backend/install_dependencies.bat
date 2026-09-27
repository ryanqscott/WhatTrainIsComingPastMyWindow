@echo off
rem Installs the backend's Python packages into .venv (created if missing).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
pause
