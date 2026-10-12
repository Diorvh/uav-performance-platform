@echo off
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0launch.py"
) else (
    python "%~dp0launch.py"
    if errorlevel 1 pause
)
