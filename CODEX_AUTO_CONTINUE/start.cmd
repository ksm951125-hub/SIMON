@echo off
cd /d "%~dp0"
rem Runs in the system tray without a console window.
rem Options:  start.cmd --dry-run   /   start.cmd --test-mode
if not exist ".venv\Scripts\pythonw.exe" call "%~dp0setup.cmd"
if not exist ".venv\Scripts\pythonw.exe" exit /b 1
start "" ".venv\Scripts\pythonw.exe" "%~dp0run_gui.pyw" %*
