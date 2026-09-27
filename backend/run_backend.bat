@echo off
rem Starts the backend. Credentials and data file paths are read from .env.
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe main.py
) else (
    python main.py
)
pause
