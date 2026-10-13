"""Desktop-owned backend entry: no shared freelance.pid and loopback only."""
import argparse
import sys
import uvicorn


def main():
    # Windowed frozen executables have no Python standard streams.
    if sys.stdout is None:
        sys.stdout = open("desktop-launch.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
