import argparse
import sys
from pathlib import Path


def main():
    # Frozen builds reuse the executable as a headless backend child.
    if "--backend" in sys.argv[1:]:
        sys.argv.remove("--backend")
        from .backend import main as backend_main
        backend_main()
        return 0
    parser = argparse.ArgumentParser(description="Freelance desktop gateway client")
    parser.add_argument("--workspace", type=Path, help="Override the program's local configuration directory")
    parser.add_argument("--preview", type=Path, help="Save a window preview and exit (for UI verification)")
    args = parser.parse_args()
    if args.workspace is None:
        from .workspace import default_workspace
        args.workspace = default_workspace()
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QFont, QFontDatabase
        from .window import MainWindow
    except ImportError as exc:
        if exc.name and exc.name.startswith("PySide6"):
            print('Desktop dependency missing. Install: python -m pip install -e ".[desktop]"', file=sys.stderr)
            return 1
        raise
    application = QApplication(sys.argv[:1])
    application.setApplicationName("Freelance Gateway")
    if sys.platform == "win32":
        # Explicit font also works in offscreen validation where fallback is absent.
        import os
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
        if font_path.exists():
            QFontDatabase.addApplicationFont(str(font_path))
        application.setFont(QFont("Microsoft YaHei UI", 10))
    window = MainWindow(args.workspace)
    window.show()
    if args.preview:
        from PySide6.QtCore import QTimer

        def preview():
            args.preview.parent.mkdir(parents=True, exist_ok=True)
            application.exit(0 if window.grab().save(str(args.preview)) else 1)
        QTimer.singleShot(150, preview)
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
