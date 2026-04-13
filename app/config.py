"""
Configuration management for Freelance API Gateway.
Loads settings from .env and config.yaml.
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import Field, BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Advanced Path Resolution ---
# 1. First, check the Current Working Directory (where the user is running the command)
# 2. Second, fallback to the directory containing the 'app' folder
CWD_DIR = Path.cwd()
PKG_DIR = Path(__file__).parent.parent

def find_config_file(filename: str) -> Path:
    """Check CWD first, then package root."""
    cwd_path = CWD_DIR / filename
    if cwd_path.exists():
        return cwd_path
    
    pkg_path = PKG_DIR / filename
    if pkg_path.exists():
        return pkg_path
        
    return cwd_path  # Default to CWD path if none found (for error reporting)

# Pre-configure logging (Logic moved to main.py)
logger = logging.getLogger("freelance.config")

# Load .env from best available location
ENV_PATH = find_config_file(".env")
load_dotenv(dotenv_path=ENV_PATH)
logger.info(f"[*] Environment loaded from: {ENV_PATH.absolute()}")

class CooldownSettings(BaseModel):
    """Cooldown durations for different error types."""
    rate_limit_429: int = 1800
    auth_error_401: int = 10800
    forbidden_403: int = 10800
    quota_exceeded: int = 3600
    timeout_network: int = 3600
    stream_failure_fallback: int = 3600
    other_error: int = 10800
    provider_melt: int = 10800

class StreamProbeSettings(BaseModel):
    """Tiered first-token timeout settings for SSE."""
    small_model_timeout: int = 15       # For small models (<10B)
    large_model_timeout: int = 30       # For large models (10B-100B)
    xlarge_model_timeout: int = 60      # For extra-large models (100B-500B)
    defunct_model_timeout: int = 120    # For unrecognized models
    ping_interval: int = 3              # Heartbeat interval (seconds)
    read_timeout: int = 120             # Read timeout between chunks (seconds)
    huge_model_read_timeout: int = 180  # Read timeout for huge models (500B+)

class ConnectionPoolConfig(BaseModel):
    """Configuration for a single HTTP connection pool."""
    max_connections: int = 100
    max_keepalive: int = 50
    keepalive_expiry: int = 300

class PoolSettings(BaseModel):
    """Settings for isolated connection pools."""
    stream: ConnectionPoolConfig = Field(default_factory=lambda: ConnectionPoolConfig(max_connections=100, max_keepalive=50, keepalive_expiry=600))
    batch: ConnectionPoolConfig = Field(default_factory=lambda: ConnectionPoolConfig(max_connections=200, max_keepalive=100, keepalive_expiry=60))

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    api_key: str = Field(default="", alias="API_KEY")
    api_keys: str = Field(default="", alias="API_KEYS")  # comma-separated string of valid keys
    max_retries: int = Field(default=3, alias="MAX_RETRIES")
    max_iterations: int = Field(default=3, alias="MAX_ITERATIONS")
    unified_timeout: Optional[float] = Field(default=None, alias="UNIFIED_TIMEOUT")
    cooldown: CooldownSettings = Field(default_factory=CooldownSettings)
    stream_probe: StreamProbeSettings = Field(default_factory=StreamProbeSettings)
    connection_pool: PoolSettings = Field(default_factory=PoolSettings)

    # Legacy fields removed. All provider config now lives in config.yaml
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


def _resolve_env_vars(value: str, settings: Settings) -> str:
    """Resolve ${VAR:default} patterns using Pydantic Settings values or environment."""
    pattern = r"\$\{(\w+):?([^}]*)\}"

    def replacer(match):
        var_name = match.group(1)
        default_val = match.group(2)
        return os.getenv(var_name, default_val)

    return re.sub(pattern, replacer, value)


class ProviderConfig:
    """Configuration for a single provider."""

    def __init__(self, name: str, base_url: str, provider_type: str = "openai", api_key: str = "", timeout: int = 120, score_offset: float = 0.0, default_model: str = ""):
        self.name = name
        self.provider_type = provider_type
        self.base_url = base_url.rstrip("/")
        self.score_offset = score_offset  # Manual boost/penalty for this provider
        self.timeout = timeout
        self.default_model = default_model  # Default model to use in auto mode
        
        # Internal API key pool and round-robin counter
        self.api_keys = []
        self._current_key_idx = 0
        self.key_cooldowns: dict[int, float] = {}   # index -> melted_until timestamp
        self.fatal_keys: set[int] = set()           # set of key indices that are fatally failed
        self.model_cooldowns: dict[str, float] = {} # model_id -> melted_until timestamp
        self.stream_melted_until: dict[str, float] = {} # model_id -> melted_until timestamp
        self.melted_until = 0 # Platform-level circuit breaker
        self.set_api_key(api_key)

    def set_api_key(self, api_key: str):
        """Parse comma-separated API keys into the pool with strict filtering."""
        if not api_key:
            self.api_keys = []
        else:
            # Split and filter out empty strings after stripping (e.g., "key1, ,key2")
            self.api_keys = [k.strip() for k in api_key.split(",") if k.strip()]
        
        self._current_key_idx = 0
        self.key_cooldowns = {}
        self.fatal_keys = set()
        self.model_cooldowns = {}
        self.stream_melted_until = {}
        self.melted_until = 0

    def get_next_key(self) -> tuple[str, int]:
        """
        Returns (key, index) for the next healthy API key in the pool.
        Skips keys currently in cooldown. 
        UNLESS the key is marked as Fatal (401/403), it won't even be returned as fallback.
        """
        if not self.api_keys:
            logger.info(f"[{self.name}] get_next_key: No API keys configured")
            return "", -1
            
        now = time.time()
        num_keys = len(self.api_keys)
        
        # Build status info for logging
        status_parts = []
        for idx in range(num_keys):
            if idx in self.fatal_keys:
                status_parts.append(f"key[{idx}]=FATAL")
            elif idx in self.key_cooldowns:
                remaining = self.key_cooldowns[idx] - now
                if remaining > 0:
                    status_parts.append(f"key[{idx}]=COOLDOWN({remaining:.0f}s)")
                else:
                    status_parts.append(f"key[{idx}]=HEALTHY")
            else:
                status_parts.append(f"key[{idx}]=HEALTHY")
        
        # 1. First pass: try to find a completely healthy key
        for i in range(num_keys):
            idx = (self._current_key_idx + i) % num_keys
            if idx not in self.key_cooldowns or now >= self.key_cooldowns[idx]:
                # Found a healthy key!
                if idx in self.key_cooldowns:
                    del self.key_cooldowns[idx] # Clean up
                    if idx in self.fatal_keys:
                        self.fatal_keys.remove(idx)
                
                self._current_key_idx = (idx + 1) % num_keys
                logger.info(f"[{self.name}] get_next_key: Selected key[{idx}] (HEALTHY) | Status: {', '.join(status_parts)}")
                return self.api_keys[idx], idx

        # 2. Fallback: return the next one that is NOT fatal.
        # This is for transient errors like 429.
        for i in range(num_keys):
            idx = (self._current_key_idx + i) % num_keys
            if idx not in self.fatal_keys:
                self._current_key_idx = (idx + 1) % num_keys
                remaining = self.key_cooldowns[idx] - now if idx in self.key_cooldowns else 0
                logger.info(f"[{self.name}] get_next_key: Selected key[{idx}] (IN_COOLDOWN, {remaining:.0f}s remaining) | Status: {', '.join(status_parts)}")
                return self.api_keys[idx], idx
        
        # 3. No non-fatal keys available. Trigger platform-level melt if not already set.
        if not self.is_platform_melted():
             # Use provider_melt duration (default 3h)
             self.record_platform_failure(10800)
             logger.warning(f"[{self.name}] No non-fatal keys available. Melting platform for 3 hours.")

        logger.error(f"[{self.name}] get_next_key: No available keys | Status: {', '.join(status_parts)}")
        return "", -1

    def record_key_failure(self, index: int, cooldown: int = 60, is_fatal: bool = False):
        """Mark a specific key as hot for a period of time."""
        if 0 <= index < len(self.api_keys):
            self.key_cooldowns[index] = time.time() + cooldown
            if is_fatal:
                self.fatal_keys.add(index)
                # If ALL keys are now fatal, melt the platform
                if len(self.fatal_keys) >= len(self.api_keys):
                    self.record_platform_failure() # 3 hour default
                    logger.error(f"[{self.name}] All keys are fatal. Melting entire platform.")
            elif index in self.fatal_keys:
                self.fatal_keys.remove(index)

    def record_model_failure(self, model_id: str, cooldown: int = 3600):
        """Mark a specific model path as hot for a period of time."""
        self.model_cooldowns[model_id] = time.time() + cooldown

    def record_platform_failure(self, cooldown: int = 10800):
        """Mark the entire provider as melted for a period of time."""
        self.melted_until = time.time() + cooldown

    def is_model_melted(self, model_id: str) -> bool:
        """Check if a specific model is cooling down."""
        if model_id in self.model_cooldowns:
            if time.time() < self.model_cooldowns[model_id]:
                return True
            del self.model_cooldowns[model_id]
        return False

    def is_platform_melted(self) -> bool:
        """Check if the entire provider is cooling down."""
        if self.melted_until > 0:
            if time.time() < self.melted_until:
                return True
            self.melted_until = 0
        return False

    def record_stream_failure(self, model_id: str, cooldown: float):
        """Record a failure specifically for streaming protocol."""
        self.stream_melted_until[model_id] = time.time() + cooldown

    def is_stream_melted(self, model_id: str) -> bool:
        """Check if streaming is currently disabled for this model."""
        if model_id in self.stream_melted_until:
            if time.time() < self.stream_melted_until[model_id]:
                return True
            del self.stream_melted_until[model_id]
        return False

    def __repr__(self):
        status = "HEALTHY"
        if self.is_platform_melted(): status = "MELTED"
        return f"ProviderConfig(name={self.name!r}, status={status}, keys={len(self.api_keys)}, hot_keys={len(self.key_cooldowns)}, hot_models={len(self.model_cooldowns)})"


class ModelRoute:
    """Routing information for a model."""

    def __init__(self, alias: str, provider: str, model: str):
        self.alias = alias
        self.provider = provider
        self.model = model


class AppConfig:
    """Full application configuration combining env settings and YAML config."""

    def __init__(self):
        self.settings = Settings()
        self.providers: dict[str, ProviderConfig] = {}
        self.model_routes: dict[str, ModelRoute] = {}
        self.default_provider: str = "ollama"
        self.auto_models_cache: list[dict] = []  # Ranked list of available models
        self.auto_models_last_update: float = 0.0
        self._load_yaml_config()

    def _load_yaml_config(self):
        """Loads routing and provider settings from config.yaml."""
        # Check CWD first (important for installed packages), then package root
        config_path = find_config_file("config.yaml")
        
        if not config_path.exists():
            logger.error(f"FATAL: Configuration file NOT FOUND at any expected location: {config_path}")
            self._setup_default_providers()
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                content = f.read()
                raw = yaml.safe_load(content) or {}

            self.settings = Settings()
            if "settings" in raw:
                s_data = raw["settings"]
                if "max_retries" in s_data:
                    self.settings.max_retries = int(s_data["max_retries"])
                if "max_iterations" in s_data:
                    self.settings.max_iterations = int(s_data["max_iterations"])
                if "unified_timeout" in s_data:
                    unified_timeout_val = s_data["unified_timeout"]
                    if unified_timeout_val is not None:
                        self.settings.unified_timeout = float(unified_timeout_val)
                    else:
                        self.settings.unified_timeout = None
                
                if "cooldown" in s_data:
                    c_data = s_data["cooldown"]
                    for key, val in c_data.items():
                        if hasattr(self.settings.cooldown, key):
                            setattr(self.settings.cooldown, key, int(val))
                
                if "stream_probe" in s_data:
                    p_data = s_data["stream_probe"]
                    for key, val in p_data.items():
                        if hasattr(self.settings.stream_probe, key):
                            setattr(self.settings.stream_probe, key, int(val))
                
                if "connection_pool" in s_data:
                    cp_data = s_data["connection_pool"]
                    # Load stream pool
                    if "stream" in cp_data:
                        for key, val in cp_data["stream"].items():
                            if hasattr(self.settings.connection_pool.stream, key):
                                setattr(self.settings.connection_pool.stream, key, int(val))
                    # Load batch pool
                    if "batch" in cp_data:
                        for key, val in cp_data["batch"].items():
                            if hasattr(self.settings.connection_pool.batch, key):
                                setattr(self.settings.connection_pool.batch, key, int(val))

            # Load providers
            provider_count = 0
            providers_data = raw.get("providers", {})
            for name, pconf in providers_data.items():
                base_url = _resolve_env_vars(str(pconf.get("base_url", "")), self.settings)
                api_key = _resolve_env_vars(str(pconf.get("api_key", "")), self.settings)
                provider_type = str(pconf.get("type", "openai")).lower()
                timeout = pconf.get("timeout", 120)
                score_offset = float(pconf.get("score_offset", 0.0))
                default_model = pconf.get("default_model", "")
                
                self.providers[name] = ProviderConfig(
                    name=name, base_url=base_url, provider_type=provider_type, 
                    api_key=api_key, timeout=timeout, score_offset=score_offset,
                    default_model=default_model
                )
                provider_count += 1
            
            # Load model routes
            route_count = 0
            models_data = raw.get("models", {})
            for alias, mconf in models_data.items():
                if isinstance(mconf, dict) and "provider" in mconf and "model" in mconf:
                    self.model_routes[alias] = ModelRoute(
                        alias=alias,
                        provider=mconf["provider"],
                        model=mconf["model"],
                    )
                    route_count += 1
            
            self.default_provider = raw.get("default_provider", "ollama")
            logger.info(f"[*] Configuration loaded from: {config_path.absolute()}")
            logger.info(f"Loaded {provider_count} providers and {route_count} routes")

        except Exception as e:
            logger.error(f"Critical error parsing {config_path}: {e}")
            self._setup_default_providers()

    def _setup_default_providers(self):
        """Setup minimal default provider configurations if config.yaml is missing."""
        self.providers["ollama"] = ProviderConfig(
            name="ollama",
            provider_type="openai",
            base_url="https://ollama.com/v1",
        )

    def resolve_model(self, model_name: str) -> tuple[ProviderConfig, str]:
        """
        Resolve a model name to its provider and actual model identifier.
        Returns (provider_config, actual_model_name).
        """
        if model_name in self.model_routes:
            route = self.model_routes[model_name]
            provider = self.providers.get(route.provider)
            if provider:
                return provider, route.model

        # Smart fallback for IDs with slashes (e.g., google/gemini-3.1)
        if "/" in model_name and "openrouter" in self.providers:
            return self.providers["openrouter"], model_name

        # Standard fallback: use default provider
        default = self.providers.get(self.default_provider)
        if not default and self.providers:
            # Last resort: first available provider in the registry
            default = next(iter(self.providers.values()))
        
        if not default:
            # Critical fallback: Create a dummy one or handle in router
            return None, model_name
            
        return default, model_name


# Singleton
config = AppConfig()
