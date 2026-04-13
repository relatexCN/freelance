import asyncio
import time
import logging
from typing import List, Tuple, Optional, Any

from app.config import config
from app.providers import get_provider

logger = logging.getLogger("freelance.router")

# Cache for provider instances
_providers = {name: get_provider(p_cfg) for name, p_cfg in config.providers.items() if p_cfg.api_keys}

def get_provider_instance(name: str):
    return _providers.get(name)

class RouterService:
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

            # Check provider defaults first
            providers_with_default = [n for n, p in config.providers.items() if p.default_model]
            if providers_with_default:
                for p_name in providers_with_default:
                    models_to_try.append((p_name, config.providers[p_name].default_model))
            else:
                for m in config.auto_models_cache:
                    models_to_try.append((m.get("_gateway_provider", config.default_provider), m["id"]))
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
