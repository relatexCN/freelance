"""PyInstaller entry; the executable supports GUI and owned backend subprocesses."""
import multiprocessing
import sys
import traceback
from pathlib import Path

from app.desktop.__main__ import main


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        result = main()
    except Exception:
        # A windowed executable otherwise hides startup errors in a blocking dialog.
        directory = Path.home() / ".freelance"
        if "--backend" in sys.argv:
            directory = Path.cwd()
        if "--workspace" in sys.argv:
            directory = Path(sys.argv[sys.argv.index("--workspace") + 1])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "desktop-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        if sys.platform == "win32" and "--preview" not in sys.argv and "--backend" not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, "桌面客户端启动失败，请查看配置目录内的 desktop-error.log。", "Freelance Gateway", 0x10)
        result = 1
    raise SystemExit(result)
