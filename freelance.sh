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

# Forward arguments to freelance-api
if [ -z "$1" ]; then
    echo "[USAGE] ./freelance.sh [command] [options]"
    echo "[EXAMPLE] ./freelance.sh start -p 8000"
    echo "[EXAMPLE] ./freelance.sh keygen"
    ./.venv/bin/freelance-api --help
else
    # Correctly forward all arguments
    ./.venv/bin/freelance-api "$@"
fi
