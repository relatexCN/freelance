import sys
import os
from pathlib import Path

def check_env_api_key():
    env_path = Path(".env")
    if not env_path.exists():
        print("[ERROR] .env file not found!")
        print("[INFO] Please create a .env file with your configuration.")
        print("[INFO] You can copy .env.example to .env and fill in the required values.")
        return False

    with open(env_path, "r", encoding="utf-8") as f:
        found = False
        empty = True
        for line in f:
            line = line.strip()
            if line.startswith("API_KEY="):
                found = True
                if "=" in line:
                    value = line.split("=", 1)[1].strip()
                    # Remove quotes if present
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    empty = len(value) == 0
                break
        
    if found and not empty:
        return True
    else:
        if found:
            print("[ERROR] Gateway API_KEY is empty in .env file.")
        else:
            print("[ERROR] Gateway API_KEY is not defined in .env file.")
        print("[INFO] Please set API_KEY in .env file or run the 'keygen' command to generate one.")
        print("[INFO] Usage: freelance start (will prompt) or freelance keygen")
        return False

if __name__ == "__main__":
    if check_env_api_key():
        sys.exit(0)
    else:
        sys.exit(1)
