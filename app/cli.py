import argparse
import uvicorn
import sys
import os
import psutil
import time
import subprocess

import secrets
import re

PID_FILE = "freelance.pid"
LOG_FILE = "freelance.log"

def update_env_file(file_path, key, value):
    """Update or add a key-value pair in a .env file."""
    if not os.path.exists(file_path):
        return False
        
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
        
    pattern = rf"^{key}=.*"
    replacement = f"{key}={value}"
    
    if re.search(pattern, content, flags=re.MULTILINE):
        new_content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
    else:
        # Append if not found
        new_content = content.rstrip() + f"\n{replacement}\n"
        
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True

def keygen():
    """Generate a new secure API key and update .env files."""
    new_key = f"sk-freelance-{secrets.token_urlsafe(32)}"
    print(f"🔑 Generated new API Key: {new_key}")
    
    updated_files = []
    # Update both .env and .env.example
    for filename in [".env", ".env.example"]:
        if update_env_file(filename, "API_KEY", new_key):
            updated_files.append(filename)
            
    if updated_files:
        print(f"✅ Successfully updated: {', '.join(updated_files)}")
        print("⚠️  Please restart the gateway to apply changes if it is currently running.")
    else:
        print("❌ Could not find .env or .env.example to update.")
        print(f"💡 You can manually set: API_KEY={new_key}")

def get_pid():
    if os.path.exists(PID_FILE):
        with open(PID_FILE, "r") as f:
            try:
                return int(f.read().strip())
            except ValueError:
                return None
    return None

def save_pid(pid):
    with open(PID_FILE, "w") as f:
        f.write(str(pid))

def remove_pid():
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)

def stop_process(pid):
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
        return True
    except psutil.NoSuchProcess:
        return False
    except Exception as e:
        print(f"❌ Error stopping process: {e}")
        return False

def start(args):
    old_pid = get_pid()
    if old_pid and psutil.pid_exists(old_pid):
        print(f"⚠️ Freelance API is already running with PID {old_pid}.")
        return

    # Handle Daemon mode
    if args.daemon:
        # Pre-import check before backgrounding
        import importlib
        try:
             importlib.import_module("app.main")
        except Exception as e:
            import traceback
            print(f"\n❌ FATAL: Cannot start background service due to application error:")
            print("-" * 40)
            traceback.print_exc()
            print("-" * 40)
            sys.exit(1)

        print(f"🌙 Starting Freelance API in background Mode...")
        # Construct the command to run without the --daemon flag
        cmd = [sys.executable, "-m", "app.cli", "start", "--host", args.host, "--port", str(args.port)]
        if args.reload:
            cmd.append("--reload")
            
        # Launch detached process
        process = subprocess.Popen(
            cmd,
            stdout=open(LOG_FILE, "a"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
            start_new_session=True
        )
        print(f"✅ Freelance API started in background (PID {process.pid}).")
        print(f"📝 Logs are being recorded in {LOG_FILE}")
        return

    print(f"🚀 Starting Freelance API Gateway on {args.host}:{args.port}...")
    save_pid(os.getpid())
    
    try:
        # Pre-import check to find errors early
        import importlib
        try:
             importlib.import_module("app.main")
        except Exception as e:
            import traceback
            print(f"\n❌ FATAL during application import:")
            print("-" * 40)
            traceback.print_exc()
            print("-" * 40)
            print("💡 Please check your code or environment variables.")
            remove_pid()
            sys.exit(1)

        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level="info"
        )
    except Exception as e:
        import traceback
        print(f"\n❌ FATAL during runtime:")
        traceback.print_exc()
    finally:
        remove_pid()

def stop():
    pid = get_pid()
    if not pid or not psutil.pid_exists(pid):
        print("ℹ️ No Freelance API instance found running.")
        remove_pid()
        return
    
    print(f"🛑 Stopping Freelance API (PID {pid})...")
    if stop_process(pid):
        remove_pid()
        print("✅ Stopped.")
    else:
        print("❌ Failed to stop process.")

def main():
    parser = argparse.ArgumentParser(description="Freelance LLM API Gateway CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 'start' command
    start_parser = subparsers.add_parser("start", help="Start the server")
    start_parser.add_argument("-p", "--port", type=int, default=8000, help="Port (default: 8000)")
    start_parser.add_argument("--host", type=str, default="0.0.0.0", help="Host (default: 0.0.0.0)")
    start_parser.add_argument("--reload", action="store_true", help="Auto-reload")
    start_parser.add_argument("-d", "--daemon", action="store_true", help="Run in background (daemon mode)")

    # 'stop' command
    subparsers.add_parser("stop", help="Stop the running server")

    # 'keygen' command
    subparsers.add_parser("keygen", help="Generate and sync a secure API key for the gateway")

    # 'restart' command
    restart_parser = subparsers.add_parser("restart", help="Restart the server")
    restart_parser.add_argument("-p", "--port", type=int, default=8000, help="Port")
    restart_parser.add_argument("--host", type=str, default="0.0.0.0", help="Host")
    restart_parser.add_argument("--reload", action="store_true", help="Auto-reload")
    restart_parser.add_argument("-d", "--daemon", action="store_true", help="Run in background")

    args = parser.parse_args()

    if args.command == "start":
        start(args)
    elif args.command == "stop":
        stop()
    elif args.command == "keygen":
        keygen()
    elif args.command == "restart":
        stop()
        time.sleep(1)
        start(args)
    else:
        if args.command == "logs":
            print("⚠️ The 'logs' command has been removed. Please check 'freelance.log' directly.")
        else:
            parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
