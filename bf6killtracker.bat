@echo off
rem Windows launcher: sets up a virtual environment on first run, then runs the tracker.
rem Double-click to open the app, or from a terminal:
rem   bf6killtracker.bat analyze "C:\Videos\clip.mp4" --reel highlights.mp4
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"
if exist "%HERE%.venv\installed.ok" goto run

echo Setting up Python environment (first run only)...
if exist "%PY%" goto deps
py -3 -m venv "%HERE%.venv"
if not exist "%PY%" python -m venv "%HERE%.venv"
if not exist "%PY%" (
    echo Python 3.9+ is required: https://www.python.org/downloads/
    exit /b 1
)

:deps
"%PY%" -m pip install --upgrade pip >nul
"%PY%" -m pip install -r "%HERE%requirements.txt"
if errorlevel 1 exit /b 1
echo ok> "%HERE%.venv\installed.ok"

:run
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
if "%~1"=="" (
    rem no arguments: start the desktop app without a console window
    start "" "%HERE%.venv\Scripts\pythonw.exe" -m bf6killtracker gui
    exit /b 0
)
"%PY%" -m bf6killtracker %*
