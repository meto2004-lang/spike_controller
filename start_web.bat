@echo off
setlocal
cd /d "%~dp0"
rem Prefer the project's interpreter; never mix python and pythonw from PATH.
set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
"%PYTHON_EXE%" -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>nul
if errorlevel 1 (
    echo Python 3.11 or newer is required.
    echo Selected interpreter: "%PYTHON_EXE%"
    pause
    exit /b 1
)
rem The browser UI is served locally; pybricksdev is optional (simulation mode).
"%PYTHON_EXE%" spike_server.py %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" pause
endlocal & exit /b %EXIT_CODE%
