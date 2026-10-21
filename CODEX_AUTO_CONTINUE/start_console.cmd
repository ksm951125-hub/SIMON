@echo off
cd /d "%~dp0"
rem Console mode (shows logs). Options: --dry-run / --test-mode
if not exist ".venv\Scripts\python.exe" call "%~dp0setup.cmd"
".venv\Scripts\python.exe" -m codex_auto_continue run %*
pause
