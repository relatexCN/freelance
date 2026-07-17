import asyncio
import os
import time
import logging
from datetime import datetime
from typing import List, Tuple, Optional, Any

from app.config import config
from app.providers import get_provider
from app.services.gateway_errors import GatewayError

logger = logging.getLogger("freelance.router")

ERROR_MODELS_LOG = "error_models.log"

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
    def build_auto_candidates() -> List[Tuple[str, str]]:
        """Build auto-routing candidates, applying explicit ranking when present."""
        default_ranked = []
        providers_with_default = {
            p_name for p_name, p_cfg in config.providers.items()
            if p_cfg.default_model
        }

        seen = set()
        for m in config.auto_models_cache:
            p_name = m.get("_gateway_provider", config.default_provider)
            model_id = m.get("id")
            if not model_id:
                continue
            pair = (p_name, model_id)
            if pair in seen:
                continue
            seen.add(pair)

            p_cfg = config.providers.get(p_name)
            if not p_cfg:
                continue
            if p_name not in providers_with_default or model_id == p_cfg.default_model:
                default_ranked.append(pair)

        # A configured default remains usable when /models omitted it or model
        # discovery failed for that provider.
        for p_name, p_cfg in config.providers.items():
            if p_cfg.default_model and p_cfg.api_keys:
                pair = (p_name, p_cfg.default_model)
                if pair not in default_ranked:
                    logger.info(
                        f"[router] Adding fallback default_model for {p_name}: "
                        f"{p_cfg.default_model}"
                    )
                    default_ranked.append(pair)

        if not config.custom_model_ranking:
            return default_ranked

        # Explicit entries lead.  Keep the normal ranking after them so a
        # partial custom list changes priority without removing safe fallbacks.
        candidates = []
        candidate_seen = set()
        for pair in [*config.custom_model_ranking, *default_ranked]:
            if pair in candidate_seen:
                continue
            p_cfg = config.providers.get(pair[0])
            if not p_cfg or not p_cfg.api_keys:
                continue
            candidates.append(pair)
            candidate_seen.add(pair)
        return candidates

    @staticmethod
    async def get_models_to_try(model_name: str) -> List[Tuple[str, str]]:
        """Resolve a requested model name to a ranked list of (provider, actual_id)."""
        model_name = model_name.lower()
        models_to_try = []

        if model_name in ("auto", "free", "best-free"):
            # Wait for cache if empty
            if not config.auto_models_cache and not config.custom_model_ranking:
                start_wait = time.time()
                while not config.auto_models_cache and (time.time() - start_wait) < 5.0:
                    await asyncio.sleep(1.0)
            if not config.auto_models_cache and not config.custom_model_ranking:
                return []
            models_to_try = RouterService.build_auto_candidates()
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
            
        gw_error = GatewayError.from_exception(error, provider=p_name, model=actual_id)
        err_str = str(gw_error)
        stats_service.record_request(p_name, actual_id, error=err_str)

        if isinstance(error, GatewayError):
            err_type = error.error_type
            c_dur = error.cooldown or config.settings.cooldown.provider_melt
            if error.disable_provider:
                logger.warning(f"Recording provider failure for {p_name}/{actual_id}: {err_str}")
                p_cfg.record_platform_failure(c_dur)
            else:
                logger.warning(f"Recording non-provider failure for {p_name}/{actual_id}: {err_str}")
        else:
            lower_err = err_str.lower()
            if "first chunk" in lower_err or "stream" in lower_err:
                c_dur = config.settings.cooldown.stream_failure_fallback
                err_type = "StreamFailure"
            elif "timeout" in lower_err or "readtimeout" in lower_err:
                c_dur = config.settings.cooldown.timeout_network
                err_type = "Timeout"
            else:
                c_dur = config.settings.cooldown.provider_melt
                err_type = "General"

            logger.warning(f"Recording legacy {err_type} failure for {p_name}/{actual_id}: {err_str}")
            p_cfg.record_platform_failure(c_dur)
        
        RouterService._write_error_log(p_name, actual_id, err_type, err_str, gw_error)

    @staticmethod
    def _write_error_log(p_name: str, actual_id: str, err_type: str, err_str: str, error: Optional[GatewayError] = None):
        today = datetime.now().strftime("%Y-%m-%d")
        time_str = datetime.now().strftime("%H:%M:%S")
        
        header = f"=== {today} ==="
        if error:
            status = error.status_code if error.status_code is not None else "-"
            retryable = "retryable" if error.retryable else "skip-retry"
            actions = []
            if error.disable_key:
                actions.append("key")
            if error.disable_model:
                actions.append("model")
            if error.disable_provider:
                actions.append("provider")
            action_str = ",".join(actions) if actions else "none"
            entry = (
                f"{time_str} | {err_type} | status={status} | {retryable} | "
                f"cooldown={action_str} | {err_str} | {p_name}:{actual_id}"
            )
        else:
            entry = f"{time_str} | {err_type} | {err_str} | {p_name}:{actual_id}"
        
        try:
            need_header = True
            if os.path.exists(ERROR_MODELS_LOG):
                with open(ERROR_MODELS_LOG, "r", encoding="utf-8") as f:
                    content = f.read()
                    if header in content:
                        need_header = False
            
            with open(ERROR_MODELS_LOG, "a", encoding="utf-8") as f:
                if need_header:
                    f.write(f"\n{header}\n")
                f.write(f"{entry}\n")
        except Exception as e:
            logger.error(f"Failed to write error_models.log: {e}")

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
