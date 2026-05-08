import asyncio
import time
import logging
from typing import List, Tuple, Optional, Any

from app.config import config
from app.providers import get_provider

logger = logging.getLogger("freelance.router")

# Cache for provider instances
# Only instantiate providers that have API keys configured
_providers = {name: get_provider(p_cfg) for name, p_cfg in config.providers.items() if p_cfg.api_keys}

def get_provider_instance(name: str, force_create: bool = False):
    """Retrieve or optionally create a provider instance."""
    if name not in _providers and force_create:
        p_cfg = config.providers.get(name)
        if p_cfg:
            _providers[name] = get_provider(p_cfg)
    return _providers.get(name)

class RouterService:
    @staticmethod
    def is_provider_available(p_name: str, actual_id: str) -> bool:
        """Check if a provider/model is available (not melted)."""
        p_cfg = config.providers.get(p_name)
        if not p_cfg:
            return False
        return not (p_cfg.is_platform_melted() or p_cfg.is_model_melted(actual_id))

    @staticmethod
    async def get_models_to_try(model_name: str) -> List[Tuple[str, str]]:
        """Resolve a requested model name to a ranked list of (provider, actual_id)."""
        model_name = model_name.lower()
        models_to_try = []

        if model_name in ("auto", "free", "best-free"):
            # Wait for cache if empty
            if not config.auto_models_cache:
                start_wait = time.time()
                while not config.auto_models_cache and (time.time() - start_wait) < 5.0:
                    await asyncio.sleep(1.0)
            
            if not config.auto_models_cache:
                return []

            # Build a set of providers that have a default_model configured
            providers_with_default = {
                p_name for p_name, p_cfg in config.providers.items()
                if p_cfg.default_model
            }

            # Iterate cache (already ranked by score) and apply filtering rules:
            # - Provider WITHOUT default_model: include all its models
            # - Provider WITH default_model: only include its default_model, skip others
            seen = set()
            for m in config.auto_models_cache:
                p_name = m.get("_gateway_provider", config.default_provider)
                model_id = m["id"]
                pair = (p_name, model_id)

                if pair in seen:
                    continue
                seen.add(pair)

                p_cfg = config.providers.get(p_name)
                if not p_cfg:
                    continue

                if p_name in providers_with_default:
                    # Provider has a default_model: only allow the default_model itself
                    if model_id == p_cfg.default_model:
                        models_to_try.append(pair)
                    # else: skip non-default models from this provider
                else:
                    # Provider has NO default_model: allow all its models (score-ranked)
                    models_to_try.append(pair)

            # Fallback: for providers with default_model whose models are NOT in cache
            # (e.g., list_models failed due to 403), add their default_model as fallback
            # Only for providers that have API keys configured AND failed to return models
            for p_name, p_cfg in config.providers.items():
                if p_name in providers_with_default and p_cfg.default_model and p_cfg.api_keys:
                    # Only fallback if provider failed to return models (not in cache)
                    if p_name not in config.providers_with_models_in_cache:
                        pair = (p_name, p_cfg.default_model)
                        if pair not in seen:
                            logger.info(f"[router] Adding fallback default_model for {p_name}: {p_cfg.default_model}")
                            models_to_try.append(pair)
        else:
            p_cfg, actual = config.resolve_model(model_name)
            if p_cfg:
                models_to_try.append((p_cfg.name, actual))
                
        return models_to_try

    @staticmethod
    def record_failure(p_name: str, actual_id: str, error: Exception):
        from app.services.stats_service import stats_service
        p_cfg = config.providers.get(p_name)
        if not p_cfg:
            return
            
        err_str = str(error)
        stats_service.record_request(p_name, actual_id, error=err_str)
        
        c_dur = config.settings.cooldown.provider_melt
        logger.warning(f"Recording failure for {p_name}/{actual_id}: {err_str}")
        p_cfg.record_platform_failure(c_dur)

    @staticmethod
    def get_provider_status_message(p_name: str, actual_id: str) -> str:
        """Get a human-readable status message for a provider/model."""
        p_cfg = config.providers.get(p_name)
        if not p_cfg:
            return f"Provider '{p_name}' not found"
            
        now = time.time()
        if p_cfg.is_platform_melted():
            rem = int(p_cfg.melted_until - now)
            return f"{p_name} platform melted ({rem}s remaining)"
            
        if p_cfg.is_model_melted(actual_id):
            rem = int(p_cfg.model_cooldowns.get(actual_id, 0) - now)
            return f"{p_name}/{actual_id} model melted ({rem}s remaining)"
            
        # Check if all keys are fatal/cooldown
        num_keys = len(p_cfg.api_keys)
        if num_keys > 0:
            usable_keys = 0
            for idx in range(num_keys):
                if idx not in p_cfg.fatal_keys and (idx not in p_cfg.key_cooldowns or now >= p_cfg.key_cooldowns[idx]):
                    usable_keys += 1
            if usable_keys == 0:
                return f"{p_name} has no usable API keys (all in cooldown or fatal)"
                
        return f"{p_name}/{actual_id} is healthy"
