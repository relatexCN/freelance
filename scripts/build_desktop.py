"""Build a Windows portable directory without bundling user config or credentials."""
import subprocess
import sys
import os
import importlib.util
import argparse
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build the portable desktop client")
    parser.add_argument("--distpath", type=Path, default=root / "dist", help="Build into another directory while an older executable is running")
    args = parser.parse_args()
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
               "--name", "FreelanceDesktop", "--distpath", str(args.distpath.resolve()),
               "--workpath", str(root / "build" / "desktop"), "--specpath", str(root / "build"),
               "--paths", str(root), "--collect-submodules", "app", "--collect-submodules", "uvicorn",
               "--collect-data", "app.desktop", "--exclude-module", "PySide6.QtWebEngineCore",
               "--exclude-module", "PySide6.QtWebEngineWidgets", "--exclude-module", "PySide6.QtWebEngineQuick",
               str(root / "scripts" / "desktop_entry.py")]
    environment = os.environ.copy()
    if os.name == "nt":
        # Tool installations can put ABI-incompatible ICU DLLs on PATH. Qt uses
        # Windows' system ICU; let analysis resolve it before unrelated tools.
        system = Path(environment.get("WINDIR", "C:/Windows"))
        package = Path(next(iter(importlib.util.find_spec("PySide6").submodule_search_locations)))
        environment["PATH"] = os.pathsep.join(str(p) for p in (
            Path(sys.executable).parent, system / "System32", system, package, package.parent / "shiboken6"))
    return subprocess.call(command, cwd=root, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
