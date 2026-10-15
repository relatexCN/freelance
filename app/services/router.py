import asyncio
import os
import threading
import time
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple, Optional, Any

from app.config import config
from app.providers import get_provider
from app.services.gateway_errors import GatewayError

logger = logging.getLogger("freelance.router")
AUTO_ROUTING_ALIASES = ("auto", "free", "best-free")

ERROR_MODELS_LOG = "error_models.log"

# Cache for provider instances
# Only instantiate providers that have API keys configured
_providers = {name: get_provider(p_cfg) for name, p_cfg in config.providers.items() if p_cfg.api_keys}


@dataclass
class SessionSwitchState:
    provider: str
    model: str
    explicit: bool
    expires_at: float

def get_provider_instance(name: str, force_create: bool = False):
    """Retrieve or optionally create a provider instance."""
    if name not in _providers and force_create:
        p_cfg = config.providers.get(name)
        if p_cfg:
            _providers[name] = get_provider(p_cfg)
    return _providers.get(name)

class RouterService:
    _SWITCH_PREFIX = "/switch"
    _SWITCH_AUTO = "__auto__"
    _session_switches: dict[str, SessionSwitchState] = {}
    _session_switch_lock = threading.Lock()

    @staticmethod
    def resolve_session_id(request: Any) -> Optional[str]:
        """Resolve an explicit, stable session identifier without unsafe IP fallbacks."""
        headers = getattr(request, "_gateway_headers", {}) or {}
        normalized_headers = {str(k).lower(): str(v).strip() for k, v in headers.items()}
        for header_name in (
            "x-freelance-session-id",
            "x-openclaw-session-id",
            "x-conversation-id",
            "x-session-id",
            "x-chat-id",
        ):
            value = normalized_headers.get(header_name, "")
            if value and len(value) <= 512:
                return f"header:{header_name}:{value}"

        user = str(getattr(request, "user", "") or "").strip()
        if user and len(user) <= 512:
            return f"user:{user}"
        return None

    @staticmethod
    def _purge_expired_switches(now: Optional[float] = None):
        current_time = time.time() if now is None else now
        expired = [
            session_id
            for session_id, state in RouterService._session_switches.items()
            if state.expires_at <= current_time
        ]
        for session_id in expired:
            RouterService._session_switches.pop(session_id, None)

    @staticmethod
    def _get_session_switch(session_id: str) -> Optional[SessionSwitchState]:
        with RouterService._session_switch_lock:
            RouterService._purge_expired_switches()
            state = RouterService._session_switches.get(session_id)
            if state is None:
                return None
            state.expires_at = time.time() + config.settings.switch_session_ttl_seconds
            return state

    @staticmethod
    def _set_session_switch(
        session_id: str, target: Tuple[str, str], *, explicit: bool
    ) -> SessionSwitchState:
        state = SessionSwitchState(
            provider=target[0],
            model=target[1],
            explicit=explicit,
            expires_at=time.time() + config.settings.switch_session_ttl_seconds,
        )
        with RouterService._session_switch_lock:
            RouterService._purge_expired_switches()
            RouterService._session_switches[session_id] = state
        return state

    @staticmethod
    def _clear_session_switch(session_id: str):
        with RouterService._session_switch_lock:
            RouterService._session_switches.pop(session_id, None)

    @staticmethod
    def clear_session_switches():
        """Clear all in-memory switch state; primarily useful for controlled resets/tests."""
        with RouterService._session_switch_lock:
            RouterService._session_switches.clear()

    @staticmethod
    def _message_text(message: Any) -> str:
        """Return plain text only when a message has an unambiguous text body."""
        content = getattr(message, "content", None)
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list) and content:
            text_parts = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in ("text", "input_text"):
                    return ""
                value = part.get("text")
                if not isinstance(value, str):
                    return ""
                text_parts.append(value)
            text = "\n".join(text_parts).strip()
        else:
            return ""

        # OpenClaw may prepend its conversation metadata to the actual user
        # text.  Only inspect the text after that fenced metadata envelope.
        metadata_marker = "Conversation info (untrusted metadata):"
        if metadata_marker in text:
            metadata_tail = text.rsplit(metadata_marker, 1)[1]
            fence_end = metadata_tail.find("```", metadata_tail.find("```") + 3)
            if fence_end >= 0:
                return metadata_tail[fence_end + 3:].strip()
        return text

    @staticmethod
    def parse_switch_command(message: Any) -> Optional[Tuple[str, str]]:
        """Parse an exact /switch command; ('', '') means rotate automatically."""
        if str(getattr(message, "role", "")).lower() != "user":
            return None

        text = RouterService._message_text(message)
        if text.lower() == RouterService._SWITCH_PREFIX:
            return "", ""
        if text.lower() == f"{RouterService._SWITCH_PREFIX}:auto":
            return RouterService._SWITCH_AUTO, ""
        if not text.lower().startswith(f"{RouterService._SWITCH_PREFIX}:"):
            return None

        target = text[len(RouterService._SWITCH_PREFIX) + 1:]
        if ":" not in target:
            return None
        provider_name, model_id = target.split(":", 1)
        provider_name = provider_name.strip().lower()
        model_id = model_id.strip()
        if not provider_name or not model_id:
            return None
        return provider_name, model_id

    @staticmethod
    def _current_switch_command(messages: Optional[List[Any]]) -> Optional[Tuple[str, str]]:
        """Return a command only when the current (last user) message is a command."""
        for message in reversed(messages or []):
            if str(getattr(message, "role", "")).lower() == "user":
                return RouterService.parse_switch_command(message)
        return None

    @staticmethod
    def _rotate_candidates(
        candidates: List[Tuple[str, str]], current: Optional[Tuple[str, str]]
    ) -> List[Tuple[str, str]]:
        if not candidates:
            return []
        if current not in candidates:
            return candidates
        start = candidates.index(current)
        return candidates[start:] + candidates[:start]

    @staticmethod
    def apply_session_switch(
        candidates: List[Tuple[str, str]],
        session_id: str,
        command: Optional[Tuple[str, str]],
    ) -> List[Tuple[str, str]]:
        """Apply/update the persistent switch state for one identified session."""
        if command is not None:
            provider_name, model_id = command
            if provider_name == RouterService._SWITCH_AUTO:
                RouterService._clear_session_switch(session_id)
                logger.info("[router] Switch command restored automatic routing")
                return candidates

            if provider_name:
                target = (provider_name, model_id)
                RouterService._set_session_switch(session_id, target, explicit=True)
                logger.info(f"[router] Persistent switch selected {target[0]}:{target[1]}")
                return [target]

            previous = RouterService._get_session_switch(session_id)
            current = (previous.provider, previous.model) if previous else (
                candidates[0] if candidates else None
            )
            if not candidates:
                return []
            if current in candidates:
                target = candidates[(candidates.index(current) + 1) % len(candidates)]
            else:
                target = candidates[0]
            RouterService._set_session_switch(session_id, target, explicit=False)
            logger.info(f"[router] Persistent switch rotated to {target[0]}:{target[1]}")
            return RouterService._rotate_candidates(candidates, target)

        state = RouterService._get_session_switch(session_id)
        if state is None:
            return candidates
        target = (state.provider, state.model)
        return [target] if state.explicit else RouterService._rotate_candidates(candidates, target)

    @staticmethod
    def apply_switch_commands(
        candidates: List[Tuple[str, str]], messages: Optional[List[Any]] = None
    ) -> List[Tuple[str, str]]:
        """Rebuild the selected model from exact switch commands in message history."""
        if not messages:
            return candidates

        current = candidates[0] if candidates else None
        switched = False
        explicit_target = False

        for message in messages:
            command = RouterService.parse_switch_command(message)
            if command is None:
                continue

            switched = True
            provider_name, model_id = command
            if provider_name == RouterService._SWITCH_AUTO:
                current = candidates[0] if candidates else None
                explicit_target = False
                continue
            if provider_name:
                current = (provider_name, model_id)
                explicit_target = True
                continue

            explicit_target = False
            if not candidates:
                current = None
            elif current in candidates:
                current = candidates[(candidates.index(current) + 1) % len(candidates)]
            else:
                current = candidates[0]

        if not switched or current is None:
            return candidates

        if explicit_target:
            logger.info(f"[router] Switch command selected {current[0]}:{current[1]}")
            return [current]

        start = candidates.index(current)
        rotated = candidates[start:] + candidates[:start]
        logger.info(f"[router] Switch command rotated auto routing to {current[0]}:{current[1]}")
        return rotated

    @staticmethod
    def is_provider_available(p_name: str, actual_id: str) -> bool:
        """Check if a provider/model is available (not melted)."""
        p_cfg = config.providers.get(p_name)
        if not p_cfg:
            return False
        now = time.time()
        if not any(now >= p_cfg.key_cooldowns.get(index, 0)
                   for index in range(len(p_cfg.api_keys))):
            return False
        return not (p_cfg.is_platform_melted() or p_cfg.is_model_melted(actual_id)
                    or actual_id in p_cfg.model_probe_tokens)

    @staticmethod
    def candidate_retry_after(p_name: str, actual_id: str) -> Optional[int]:
        """Earliest locally permitted retry, not a promise of upstream recovery."""
        import math
        p_cfg = config.providers.get(p_name)
        if not p_cfg or not p_cfg.api_keys:
            return None
        now = time.time()
        keys = [p_cfg.key_cooldowns.get(i, 0) for i in range(len(p_cfg.api_keys))
                if i not in p_cfg.fatal_keys]
        if not keys:
            return None
        ready_at = max(p_cfg.melted_until, p_cfg.model_cooldowns.get(actual_id, 0), min(keys))
        return max(0, math.ceil(ready_at - now))

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

        provider_order = getattr(config, "custom_provider_order", [])
        positions = {name: index for index, name in enumerate(provider_order)}
        def order_providers(candidates):
            return sorted(candidates, key=lambda pair: positions.get(pair[0], len(positions))) if positions else candidates

        if not config.custom_model_ranking:
            return order_providers(default_ranked)

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
        return order_providers(candidates)

    @staticmethod
    async def get_models_to_try(
        model_name: str,
        messages: Optional[List[Any]] = None,
        session_id: Optional[str] = None,
    ) -> List[Tuple[str, str]]:
        """Resolve a requested model name to a ranked list of (provider, actual_id)."""
        model_name = model_name.lower()
        models_to_try = []

        if model_name in AUTO_ROUTING_ALIASES:
            # Wait for cache if empty
            if not RouterService.build_auto_candidates():
                start_wait = time.time()
                while not config.auto_models_cache and (time.time() - start_wait) < 5.0:
                    await asyncio.sleep(1.0)
            models_to_try = RouterService.build_auto_candidates()
        else:
            p_cfg, actual = config.resolve_model(model_name)
            if p_cfg:
                models_to_try.append((p_cfg.name, actual))

        if session_id:
            current_command = RouterService._current_switch_command(messages)
            current_state = RouterService._get_session_switch(session_id)
            needs_auto_candidates = (
                model_name in AUTO_ROUTING_ALIASES
                or current_command in (("", ""), (RouterService._SWITCH_AUTO, ""))
                or (current_state is not None and not current_state.explicit)
            )
            if needs_auto_candidates:
                auto_candidates = RouterService.build_auto_candidates()
                switch_base = auto_candidates or models_to_try
            else:
                switch_base = models_to_try
            return RouterService.apply_session_switch(
                switch_base,
                session_id,
                current_command,
            )

        has_switch_command = bool(messages) and any(
            RouterService.parse_switch_command(message) is not None
            for message in messages
        )
        if not has_switch_command:
            return models_to_try

        auto_candidates = RouterService.build_auto_candidates()
        switch_base = auto_candidates or models_to_try
        return RouterService.apply_switch_commands(switch_base, messages)

    @staticmethod
    def record_failure(p_name: str, actual_id: str, error: Exception):
        from app.services.stats_service import stats_service
        p_cfg = config.providers.get(p_name)
        if not p_cfg:
            return
            
        gw_error = GatewayError.from_exception(error, provider=p_name, model=actual_id)
        from app.core.observability import log_event
        log_event(logger, "routing_failure", logging.WARNING, provider=p_name, model=actual_id,
                  status_code=gw_error.status_code, error_code=gw_error.error_code,
                  error_type=gw_error.error_type, retryable=gw_error.retryable,
                  exception_type=gw_error.exception_type, timeout_phase=gw_error.timeout_phase)
        err_str = str(gw_error)
        stats_service.record_request(p_name, actual_id, error=err_str)
        # Provider errors are counted by the Provider itself; only count this
        # gateway-owned deadline here, once, without disabling a whole platform.
        if gw_error.error_code in {'first_output_timeout', 'candidate_first_chunk_timeout', 'candidate_idle_timeout'}:
            p_cfg.record_transient_model_failure(actual_id)

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
        from app.core.observability import EventFormatter, request_id
        if config.settings.logging.include_diagnostics:
            secrets = [config.settings.api_key, *config.settings.api_keys.split(",")]
            for provider in config.providers.values():
                secrets.extend(provider.api_keys)
            err_str = EventFormatter(config.settings.logging, lambda: secrets).redact(err_str)
        else:
            err_str = f"request_id={request_id.get()} error_code={error.error_code if error else err_type}"
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
