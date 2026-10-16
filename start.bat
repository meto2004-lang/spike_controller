@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw spike_controller.py
) else (
    python spike_controller.py
    pause
)
