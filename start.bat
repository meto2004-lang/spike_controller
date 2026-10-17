@echo off
setlocal
cd /d "%~dp0"
rem Prefer the project's interpreter; never mix python and pythonw from PATH.
set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
"%PYTHON_EXE%" -c "import sys; assert sys.version_info >= (3, 11); from PySide6.QtWidgets import QApplication; from pybricksdev.connections.pybricks import PybricksHubBLE, PybricksHubUSB; import serial, libusb_package" >nul 2>nul
if errorlevel 1 (
    echo Python 3.11 or newer with PySide6/Qt and runtime dependencies is required.
    echo Selected interpreter: "%PYTHON_EXE%"
    echo Run: "%PYTHON_EXE%" -m pip install -r requirements.txt
    pause
    exit /b 1
)
rem Keep errors visible and preserve the application's exit status.
"%PYTHON_EXE%" spike_controller.py
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
endlocal & exit /b %EXIT_CODE%
