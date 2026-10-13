"""Smoke-test the actual frozen GUI and its headless local backend, no upstream calls."""
import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.desktop.client import GatewayClient
from app.desktop.local import LocalGateway, prepare_workspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, default=Path("dist/FreelanceDesktop/FreelanceDesktop.exe"))
    args = parser.parse_args()
    executable = args.executable.resolve()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        preview = root / "preview.png"
        environment = os.environ.copy()
        environment["QT_QPA_PLATFORM"] = "offscreen"
        result = subprocess.run([str(executable), "--workspace", str(root), "--preview", str(preview)],
                                env=environment, timeout=30)
        if result.returncode or not preview.exists():
            error = root / "desktop-error.log"
            raise RuntimeError("Frozen UI preview failed: " + (error.read_text(encoding="utf-8") if error.exists() else str(result.returncode)))
        print("Frozen GUI preview passed")
        root, keys = prepare_workspace(root)
        (root / "config.yaml").write_text("providers: {}\nmodels: {}\n", encoding="utf-8")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        gateway = LocalGateway(root)
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)):
            try:
                gateway.start(port)
                client = GatewayClient(f"http://127.0.0.1:{port}", keys["API_KEY"], keys["ADMIN_API_KEY"])
                deadline = time.monotonic() + 20
                ready = False
                while time.monotonic() < deadline:
                    try:
                        ready = client.request("GET", "/admin/status", admin=True)["status"] == "ready"
                        if ready:
                            break
                    except Exception:
                        pass
                    time.sleep(0.1)
                if not ready:
                    raise RuntimeError("Frozen backend failed: " + gateway.log_tail())
                assert LocalGateway(root).port() == port
                assert client.request("GET", "/v1/models")["object"] == "list"
                assert "providers" in client.request("GET", "/admin/config", admin=True)
                gateway.stop()
                assert gateway.owned_process() is None
                print("Frozen local backend, API, reconnect and stop passed")
            finally:
                gateway.stop()


if __name__ == "__main__":
    main()
