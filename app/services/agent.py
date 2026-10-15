"""Single-request routing with streaming and provider fallback; no local execution."""
import time
import logging
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from app.models import ChatCompletionRequest
from app.config import config
from app.core.observability import log_event
from app.utils.responses import make_error_response
from app.services.router import RouterService, get_provider_instance
from app.services.streamer import StreamerService
from app.services.gateway_errors import GatewayError
from app.services.stream_fallback import can_fallback

logger = logging.getLogger("freelance.agent")

class AgentService:
    def __init__(self, fastapi_request: Request):
        self.fastapi_request = fastapi_request

    async def run(self, request: ChatCompletionRequest):
        model_name = (request.model or "auto").lower()
        session_id = RouterService.resolve_session_id(request)
        models_to_try = await RouterService.get_models_to_try(
            model_name,
            request.messages,
            session_id=session_id,
        )

        if not models_to_try:
            return make_error_response("No available models found", 404)

        models_to_try = list(dict.fromkeys(models_to_try + RouterService.build_auto_candidates()))

        if request.stream:
            from app.services.stream_fallback import stream_candidates
            # The requested model remains first; configured defaults provide fallbacks.
            gen = stream_candidates(request, self.fastapi_request, models_to_try)
            relay = StreamerService.stream_with_heartbeat(
                gen, 'routing', model_name, self.fastapi_request, initial_chunks=1,
                monitor_read_timeout=False)
            return StreamingResponse(relay, media_type="text/event-stream")

        tried_details = []
        last_err = None
        last_exception = None
        iteration = 1
        # Try each candidate until one completes
        for p_name, actual_id in models_to_try:
            if not RouterService.is_provider_available(p_name, actual_id):
                status_msg = RouterService.get_provider_status_message(p_name, actual_id)
                tried_details.append(f"{status_msg} (Skipped)")
                continue

            provider = get_provider_instance(p_name)
            if not provider:
                tried_details.append(f"{p_name} instance not found")
                continue

            try:
                log_event(logger, "provider_selected", provider=p_name, model=actual_id,
                          iteration=iteration, stream=request.stream)
                from app.services.stats_service import stats_service
                start_time = time.time()
                resp = await provider.chat_completion(request, actual_id, timeout=config.settings.unified_timeout or 30)
                latency = (time.time() - start_time) * 1000
                stats_service.record_request(p_name, actual_id, latency_ms=latency)
                p_cfg = config.providers.get(p_name)
                if p_cfg:
                    p_cfg.reset_consecutive_failures()

                return JSONResponse(content=resp.model_dump(exclude_none=True))
            except Exception as e:
                last_exception = e
                last_err = str(e)
                tried_details.append(f"{p_name}/{actual_id} error: {last_err}")
                RouterService.record_failure(p_name, actual_id, e)
                if isinstance(e, GatewayError) and not can_fallback(e):
                    status_code = e.status_code if e.status_code is not None else 500
                    return make_error_response(
                        last_err,
                        status_code=status_code,
                        type=e.error_type,
                        code=e.error_code,
                    )
                continue

        # Return a detailed error after exhausting the candidates
        # Deduplicate similar errors to avoid verbose messages
        error_counts = {}
        for detail in tried_details:
            # Normalize error: extract the key pattern (provider + error type)
            if " (Skipped)" in detail:
                # Group skipped errors by provider
                key = detail.split(" (Skipped)")[0].strip()
            else:
                # Group other errors by provider + error type
                key = detail.split(" error: ")[0].strip() if " error: " in detail else detail[:80]
            error_counts[key] = error_counts.get(key, 0) + 1

        deduped_details = []
        for key, count in error_counts.items():
            if count > 1:
                deduped_details.append(f"{key} ({count}x)")
            else:
                deduped_details.append(key)

        details_str = "; ".join(deduped_details)
        err_msg = f"All providers failed for this request. Details: {details_str}"
        status_code = 503 if not last_err else 502
        if (
            isinstance(last_exception, GatewayError)
            and last_exception.status_code is not None
            and 400 <= last_exception.status_code <= 599
        ):
            status_code = last_exception.status_code
        return make_error_response(err_msg, status_code)
