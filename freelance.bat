@echo off
SETLOCAL EnableDelayedExpansion

:: Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.8 or higher.
    exit /b 1
)

:: Check for .venv
if not exist .venv (
    echo [INFO] Creating virtual environment...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        exit /b 1
    )
    echo [SUCCESS] Virtual environment created.
    
    echo [INFO] Installing dependencies...
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\pip install -e .
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        exit /b 1
    )
    echo [SUCCESS] Dependencies installed.
)

:: Check if running start command (explicit or default)
set "is_start_cmd=0"
if /i "%1"=="start" set "is_start_cmd=1"
if "%1"=="" set "is_start_cmd=1"

:: For start command, check API_KEY
if %is_start_cmd% equ 1 (
    .venv\Scripts\python scripts\check_gateway_key.py
    if %errorlevel% neq 0 (
        exit /b 1
    )
    set "PYTHONPATH=%CD%"
    .venv\Scripts\python -m app.cli %*
    ENDLOCAL
    exit /b %errorlevel%
)

:: For non-start commands, forward arguments directly
if "%1"=="" (
    echo [USAGE] freelance [command] [options]
    echo [EXAMPLE] freelance start -p 8000
    echo [EXAMPLE] freelance keygen
    set "PYTHONPATH=%CD%"
    .venv\Scripts\python -m app.cli --help
) else (
    set "PYTHONPATH=%CD%"
    .venv\Scripts\python -m app.cli %*
)
