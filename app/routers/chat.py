import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import config
from app.core.observability import log_event, request_fingerprint
from app.models import (
    ChatCompletionRequest,
    ModelInfo,
    ModelListResponse,
    MultiChatCompletionRequest,
)
from app.services.agent import AgentService
from app.services.router import AUTO_ROUTING_ALIASES
from app.services.multi_chat import MultiChatRequestError, MultiChatService
from app.utils.responses import make_error_response

router = APIRouter()
logger = logging.getLogger("freelance")


@router.post("/chat/completions/multi")
async def multi_chat_completions(
    fastapi_request: Request,
    request: MultiChatCompletionRequest,
):
    """Return independent answers from multiple configured models."""
    object.__setattr__(request, "_gateway_headers", dict(fastapi_request.headers))
    log_event(logger, "multi_chat_requested", models=request.models, parallel=request.parallel,
              request_fingerprint=request_fingerprint(request))
    if config.settings.logging.log_request_payload:
        log_event(logger, "request_payload", request_payload=request.model_dump(exclude_none=True))
    try:
        targets = MultiChatService.prepare_targets(request)
    except MultiChatRequestError as exc:
        return make_error_response(
            str(exc),
            400,
            type="invalid_request_error",
            code=exc.code,
        )

    result = await MultiChatService.run(request, targets)
    body = result.model_dump(exclude_none=True)
    if result.status == "failed":
        all_timed_out = all(
            item.error_code == "multi_timeout" for item in result.responses
        )
        return JSONResponse(status_code=504 if all_timed_out else 502, content=body)
    return body


@router.post("/chat/completions")
async def chat_completions(fastapi_request: Request, request: ChatCompletionRequest):
    """
    OpenAI-compatible chat completion endpoint.
    Orchestrates multi-turn dialogue via AgentService.
    """
    model_name = (request.model or "auto").lower()
    log_event(logger, "chat_requested", model=model_name, stream=request.stream,
              message_count=len(request.messages), request_fingerprint=request_fingerprint(request))
    if config.settings.logging.log_request_payload:
        log_event(logger, "request_payload", request_payload=request.model_dump(exclude_none=True))

    # Keep the original client request available for provider-level pass-through.
    object.__setattr__(request, "_gateway_raw_body", await fastapi_request.body())
    object.__setattr__(request, "_gateway_headers", dict(fastapi_request.headers))

    agent = AgentService(fastapi_request)
    return await agent.run(request)


def virtual_models():
    """Advertise routing contracts, not inferred upstream capabilities."""
    models = {
        alias: ModelInfo(id=alias, score=None, virtual=True, routing_type="auto")
        for alias in AUTO_ROUTING_ALIASES
    }
    for alias, route in config.model_routes.items():
        # Chat routing lowercases names before looking them up.
        if alias == alias.lower() and alias not in models and route.provider in config.providers:
            models[alias] = ModelInfo(id=alias, score=None, virtual=True,
                routing_type="configured_alias", route_provider=route.provider, route_model=route.model)
    return models


@router.get("/models", response_model=ModelListResponse, response_model_exclude_none=True)
async def list_models(mode: str = "all"):
    """Lists available models from the auto-routing cache."""
    from app.scoring import rank_models

    scored = rank_models(config.auto_models_cache, only_free=(mode == "free"))
    aliases = virtual_models()
    return ModelListResponse(data=list(aliases.values()) + [
        ModelInfo(
            id=str(m["id"]),
            created=int(time.time()),
            owned_by=f"{m.get('_gateway_provider','unknown')} | Score: {m.get('_score',0):.3f}",
            score=m.get("_score", 0.0),
            context_length=m.get("context_length"),
            capabilities=m.get("supported_parameters") or m.get("capabilities"),
            score_breakdown=m.get("_score_components"),
        )
        for m in scored
        if isinstance(m, dict) and m.get("id") and str(m["id"]).lower() not in aliases
    ])


@router.get("/models/{model_id:path}")
async def get_model(model_id: str):
    """Fetches details for a specific model from the cache."""
    if model_id.startswith("{"):
        model_id = model_id[1:-1]

    alias = virtual_models().get(model_id.lower())
    if alias is not None:
        return alias.model_dump(exclude_none=True)

    for m in config.auto_models_cache:
        if m.get("id") == model_id:
            return ModelInfo(
                id=str(m["id"]),
                created=int(time.time()),
                owned_by=m.get("_gateway_provider", "cache"),
                score=m.get("_score", 0.0),
                context_length=m.get("context_length"),
                capabilities=m.get("supported_parameters") or m.get("capabilities"),
                score_breakdown=m.get("_score_components"),
            ).model_dump()

    return make_error_response("Not found", 404)
