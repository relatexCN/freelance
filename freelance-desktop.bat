@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m app.desktop %*
) else (
    python -m app.desktop %*
)
if errorlevel 1 (
    echo Install desktop dependencies first: python -m pip install -e ".[desktop]"
    pause
)
