@echo off
setlocal
cd /d "%~dp0"
rem Prefer the portable build; it includes Python and all desktop dependencies.
if exist "dist\FreelanceDesktop\FreelanceDesktop.exe" (
    "dist\FreelanceDesktop\FreelanceDesktop.exe" %*
    exit /b
)
if exist "FreelanceDesktop\FreelanceDesktop.exe" (
    "FreelanceDesktop\FreelanceDesktop.exe" %*
    exit /b
)
set "desktop_python=python"
if exist ".venv\Scripts\python.exe" (
    set "desktop_python=%~dp0.venv\Scripts\python.exe"
)
"%desktop_python%" -c "from PySide6.QtWidgets import QApplication" >nul 2>&1
if errorlevel 1 (
    echo The selected Python cannot load desktop dependencies:
    "%desktop_python%" -c "import sys; print(sys.executable)"
    echo Install into this same environment:
    echo "%desktop_python%" -m pip install -e ".[desktop]"
    echo Or extract the portable package and run FreelanceDesktop.exe.
    pause
    exit /b 1
)
"%desktop_python%" -m app.desktop %*
if errorlevel 1 (
    echo Desktop startup failed. See the error above.
    pause
    exit /b 1
)
