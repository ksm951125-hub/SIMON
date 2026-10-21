@echo off
cd /d "%~dp0"
rem STEP 1: analyze the running Codex app (read-only: no typing, no clicking).
if not exist ".venv\Scripts\python.exe" call "%~dp0setup.cmd"
".venv\Scripts\python.exe" -m codex_auto_continue inspect
pause
