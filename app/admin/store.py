"""File-backed configuration edits shared by the desktop and admin API.

Never import app.config here: the desktop must not load backend environment state.
"""
import hashlib
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConflictError(ValueError):
    pass


class ProviderEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    base_url: str
    default_model: str = Field(default="", max_length=300)
    timeout: int = Field(default=120, ge=1, le=3600)
    # None means preserve; an empty string explicitly clears the key.
    api_key: str | None = Field(default=None, max_length=16384)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Use an HTTP(S) base URL without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value is not None and any(c in value for c in "\r\n\x00"):
            raise ValueError("API keys must be single-line values")
        return value


class AliasEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(min_length=1, max_length=300)
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=300)

    @field_validator("alias")
    @classmethod
    def not_reserved(cls, value):
        if value in {"auto", "free", "best-free"}:
            raise ValueError("This alias is reserved for automatic routing")
        return value


def atomic_write(path: Path, data: bytes):
    """Replace atomically; POSIX mode is private, Windows inherits directory ACLs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".freelance-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def file_lock(path):
    """Serialize desktop and API writers across processes, without a new dependency."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        deadline = time.monotonic() + 3
        while not locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise ConflictError("Configuration is being saved; reload and retry") from None
                time.sleep(0.05)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class ConfigStore:
    _lock = threading.RLock()

    def __init__(self, config_path, env_path=None, external_env_names=None):
        self.path = Path(config_path).resolve()
        self.env_path = Path(env_path or self.path.with_name(".env")).resolve()
        self.external_env_names = frozenset(os.environ if external_env_names is None else external_env_names)
        self.runtime_revision = self.revision()

    def revision(self):
        digest = hashlib.sha256()
        for path in (self.path, self.env_path):
            data = path.read_bytes() if path.exists() else b""
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        return digest.hexdigest()

    def _raw(self):
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            raise ValueError("Invalid YAML configuration") from None
        if not isinstance(raw, dict) or not isinstance(raw.get("providers", {}), dict) or not isinstance(raw.get("models", {}), dict):
            raise ValueError("Configuration must contain provider and model mappings")
        return raw

    def read(self):
        with self._lock, file_lock(self.path.with_name(".freelance-config.lock")):
            return self._read_locked()

    def provider_key(self, name):
        """Local filesystem access only; this value is never included in admin reads."""
        with self._lock, file_lock(self.path.with_name(".freelance-config.lock")):
            item = self._raw().get("providers", {}).get(name, {})
            if not isinstance(item, dict):
                raise ValueError("Invalid provider configuration")
            source = str(item.get("api_key", ""))
            match = re.fullmatch(r"\$\{(\w+):?([^}]*)\}", source)
            if not match:
                return source
            if match[1] in self.external_env_names:
                return os.environ.get(match[1], "")
            env = dotenv_values(self.env_path) if self.env_path.exists() else {}
            return env.get(match[1], match[2]) or ""

    def regenerate_gateway_key(self, revision):
        with self._lock, file_lock(self.path.with_name(".freelance-config.lock")):
            if "API_KEY" in self.external_env_names:
                raise ValueError("调用 Key 来自进程环境变量，请在运行环境中修改")
            if revision != self.revision():
                raise ConflictError("配置已变化，请重新加载配置后再生成 Key")
            self._raw()  # Do not rotate credentials if configuration is malformed.
            old_env = self.env_path.read_bytes() if self.env_path.exists() else None
            content = (old_env or b"").decode("utf-8")
            new_key = "sk-freelance-" + secrets.token_urlsafe(32)
            replacement = "API_KEY=" + new_key
            pattern = r"^(?:export\s+)?API_KEY\s*=.*$"
            content = re.sub(pattern, lambda _: replacement, content, flags=re.MULTILINE) if re.search(pattern, content, re.MULTILINE) else content.rstrip() + "\n" + replacement + "\n"
            if old_env is not None:
                atomic_write(self.env_path.with_name(".env.bak"), old_env)
            try:
                atomic_write(self.env_path, content.encode("utf-8"))
                configuration = self._read_locked()
            except (OSError, ValueError):
                if old_env is not None:
                    atomic_write(self.env_path, old_env)
                else:
                    self.env_path.unlink(missing_ok=True)
                raise
            return {"api_key": new_key, "configuration": configuration}

    def _read_locked(self):
        with self._lock:
            raw = self._raw()
            env = dotenv_values(self.env_path) if self.env_path.exists() else {}
            providers = {}
            for name, item in raw.get("providers", {}).items():
                if not isinstance(item, dict):
                    raise ValueError("Invalid provider configuration")
                source = str(item.get("api_key", ""))
                match = re.fullmatch(r"\$\{(\w+):?([^}]*)\}", source)
                key = (os.environ.get(match[1], "") if match[1] in self.external_env_names else env.get(match[1], match[2]) or "") if match else source
                # Whitelist: never expose headers, body overrides or literal keys.
                url = str(item.get("base_url", ""))
                parsed = urlsplit(url)
                if parsed.username or parsed.password or parsed.query:
                    url = ""
                providers[name] = {"base_url": url, "default_model": item.get("default_model", ""),
                                   "timeout": item.get("timeout", 120), "key_configured": bool(key),
                                   "key_count": len([k for k in key.split(",") if k.strip()]),
                                   "key_environment_override": bool(match and match[1] in self.external_env_names)}
            aliases = {name: {"provider": value.get("provider", ""), "model": value.get("model", "")}
                       for name, value in raw.get("models", {}).items() if isinstance(value, dict)}
            revision = self.revision()
            return {"revision": revision, "pending_restart": revision != self.runtime_revision,
                    "providers": providers, "models": aliases}

    def _save(self, revision, change, env_change=None):
        with self._lock, file_lock(self.path.with_name(".freelance-config.lock")):
            return self._save_locked(revision, change, env_change)

    def _save_locked(self, revision, change, env_change=None):
        with self._lock:
            if revision != self.revision():
                raise ConflictError("Configuration changed; reload before saving")
            raw = self._raw()
            change(raw)
            config_data = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8")
            old_env = self.env_path.read_bytes() if self.env_path.exists() else None
            if env_change:
                variable, value = env_change
                content = (old_env or b"").decode("utf-8")
                # Quoted dotenv values preserve $, #, spaces and single quotes.
                quoted = value.replace("\\", "\\\\").replace("'", "\\'")
                line = f"{variable}='{quoted}'"
                pattern = rf"^(?:export\s+)?{re.escape(variable)}\s*=.*$"
                content = re.sub(pattern, lambda _: line, content, flags=re.MULTILINE) if re.search(pattern, content, re.MULTILINE) else content.rstrip() + "\n" + line + "\n"
            # Backups can contain secrets; create with the same private writer.
            atomic_write(self.path.with_name(self.path.name + ".bak"), self.path.read_bytes())
            if env_change and old_env is not None:
                atomic_write(self.env_path.with_name(".env.bak"), old_env)
            try:
                if env_change:
                    atomic_write(self.env_path, content.encode("utf-8"))
                atomic_write(self.path, config_data)
            except OSError:
                if env_change:
                    if old_env is None:
                        self.env_path.unlink(missing_ok=True)
                    else:
                        atomic_write(self.env_path, old_env)
                raise
            return self._read_locked()

    def provider(self, revision, edit: ProviderEdit):
        env_change = None
        with self._lock:
            current = self._raw().get("providers", {}).get(edit.name, {})
            if not isinstance(current, dict):
                raise ValueError("Invalid existing provider")
            if edit.api_key is not None:
                source = str(current.get("api_key", ""))
                match = re.fullmatch(r"\$\{(\w+):?([^}]*)\}", source)
                # Avoid changing a shared environment variable used by another provider.
                variable = "FREELANCE_PROVIDER_" + edit.name.upper().replace("-", "_") + "_" + hashlib.sha256(edit.name.encode()).hexdigest()[:8].upper() + "_KEY"
                if (match and match[1] in self.external_env_names) or variable in self.external_env_names:
                    raise ValueError("Key is overridden by the process environment; edit it at deployment")
                env_change = (variable, edit.api_key)

            def change(raw):
                item = raw.setdefault("providers", {}).setdefault(edit.name, {"type": "openai"})
                item.update(base_url=edit.base_url, default_model=edit.default_model, timeout=edit.timeout)
                if env_change:
                    item["api_key"] = "${" + env_change[0] + ":}"
            return self._save(revision, change, env_change)

    def alias(self, revision, edit: AliasEdit):
        def change(raw):
            if edit.provider not in raw.get("providers", {}):
                raise ValueError("Unknown provider")
            raw.setdefault("models", {})[edit.alias] = {"provider": edit.provider, "model": edit.model}
        return self._save(revision, change)

    def delete_alias(self, revision, alias):
        def change(raw):
            if alias not in raw.get("models", {}):
                raise ValueError("Unknown alias")
            del raw["models"][alias]
        return self._save(revision, change)
