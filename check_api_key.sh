#!/bin/bash
# Check if API_KEY is properly set in .env file

if [ ! -f ".env" ]; then
    echo "[ERROR] .env file not found!"
    echo "[INFO] Please create a .env file with your configuration."
    echo "[INFO] You can copy .env.example to .env and fill in the required values."
    exit 1
fi

# Determine Python path based on OS
if [ -f "./.venv/Scripts/python.exe" ]; then
    PYTHON_CMD="./.venv/Scripts/python.exe"
elif [ -f "./.venv/bin/python" ]; then
    PYTHON_CMD="./.venv/bin/python"
else
    echo "[ERROR] Python not found in virtual environment."
    exit 1
fi

# Use Python for consistent cross-platform checking
$PYTHON_CMD -c "
import sys
with open('.env', 'r', encoding='utf-8') as f:
    found = False
    empty = True
    for line in f:
        if line.strip().startswith('API_KEY='):
            found = True
            if '=' in line:
                value = line.strip().split('=', 1)[1].strip()
                empty = len(value) == 0
            else:
                empty = True
            break
if found and not empty:
    sys.exit(0)
else:
    if found:
        print('[ERROR] Gateway API_KEY is empty in .env file.')
    else:
        print('[ERROR] Gateway API_KEY is not defined in .env file.')
    print('[INFO] Please set API_KEY in .env file or run \"./freelance.sh keygen\" to generate one.')
    sys.exit(1)
"
