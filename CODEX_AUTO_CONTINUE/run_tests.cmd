@echo off
cd /d "%~dp0"
rem Runs unit/scenario tests. Nothing is sent to Codex.
if exist ".venv\Scripts\python.exe" (set PY=".venv\Scripts\python.exe") else (set PY=python)
%PY% -m unittest discover -s tests -v
pause
