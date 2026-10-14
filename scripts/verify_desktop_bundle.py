"""Smoke-test the actual frozen GUI and its headless local backend, no upstream calls."""
import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import threading
import shutil
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
    with tempfile.TemporaryDirectory(dir=executable.anchor) as directory:
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
        # Hard links keep this no-argument smoke test small and on the bundle's drive.
        project = root / "project"
        (project / "app").mkdir(parents=True)
        (project / "app" / "config.py").touch()
        (project / "pyproject.toml").touch()
        (project / "config.yaml").write_text("providers: {}\nmodels: {}\n", encoding="utf-8")
        copied = project / "dist" / "FreelanceDesktop"
        shutil.copytree(executable.parent, copied, copy_function=os.link)
        isolated_env = environment.copy()
        isolated_env["USERPROFILE"] = str(root / "home")
        isolated_env["HOME"] = str(root / "home")
        default_preview = root / "default-preview.png"
        result = subprocess.run([str(copied / executable.name), "--preview", str(default_preview)],
                                cwd=root, env=isolated_env, timeout=30)
        assert result.returncode == 0 and default_preview.exists(), "Default frozen GUI failed"
        preference = root / "home" / ".freelance" / "desktop-workspace.json"
        assert not preference.exists(), "Default GUI unexpectedly remembered a shared path"
        assert (copied / ".env").exists() and (copied / "config.yaml").exists(), "Default files missing beside EXE"
        assert not (project / ".env").exists(), "Default GUI wrote into the parent source project"
        saved = {name: (copied / name).read_bytes() for name in ("config.yaml", ".env")}
        # Configuration travels with the portable folder and must remain unchanged.
        moved = root / "moved" / "FreelanceDesktop"
        moved.parent.mkdir()
        copied.rename(moved)
        result = subprocess.run([str(moved / executable.name), "--preview", str(default_preview)],
                                cwd=root, env=isolated_env, timeout=30)
        assert result.returncode == 0, "Moved GUI failed"
        assert saved == {name: (moved / name).read_bytes() for name in saved}, "Moved GUI changed its configuration"
        assert not (root / "home" / ".freelance" / ".env").exists(), "Moved GUI created an independent workspace"
        assert not preference.exists(), "Moved GUI unexpectedly remembered a shared path"
        print("Frozen EXE-local default creation and moved bundle passed")
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
                configuration = client.request("GET", "/admin/config", admin=True)
                assert "providers" in configuration
                assert configuration["auto_routing"]["custom_ranking"] == []
                saved = client.request("PUT", "/admin/ranking", {
                    "revision": configuration["revision"], "ranking": {"custom_ranking": []}}, admin=True)
                assert saved["auto_routing"]["custom_ranking"] == []
                combined = client.request("PUT", "/admin/providers", {
                    "revision": saved["revision"],
                    "provider": {"name": "demo", "base_url": "https://example.test", "default_model": "demo-model", "api_key": ""},
                    "ranking": {"custom_ranking": [{"provider": "demo", "model": "demo-model"}], "provider_order": ["demo"]}}, admin=True)
                assert combined["providers"]["demo"]["default_model"] == "demo-model"
                assert combined["auto_routing"]["custom_ranking"] == [{"provider": "demo", "model": "demo-model"}]
                assert combined["auto_routing"]["provider_order"] == ["demo"]
                loaded = client.overview(threading.Event(), include_models=True, include_config=True)
                assert loaded["state"] == "ready" and "snapshot" in loaded and "models" in loaded and "config" in loaded
                gateway.stop()
                assert gateway.owned_process() is None
                print("Frozen local backend, ranking API, reconnect and stop passed")
            finally:
                gateway.stop()


if __name__ == "__main__":
    main()
