import asyncio
import logging
import time
from typing import Optional

from app.config import config
from app.models import (
    ChatCompletionResponse,
    MultiChatCompletionItem,
    MultiChatCompletionRequest,
    MultiChatCompletionResponse,
    UsageInfo,
)
from app.services.router import RouterService, get_provider_instance

logger = logging.getLogger("freelance.multi_chat")


class MultiChatRequestError(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class MultiChatService:
    @staticmethod
    def _resolve_model(model_spec: str):
        """Resolve either an alias/model ID or an explicit provider:model target."""
        spec = str(model_spec or "").strip()
        if not spec:
            raise MultiChatRequestError("Model target cannot be empty", "invalid_model_target")

        provider_prefix, separator, explicit_model = spec.partition(":")
        provider_name = provider_prefix.lower()
        if separator and provider_name in config.providers:
            if not explicit_model.strip():
                raise MultiChatRequestError(
                    f"Model is missing from target '{spec}'",
                    "invalid_model_target",
                )
            return config.providers[provider_name], explicit_model.strip()

        provider_config, actual_model = config.resolve_model(spec.lower())
        if not provider_config:
            raise MultiChatRequestError(
                f"Model target '{spec}' could not be resolved",
                "invalid_model_target",
            )
        return provider_config, actual_model

    @staticmethod
    def _configured_default_targets() -> list[tuple[str, str]]:
        """Return every callable provider default model in configuration order."""
        targets = []
        seen = set()
        for provider_name, provider_config in config.providers.items():
            actual_model = str(provider_config.default_model or "").strip()
            target = (provider_name, actual_model)
            if not provider_config.api_keys or not actual_model or target in seen:
                continue
            seen.add(target)
            targets.append(target)
        return targets

    @staticmethod
    def prepare_targets(request: MultiChatCompletionRequest) -> list[tuple[str, str]]:
        """Validate a multi request and resolve its explicit or configured targets."""
        if config.settings.tools.normalized_mode() == "local_agent":
            raise MultiChatRequestError(
                "Multi-model requests do not support the local Agent tool loop; "
                "use tools.mode=passthrough or the single-model endpoint",
                "multi_local_agent_not_supported",
            )

        targets = []
        seen = set()
        if request.models is None:
            targets = MultiChatService._configured_default_targets()
            if not targets:
                raise MultiChatRequestError(
                    "No callable provider default models are configured",
                    "no_default_multi_models",
                )
        else:
            for model_spec in request.models:
                provider_config, actual_model = MultiChatService._resolve_model(model_spec)
                target = (provider_config.name, actual_model)
                if target in seen:
                    raise MultiChatRequestError(
                        f"Duplicate model target: {target[0]}:{target[1]}",
                        "duplicate_model",
                    )
                seen.add(target)
                targets.append(target)

        max_models = config.settings.multi_model_max_models
        if len(targets) > max_models:
            source = "configured default models" if request.models is None else "models requested"
            raise MultiChatRequestError(
                f"Too many {source}: {len(targets)}; maximum is {max_models}",
                "too_many_models",
            )
        return targets

    @staticmethod
    async def _call_model(
        request: MultiChatCompletionRequest,
        provider_name: str,
        actual_model: str,
    ) -> MultiChatCompletionItem:
        """Call one target without allowing its failure to cancel sibling calls."""
        from app.services.stats_service import stats_service

        started = time.perf_counter()
        provider_config = config.providers.get(provider_name)
        provider = get_provider_instance(provider_name)

        if not provider_config or not provider_config.api_keys:
            return MultiChatCompletionItem(
                provider=provider_name,
                model=actual_model,
                status="error",
                latency_ms=0.0,
                error=f"Provider '{provider_name}' has no configured API key",
            )
        if not RouterService.is_provider_available(provider_name, actual_model):
            return MultiChatCompletionItem(
                provider=provider_name,
                model=actual_model,
                status="error",
                latency_ms=0.0,
                error=RouterService.get_provider_status_message(provider_name, actual_model),
            )
        if provider is None:
            return MultiChatCompletionItem(
                provider=provider_name,
                model=actual_model,
                status="error",
                latency_ms=0.0,
                error=f"Provider '{provider_name}' is unavailable",
            )

        single_request = request.to_single_request(actual_model)
        gateway_headers = getattr(request, "_gateway_headers", None)
        if gateway_headers:
            object.__setattr__(single_request, "_gateway_headers", gateway_headers)

        try:
            response = await provider.chat_completion(
                single_request,
                actual_model,
                timeout=config.settings.unified_timeout or provider_config.timeout,
            )
            processed = (
                response
                if isinstance(response, ChatCompletionResponse)
                else ChatCompletionResponse(**response)
            )
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            stats_service.record_request(
                provider_name,
                actual_model,
                latency_ms=latency_ms,
            )
            provider_config.reset_consecutive_failures()

            content: Optional[str] = None
            if processed.choices and processed.choices[0].message:
                content = processed.choices[0].message.content
            return MultiChatCompletionItem(
                provider=provider_name,
                model=actual_model,
                status="success",
                latency_ms=latency_ms,
                content=content,
                response=processed.model_dump(exclude_none=True),
                usage=processed.usage,
            )
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.warning(f"[MULTI] {provider_name}/{actual_model} failed: {exc}")
            RouterService.record_failure(provider_name, actual_model, exc)
            return MultiChatCompletionItem(
                provider=provider_name,
                model=actual_model,
                status="error",
                latency_ms=latency_ms,
                error=str(exc) or repr(exc),
            )

    @staticmethod
    def _timeout_item(
        provider_name: str,
        actual_model: str,
        timeout_seconds: float,
    ) -> MultiChatCompletionItem:
        return MultiChatCompletionItem(
            provider=provider_name,
            model=actual_model,
            status="error",
            latency_ms=round(timeout_seconds * 1000, 2),
            error=f"Multi-model request exceeded its {timeout_seconds:g}s overall timeout",
            error_code="multi_timeout",
        )

    @staticmethod
    async def _run_parallel_with_deadline(
        request: MultiChatCompletionRequest,
        targets: list[tuple[str, str]],
        timeout_seconds: float,
    ) -> list[MultiChatCompletionItem]:
        tasks = [
            asyncio.create_task(
                MultiChatService._call_model(request, provider_name, actual_model)
            )
            for provider_name, actual_model in targets
        ]
        done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
        if pending:
            logger.warning(
                f"[MULTI] Overall timeout reached after {timeout_seconds:g}s; "
                f"cancelling {len(pending)} unfinished model(s)"
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        responses = []
        for target, task in zip(targets, tasks):
            if task in done:
                responses.append(task.result())
            else:
                responses.append(
                    MultiChatService._timeout_item(*target, timeout_seconds)
                )
        return responses

    @staticmethod
    async def _run_serial_with_deadline(
        request: MultiChatCompletionRequest,
        targets: list[tuple[str, str]],
        timeout_seconds: float,
    ) -> list[MultiChatCompletionItem]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        responses = []
        for index, (provider_name, actual_model) in enumerate(targets):
            remaining = deadline - loop.time()
            if remaining <= 0:
                responses.extend(
                    MultiChatService._timeout_item(*target, timeout_seconds)
                    for target in targets[index:]
                )
                break
            try:
                response = await asyncio.wait_for(
                    MultiChatService._call_model(request, provider_name, actual_model),
                    timeout=remaining,
                )
                responses.append(response)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[MULTI] Overall timeout reached after {timeout_seconds:g}s "
                    f"during serial model calls"
                )
                responses.append(
                    MultiChatService._timeout_item(
                        provider_name, actual_model, timeout_seconds
                    )
                )
                responses.extend(
                    MultiChatService._timeout_item(*target, timeout_seconds)
                    for target in targets[index + 1:]
                )
                break
        return responses

    @staticmethod
    async def run(
        request: MultiChatCompletionRequest,
        targets: Optional[list[tuple[str, str]]] = None,
    ) -> MultiChatCompletionResponse:
        targets = targets or MultiChatService.prepare_targets(request)
        timeout_seconds = config.settings.multi_model_timeout

        logger.info(
            f"[MULTI] Calling {len(targets)} model(s); parallel={request.parallel}; "
            f"overall_timeout={timeout_seconds:g}s"
        )
        if request.parallel:
            responses = await MultiChatService._run_parallel_with_deadline(
                request, targets, timeout_seconds
            )
        else:
            responses = await MultiChatService._run_serial_with_deadline(
                request, targets, timeout_seconds
            )

        successes = sum(item.status == "success" for item in responses)
        if successes == len(responses):
            status = "success"
        elif successes:
            status = "partial_success"
        else:
            status = "failed"

        usage = UsageInfo(
            prompt_tokens=sum(item.usage.prompt_tokens for item in responses if item.usage),
            completion_tokens=sum(item.usage.completion_tokens for item in responses if item.usage),
            total_tokens=sum(item.usage.total_tokens for item in responses if item.usage),
        )
        return MultiChatCompletionResponse(
            status=status,
            parallel=request.parallel,
            responses=responses,
            usage=usage,
        )
