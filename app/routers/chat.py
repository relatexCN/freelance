import logging
import time

from fastapi import APIRouter, Request

from app.config import config
from app.models import ChatCompletionRequest, ModelInfo, ModelListResponse
from app.services.agent import AgentService
from app.utils.responses import make_error_response

router = APIRouter()
logger = logging.getLogger("freelance")


@router.post("/chat/completions")
async def chat_completions(fastapi_request: Request, request: ChatCompletionRequest):
    """
    OpenAI-compatible chat completion endpoint.
    Orchestrates multi-turn dialogue via AgentService.
    """
    model_name = (request.model or "auto").lower()
    last_msg = request.messages[-1].content[:50] if request.messages else ""
    logger.info(f"[*] REQ: {model_name} | Stream: {request.stream} | Msg: {last_msg}...")

    # Keep the original client request available for provider-level pass-through.
    object.__setattr__(request, "_gateway_raw_body", await fastapi_request.body())
    object.__setattr__(request, "_gateway_headers", dict(fastapi_request.headers))

    agent = AgentService(fastapi_request)
    return await agent.run(request)


@router.get("/models", response_model=ModelListResponse)
async def list_models(mode: str = "all"):
    """Lists available models from the auto-routing cache."""
    from app.scoring import rank_models

    scored = rank_models(config.auto_models_cache, only_free=(mode == "free"))
    return ModelListResponse(data=[
        ModelInfo(
            id=str(m["id"]),
            created=int(time.time()),
            owned_by=f"{m.get('_gateway_provider','unknown')} | Score: {m.get('_score',0):.3f}",
            score=m.get("_score", 0.0),
            context_length=m.get("context_length"),
            capabilities=m.get("supported_parameters") or m.get("capabilities"),
        )
        for m in scored
        if isinstance(m, dict) and m.get("id")
    ])


@router.get("/models/{model_id:path}")
async def get_model(model_id: str):
    """Fetches details for a specific model from the cache."""
    if model_id.startswith("{"):
        model_id = model_id[1:-1]

    for m in config.auto_models_cache:
        if m.get("id") == model_id:
            return ModelInfo(
                id=str(m["id"]),
                created=int(time.time()),
                owned_by=m.get("_gateway_provider", "cache"),
                score=m.get("_score", 0.0),
                context_length=m.get("context_length"),
                capabilities=m.get("supported_parameters") or m.get("capabilities"),
            ).model_dump()

    return make_error_response("Not found", 404)
