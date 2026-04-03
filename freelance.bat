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

:: Forward arguments to freelance-api
if "%1"=="" (
    echo [USAGE] freelance [command] [options]
    echo [EXAMPLE] freelance start -p 8000
    echo [EXAMPLE] freelance keygen
    .venv\Scripts\freelance-api --help
) else (
    .venv\Scripts\freelance-api %*
)
