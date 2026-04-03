"""
Configuration management for Freelance API Gateway.
Loads settings from .env and config.yaml.
"""

import logging
import os
import re
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

# Pre-configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)-8s | %(name)s | %(message)s")
logger = logging.getLogger("freelance.config")

# Load .env from best available location
ENV_PATH = find_config_file(".env")
load_dotenv(dotenv_path=ENV_PATH)
logger.info(f"[*] Environment loaded from: {ENV_PATH.absolute()}")

class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    api_key: str = Field(default="", alias="API_KEY")
    api_keys: str = Field(default="", alias="API_KEYS")  # comma-separated string of valid keys
    max_retries: int = Field(default=3, alias="MAX_RETRIES")

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

    def __init__(self, name: str, base_url: str, provider_type: str = "openai", api_key: str = "", timeout: int = 120, score_offset: float = 0.0):
        self.name = name
        self.provider_type = provider_type
        self.base_url = base_url.rstrip("/")
        self.score_offset = score_offset  # Manual boost/penalty for this provider
        # Parse comma-separated api keys
        self.set_api_key(api_key)
        self.timeout = timeout
        self._current_key_idx = 0

    def set_api_key(self, api_key: str):
        """Parse comma-separated API keys into the pool."""
        self.api_keys = [k.strip() for k in api_key.split(",") if k.strip()]
        if not self.api_keys and api_key:
            self.api_keys = [api_key.strip()]
        self._current_key_idx = 0

    def get_next_key(self) -> str:
        """Returns the next API key in the pool (Round-Robin). Returns empty string if no keys."""
        if not self.api_keys:
            return ""
            
        key = self.api_keys[self._current_key_idx]
        self._current_key_idx = (self._current_key_idx + 1) % len(self.api_keys)
        return key

    def __repr__(self):
        return f"ProviderConfig(name={self.name!r}, type={self.provider_type!r}, base_url={self.base_url!r}, keys={len(self.api_keys)})"


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

            if not raw:
                logger.warning(f"Configuration file at {config_path} is EMPTY or invalid.")
                self._setup_default_providers()
                return

            # Load providers
            provider_count = 0
            providers_data = raw.get("providers", {})
            for name, pconf in providers_data.items():
                base_url = _resolve_env_vars(str(pconf.get("base_url", "")), self.settings)
                api_key = _resolve_env_vars(str(pconf.get("api_key", "")), self.settings)
                provider_type = str(pconf.get("type", "openai")).lower()
                timeout = pconf.get("timeout", 120)
                score_offset = float(pconf.get("score_offset", 0.0))
                
                self.providers[name] = ProviderConfig(
                    name=name, base_url=base_url, provider_type=provider_type, 
                    api_key=api_key, timeout=timeout, score_offset=score_offset
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
