"""Local workspace bootstrap and process ownership without CLI PID interference."""
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

import psutil
from dotenv import dotenv_values

from app.admin.store import atomic_write, ConfigStore


def prepare_workspace(directory):
    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    config = root / "config.yaml"
    if not config.exists():
        atomic_write(config, Path(__file__).with_name("default_config.yaml").read_bytes())
    env_path = root / ".env"
    values = dotenv_values(env_path) if env_path.exists() else {}
    # Do not rewrite an existing deployment or silently rotate existing keys.
    additions = []
    for variable in ("API_KEY", "ADMIN_API_KEY"):
        if variable not in values and variable not in os.environ:
            additions.append(f"{variable}=sk-freelance-{secrets.token_urlsafe(32)}")
    if additions:
        content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        atomic_write(env_path, (content.rstrip() + "\n" + "\n".join(additions) + "\n").encode())
    values = dotenv_values(env_path)
    return root, {name: os.environ.get(name, values.get(name, "") or "") for name in ("API_KEY", "ADMIN_API_KEY")}


class LocalGateway:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.state_path = self.root / ".desktop-process.json"
        self.child = None

    def owned_process(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            process = psutil.Process(state["pid"])
            command = process.cmdline()
            if (abs(process.create_time() - state["created"]) < 0.01
                    and Path(process.cwd()).resolve() == self.root
                    and ("app.desktop.backend" in command or ("--backend" in command and Path(process.exe()).resolve() == Path(sys.executable).resolve()))):
                return process
        except (OSError, ValueError, KeyError, psutil.Error):
            pass
        return None

    def port(self):
        if not self.owned_process():
            return None
        try:
            return int(json.loads(self.state_path.read_text())["port"])
        except (OSError, ValueError, KeyError):
            return None

    def runtime_revision(self):
        if not self.owned_process():
            return None
        try:
            return json.loads(self.state_path.read_text())["revision"]
        except (OSError, ValueError, KeyError):
            return None
    def start(self, port):
        existing = self.owned_process()
        if existing:
            return {"port": self.port(), "started": False, "message": "已连接桌面客户端启动的后台网关"}
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise ValueError("端口已被占用；请更换端口或使用远程模式连接已有服务") from None
        environment = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[2])
        environment["PYTHONPATH"] = package_root + os.pathsep + environment.get("PYTHONPATH", "")
        # Avoid inheriting keys loaded by any backend imported into the GUI process.
        command = ([sys.executable, "--backend", "--port", str(port)] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "app.desktop.backend", "--port", str(port)])
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with open(self.root / "desktop-launch.log", "ab") as log:
            self.child = subprocess.Popen(command, cwd=self.root, env=environment,
                                          stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
        try:
            process = psutil.Process(self.child.pid)
            atomic_write(self.state_path, json.dumps({"pid": process.pid, "created": process.create_time(), "port": port,
                                                     "revision": ConfigStore(self.root / "config.yaml").revision()}).encode())
        except (OSError, psutil.Error):
            self.child.terminate()
            self.child.wait(timeout=5)
            raise ValueError("无法保存网关进程状态") from None
        return {"port": port, "started": True, "message": "网关进程已启动，正在等待就绪"}

    def stop(self):
        process = self.owned_process()
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.state_path.unlink(missing_ok=True)
        if self.child:
            self.child.poll()
        return "本地网关已停止"

    def log_tail(self):
        path = self.root / "desktop-launch.log"
        if not path.exists():
            return "尚无启动日志"
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 32000))
            return stream.read().decode("utf-8", errors="replace")
