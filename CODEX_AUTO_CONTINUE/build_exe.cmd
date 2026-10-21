@echo off
cd /d "%~dp0"
rem Optional: build a single exe (dist\CODEX_AUTO_CONTINUE.exe)
if not exist ".venv\Scripts\python.exe" call "%~dp0setup.cmd"
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconsole --onefile --name CODEX_AUTO_CONTINUE --collect-submodules pywinauto --collect-submodules comtypes run_gui.pyw
pause
