#!/bin/bash

# Function to check for Python
check_python() {
    if command -v python3 &>/dev/null; then
        echo "python3"
    elif command -v python &>/dev/null; then
        echo "python"
    else
        echo ""
    fi
}

PYTHON_CMD=$(check_python)

if [ -z "$PYTHON_CMD" ]; then
    echo "[ERROR] Python not found. Please install Python 3.8 or higher."
    exit 1
fi

# Check for .venv
if [ ! -d ".venv" ]; then
    echo "[INFO] Creating virtual environment..."
    $PYTHON_CMD -m venv .venv
    if [ $? -ne 0 ]; then
        echo "[ERROR] Failed to create virtual environment."
        exit 1
    fi
    echo "[SUCCESS] Virtual environment created."
    
    echo "[INFO] Installing dependencies..."
    ./.venv/bin/python -m pip install --upgrade pip
    ./.venv/bin/pip install -e .
    if [ $? -ne 0 ]; then
        echo "[ERROR] Failed to install dependencies."
        exit 1
    fi
    echo "[SUCCESS] Dependencies installed."
fi

# Check if running start command (explicit or default)
is_start_cmd=0
if [ "$1" = "start" ] || [ -z "$1" ]; then
    is_start_cmd=1
fi

# Check .env file and API_KEY for start command
if [ $is_start_cmd -eq 1 ]; then
    # Use Python script for consistent cross-platform checking
    bash check_api_key.sh
    if [ $? -ne 0 ]; then
        exit 1
    fi
fi

# Determine freelance-api path based on OS
if [ -f "./.venv/Scripts/freelance-api.exe" ]; then
    FREELANCE_API="./.venv/Scripts/freelance-api.exe"
elif [ -f "./.venv/bin/freelance-api" ]; then
    FREELANCE_API="./.venv/bin/freelance-api"
else
    echo "[ERROR] freelance-api not found in virtual environment."
    exit 1
fi

# Forward arguments to freelance-api
if [ -z "$1" ]; then
    # Default to start command when no arguments provided
    $FREELANCE_API start
else
    # Correctly forward all arguments
    $FREELANCE_API "$@"
fi
