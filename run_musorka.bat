@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo First run install_and_run.bat
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.py
