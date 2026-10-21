@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto :have
echo [setup] Creating virtual environment and installing libraries...
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m venv .venv) else (python -m venv .venv)
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python 3.9+ is required. Install from https://www.python.org/downloads/ and run again.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Library installation failed.
  pause
  exit /b 1
)
:have
echo [setup] Ready.
