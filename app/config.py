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
from app.services.gateway_errors import matches_status, parse_status_code_ranges

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

class ErrorPolicySettings(BaseModel):
    """Status-code based retry and cooldown policy."""
    retry_status_codes: str = "401-407,409-499,500-503,505-599"
    skip_retry_status_codes: str = "400,408,422,504,524"
    disable_key_status_codes: str = "401,403"
    disable_model_status_codes: str = "404"
    disable_provider_status_codes: str = ""

    def should_retry(self, status_code: Optional[int]) -> bool:
        if matches_status(status_code, parse_status_code_ranges(self.skip_retry_status_codes)):
            return False
        return matches_status(status_code, parse_status_code_ranges(self.retry_status_codes))

    def should_disable_key(self, status_code: Optional[int]) -> bool:
        return matches_status(status_code, parse_status_code_ranges(self.disable_key_status_codes))

    def should_disable_model(self, status_code: Optional[int]) -> bool:
        return matches_status(status_code, parse_status_code_ranges(self.disable_model_status_codes))

    def should_disable_provider(self, status_code: Optional[int]) -> bool:
        return matches_status(status_code, parse_status_code_ranges(self.disable_provider_status_codes))

class ToolSettings(BaseModel):
    """Tool-call handling mode."""
    # passthrough: forward tool definitions/calls without local execution.
    # local_agent: execute supported tool calls inside this gateway.
    # disabled: remove tool request controls before sending upstream.
    mode: str = "passthrough"

    def normalized_mode(self) -> str:
        mode = (self.mode or "passthrough").strip().lower()
        return mode if mode in {"passthrough", "local_agent", "disabled"} else "passthrough"


class ScoringSettings(BaseModel):
    """Weights for metadata-based model ranking."""
    capabilities_weight: float = 0.40
    context_weight: float = 0.20
    recency_weight: float = 0.15
    model_tier_weight: float = 0.15
    stability_weight: float = 0.10
    recency_half_life_days: int = 730
    max_provider_adjustment: float = 0.03

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    api_key: str = Field(default="", alias="API_KEY")
    api_keys: str = Field(default="", alias="API_KEYS")  # comma-separated string of valid keys
    max_retries: int = Field(default=3, alias="MAX_RETRIES")
    max_iterations: int = Field(default=3, alias="MAX_ITERATIONS")
    multi_model_max_models: int = Field(default=5, alias="MULTI_MODEL_MAX_MODELS")
    multi_model_timeout: float = Field(default=90, alias="MULTI_MODEL_TIMEOUT")
    unified_timeout: Optional[float] = Field(default=None, alias="UNIFIED_TIMEOUT")
    switch_session_ttl_seconds: int = Field(default=86400, alias="SWITCH_SESSION_TTL_SECONDS")
    cooldown: CooldownSettings = Field(default_factory=CooldownSettings)
    stream_probe: StreamProbeSettings = Field(default_factory=StreamProbeSettings)
    connection_pool: PoolSettings = Field(default_factory=PoolSettings)
    error_policy: ErrorPolicySettings = Field(default_factory=ErrorPolicySettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)

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

    def __init__(
        self,
        name: str,
        base_url: str,
        provider_type: str = "openai",
        api_key: str = "",
        timeout: int = 120,
        score_offset: float = 0.0,
        default_model: str = "",
        pass_through_body: bool = False,
        header_overrides: Optional[dict] = None,
        header_passthrough: Optional[list] = None,
        param_overrides: Optional[list] = None,
        disabled_fields: Optional[list] = None,
    ):
        self.name = name
        self.provider_type = provider_type
        self.base_url = base_url.rstrip("/")
        self.score_offset = score_offset  # Manual boost/penalty for this provider
        self.timeout = timeout
        self.default_model = default_model  # Default model to use in auto mode
        self.pass_through_body = pass_through_body
        self.header_overrides = header_overrides or {}
        self.header_passthrough = header_passthrough or []
        self.param_overrides = param_overrides or []
        self.disabled_fields = disabled_fields or []
        
        # Internal API key pool and round-robin counter
        self.api_keys = []
        self._current_key_idx = 0
        self.key_cooldowns: dict[int, float] = {}   # index -> melted_until timestamp
        self.fatal_keys: set[int] = set()           # set of key indices that are fatally failed
        self.model_cooldowns: dict[str, float] = {} # model_id -> melted_until timestamp
        self.stream_melted_until: dict[str, float] = {} # model_id -> melted_until timestamp
        self.melted_until = 0 # Platform-level circuit breaker
        self.consecutive_failures: int = 0      # consecutive failure counter
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
        """Mark the entire provider as melted for a period of time.
        Only melts after consecutive failures exceed max_retries from config.yaml."""
        threshold = config.settings.max_retries
        self.consecutive_failures += 1
        if self.consecutive_failures >= threshold:
            self.melted_until = time.time() + cooldown
            logger.warning(f"[{self.name}] Platform melted after {self.consecutive_failures} consecutive failures ({cooldown}s)")
        else:
            logger.info(f"[{self.name}] Failure {self.consecutive_failures}/{threshold}, not melting yet")

    def reset_consecutive_failures(self):
        """Reset the consecutive failure counter after a successful request."""
        if self.consecutive_failures > 0:
            logger.info(f"[{self.name}] Resetting consecutive failure counter (was {self.consecutive_failures})")
            self.consecutive_failures = 0

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
        self.custom_model_ranking: list[tuple[str, str]] = []
        self.auto_models_cache: list[dict] = []  # Ranked list of available models
        self.auto_models_last_update: float = 0.0
        self.providers_with_models_in_cache: set = set()  # Providers that successfully returned models
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
                if "multi_model_max_models" in s_data:
                    max_models = int(s_data["multi_model_max_models"])
                    if max_models <= 0:
                        raise ValueError("settings.multi_model_max_models must be positive")
                    self.settings.multi_model_max_models = max_models
                if "multi_model_timeout" in s_data:
                    multi_timeout = float(s_data["multi_model_timeout"])
                    if multi_timeout <= 0:
                        raise ValueError("settings.multi_model_timeout must be positive")
                    self.settings.multi_model_timeout = multi_timeout
                if "unified_timeout" in s_data:
                    unified_timeout_val = s_data["unified_timeout"]
                    if unified_timeout_val is not None:
                        self.settings.unified_timeout = float(unified_timeout_val)
                    else:
                        self.settings.unified_timeout = None
                if "switch_session_ttl_seconds" in s_data:
                    switch_ttl = int(s_data["switch_session_ttl_seconds"])
                    if switch_ttl <= 0:
                        raise ValueError("settings.switch_session_ttl_seconds must be positive")
                    self.settings.switch_session_ttl_seconds = switch_ttl
                
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

                if "error_policy" in s_data:
                    ep_data = s_data["error_policy"]
                    for key, val in ep_data.items():
                        if hasattr(self.settings.error_policy, key):
                            # Validate early so invalid rules fail at startup, not mid-request.
                            parse_status_code_ranges(val)
                            if isinstance(val, list):
                                val = ",".join(str(item) for item in val)
                            setattr(self.settings.error_policy, key, str(val))

                if "tools" in s_data:
                    tools_data = s_data["tools"] or {}
                    if "mode" in tools_data:
                        mode = str(tools_data["mode"]).strip().lower()
                        if mode not in {"passthrough", "local_agent", "disabled"}:
                            raise ValueError(f"Invalid tools.mode: {mode}")
                        self.settings.tools.mode = mode

                if "scoring" in s_data:
                    scoring_data = s_data["scoring"] or {}
                    for key, val in scoring_data.items():
                        if not hasattr(self.settings.scoring, key):
                            continue
                        if key == "recency_half_life_days":
                            parsed = int(val)
                            if parsed <= 0:
                                raise ValueError("scoring.recency_half_life_days must be positive")
                        else:
                            parsed = float(val)
                            if parsed < 0:
                                raise ValueError(f"scoring.{key} cannot be negative")
                        setattr(self.settings.scoring, key, parsed)
                    scoring_weights = (
                        self.settings.scoring.capabilities_weight,
                        self.settings.scoring.context_weight,
                        self.settings.scoring.recency_weight,
                        self.settings.scoring.model_tier_weight,
                        self.settings.scoring.stability_weight,
                    )
                    if sum(scoring_weights) <= 0:
                        raise ValueError("At least one settings.scoring weight must be positive")

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
                pass_through_body = bool(pconf.get("pass_through_body", False))
                header_overrides = pconf.get("header_overrides", {}) or {}
                header_passthrough = pconf.get("header_passthrough", []) or []
                param_overrides = pconf.get("param_overrides", []) or []
                disabled_fields = pconf.get("disabled_fields", []) or []
                
                self.providers[name] = ProviderConfig(
                    name=name, base_url=base_url, provider_type=provider_type, 
                    api_key=api_key, timeout=timeout, score_offset=score_offset,
                    default_model=default_model,
                    pass_through_body=pass_through_body,
                    header_overrides=header_overrides,
                    header_passthrough=header_passthrough,
                    param_overrides=param_overrides,
                    disabled_fields=disabled_fields,
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

            # Optional explicit priority for auto/free/best-free routing.  Dict
            # entries are unambiguous and are the documented form; aliases are
            # also accepted as a convenience when they exist in `models`.
            auto_routing = raw.get("auto_routing", {}) or {}
            ranking_data = auto_routing.get("custom_ranking", []) or []
            if not isinstance(ranking_data, list):
                raise ValueError("auto_routing.custom_ranking must be a list")

            ranking_seen = set()
            for index, entry in enumerate(ranking_data):
                provider_name = ""
                model_id = ""
                if isinstance(entry, dict):
                    provider_name = str(entry.get("provider", "")).strip()
                    model_id = str(entry.get("model", "")).strip()
                elif isinstance(entry, str):
                    route = self.model_routes.get(entry.strip())
                    if route:
                        provider_name, model_id = route.provider, route.model
                    elif ":" in entry:
                        provider_name, model_id = (part.strip() for part in entry.split(":", 1))

                if not provider_name or not model_id:
                    raise ValueError(
                        f"Invalid auto_routing.custom_ranking entry at index {index}: "
                        "expected {provider: ..., model: ...}, a configured model alias, "
                        "or 'provider:model'"
                    )
                if provider_name not in self.providers:
                    raise ValueError(
                        f"Unknown provider '{provider_name}' in "
                        f"auto_routing.custom_ranking entry {index}"
                    )

                pair = (provider_name, model_id)
                if pair not in ranking_seen:
                    self.custom_model_ranking.append(pair)
                    ranking_seen.add(pair)
            
            self.default_provider = raw.get("default_provider", "ollama")
            logger.info(f"[*] Configuration loaded from: {config_path.absolute()}")
            logger.info(f"Loaded {provider_count} providers and {route_count} routes")
            if self.custom_model_ranking:
                logger.info(
                    "Loaded custom auto-routing ranking with "
                    f"{len(self.custom_model_ranking)} model(s)"
                )

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
