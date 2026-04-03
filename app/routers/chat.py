"""
Chat completions router - OpenAI-compatible /v1/chat/completions endpoint.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.config import config
from app.models.request import ChatCompletionRequest
from app.models.response import ModelInfo, ModelListResponse
from app.providers.base import BaseProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.utils.tool_converter import xml_to_tool_calls, has_xml_tool_call

logger = logging.getLogger("link")

router = APIRouter()

# Provider registry
_providers: dict[str, BaseProvider] = {}


def _get_provider(name: str) -> BaseProvider:
    """Get or create a provider instance by name."""
    if name not in _providers:
        provider_config = config.providers.get(name)
        if not provider_config:
            logger.error(f"Provider '{name}' not found. Available: {list(config.providers.keys())}")
            raise HTTPException(
                status_code=404,
                detail=f"Provider '{name}' not found or misconfigured.",
            )

        if provider_config.provider_type == "ollama" or provider_config.provider_type == "openai":
            _providers[name] = OpenAICompatibleProvider(provider_config)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported provider type: {provider_config.provider_type}",
            )
    return _providers[name]


@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """
    OpenAI-compatible chat completions endpoint.
    Routes to the appropriate provider based on model name, or auto-routes.
    """
    # 1. Default to 'auto' if model is empty or not provided
    model_name = (request.model or "auto").lower()

    # Debug: Log the incoming request
    logger.info(f"--- [REQUEST] ---")
    logger.info(f"Model (requested): {request.model}")
    logger.info(f"Stream: {request.stream}")
    if request.messages:
        last_msg = request.messages[-1].content
        # Truncate content for cleaner terminal output
        preview = (last_msg[:100] + "...") if len(last_msg) > 100 else last_msg
        logger.info(f"Last User Message: {preview}")
    logger.info(f"-----------------")
    
    try:
        # Check if the user is requesting auto-routing
        if model_name in ("auto", "free", "best-free"):
            if not config.auto_models_cache:
                raise HTTPException(
                    status_code=503,
                    detail="Auto-routing cache is empty. Please wait for the background task to complete."
                )
            
            # The fallback loop for auto-routing
            max_retries = config.settings.max_retries
            melted_providers = set()
            provider_errors = {}  # p_name -> error_count
            effective_attempts = 0
            
            # Deep scan: Iterate through all cached models, but stop after max_retries actual attempts
            for model_info in config.auto_models_cache:
                if effective_attempts >= max_retries:
                    break
                
                actual_model = model_info["id"]
                p_name = model_info.get("_gateway_provider", config.default_provider)
                
                # Circuit breaker: Skip if this provider has failed multiple times in this request
                if p_name in melted_providers:
                    logger.info(f"Skipping melted provider: {p_name} (Model: {actual_model})")
                    continue
                
                provider = _get_provider(p_name)
                
                logger.info(
                    f"Auto-routing attempt {effective_attempts + 1}/{max_retries}: "
                    f"Model '{actual_model}' (score: {model_info.get('_score', 0):.3f}) via '{p_name}'"
                )
                
                # Tiered timeout: 20s for the first actual attempt, default for retries
                current_timeout = 20.0 if effective_attempts == 0 else None
                
                # Increment BEFORE trying, because a failure in try block will still count as an attempt
                effective_attempts += 1
                
                try:
                    if request.stream:
                        gen = provider.chat_completion_stream(
                            request, actual_model, timeout=current_timeout
                        )
                        
                        # Sneak peek the first chunk to catch connection errors (403, 500, etc.) early
                        try:
                            # Start the generator and get the first chunk
                            first_chunk = await gen.__anext__()
                        except Exception as e:
                            # Connection failed BEFORE sending headers, so we can fallback!
                            logger.warning(f"Auto-routing attempt failed: {p_name}/{actual_model} -> {e}. Trying next...")
                            continue

                        # Debug: Log the streaming start
                        logger.info(f"--- [STREAM STARTING] ---")
                        logger.info(f"Serving stream via: {actual_model}")
                        
                        # Rebuild the generator with XML tool call detection and conversion
                        async def rebuilt_generator():
                            full_content = ""
                            has_xml = False
                            tool_calls = []
                            xml_started = False
                            xml_completed = False
                            model_name = actual_model
                            
                            def get_content(c_str):
                                try:
                                    if c_str == "[DONE]": return ""
                                    import json
                                    data = json.loads(c_str)
                                    content = (data.get("choices", [{}])[0].get("delta", {}).get("content") or "")
                                    return content
                                except: return ""

                            # Check first chunk for XML tool calls
                            first_content = get_content(first_chunk)
                            full_content += first_content
                            
                            if has_xml_tool_call(first_content):
                                logger.info("Detected XML tool calls in streaming response, converting...")
                                has_xml = True
                                xml_started = True
                            
                            # If XML started, don't yield the first chunk yet - wait until we have complete XML
                            if not xml_started:
                                yield first_chunk
                            
                            async for chunk in gen:
                                chunk_content = get_content(chunk)
                                full_content += chunk_content
                                
                                # Check for XML tool calls in accumulated content
                                if not has_xml and has_xml_tool_call(full_content):
                                    logger.info("Detected XML tool calls in streaming response, converting...")
                                    has_xml = True
                                    xml_started = True
                                
                                # Check if we're still building XML content
                                if has_xml:
                                    if "</function_calls>" in full_content:
                                        # XML is complete, convert to tool calls
                                        tool_calls = xml_to_tool_calls(full_content)
                                        if tool_calls:
                                            logger.info(f"Converted {len(tool_calls)} tool calls in streaming response")
                                            # Generate new chunk with tool_calls instead of XML
                                            import json
                                            import time
                                            # Create the initial chunk with tool_calls
                                            tool_call_chunk = {
                                                "id": "chatcmpl-123",
                                                "object": "chat.completion.chunk",
                                                "created": int(time.time()),
                                                "model": model_name,
                                                "choices": [
                                                    {
                                                        "index": 0,
                                                        "delta": {
                                                            "tool_calls": tool_calls
                                                        },
                                                        "finish_reason": None
                                                    }
                                                ]
                                            }
                                            # Yield the tool call chunk
                                            yield f"data: {json.dumps(tool_call_chunk)}" + "\n"
                                            # Mark XML as completed
                                            xml_completed = True
                                        has_xml = False
                                
                                # Only yield the original chunk if we're not processing XML
                                if not xml_started:
                                    yield chunk
                            
                            yield "[DONE]"
                            logger.info(f"--- [STREAM CONTENT] ---" )
                            logger.info(full_content)
                            logger.info(f"--- [STREAM FINISHED] ---" )
                                
                        return EventSourceResponse(
                            rebuilt_generator(),
                            media_type="text/event-stream"
                        )
                    else:
                        response = await provider.chat_completion(
                            request, actual_model, timeout=current_timeout
                        )
                        
                        # Check for XML tool calls and convert to OpenAI format
                        if response.choices and response.choices[0].message and response.choices[0].message.content:
                            content = response.choices[0].message.content
                            if has_xml_tool_call(content):
                                logger.info("Detected XML tool calls, converting to OpenAI format...")
                                tool_calls = xml_to_tool_calls(content)
                                if tool_calls:
                                    # Replace content with empty string and add tool_calls
                                    response.choices[0].message.content = None
                                    response.choices[0].message.tool_calls = tool_calls
                                    logger.info(f"Converted {len(tool_calls)} tool calls")
                        
                        # Debug: Log the full response
                        logger.info(f"--- [JSON RESPONSE] ---" )
                        logger.info(response.model_dump_json(indent=2))
                        logger.info(f"-----------------------" )
                        
                        return JSONResponse(content=response.model_dump(exclude_none=True))
                        
                except Exception as e:
                    # Update circuit breaker tracking for this provider
                    provider_errors[p_name] = provider_errors.get(p_name, 0) + 1
                    if provider_errors[p_name] >= 2:
                        logger.info(f"Melting provider '{p_name}' after {provider_errors[p_name]} failures")
                        melted_providers.add(p_name)
                    
                    logger.warning(f"Auto-routing attempt failed: {p_name}/{actual_model} -> {e}. Trying next...")
                    continue
            
            # If we exhausted the loop without returning
            raise Exception(f"All {effective_attempts} auto-routing attempts failed across available providers.")

        # 2. Regular specific model routing
        provider_config, actual_model = config.resolve_model(model_name)
        provider = _get_provider(provider_config.name)
        
        # Max attempts is based on the number of keys we have, capped at a reasonable limit (or settings.max_retries)
        max_attempts = max(1, min(len(provider_config.api_keys), config.settings.max_retries))
        
        for attempt in range(max_attempts):
            logger.info(
                f"Routing model '{request.model}' -> provider '{provider_config.name}' "
                f"(Attempt {attempt+1}/{max_attempts}), actual model: '{actual_model}', stream: {request.stream}"
            )
            
            # Tiered timeout: 20s for the first attempt, default for retries
            current_timeout = 20.0 if attempt == 0 else None
            
            try:
                if request.stream:
                    gen = provider.chat_completion_stream(
                        request, actual_model, timeout=current_timeout
                    )
                    
                    # Sneak peek the first chunk to catch key-related errors early (403, 401, etc.)
                    try:
                        first_chunk = await gen.__anext__()
                    except Exception as e:
                        logger.warning(f"Routing attempt {attempt + 1} failed for {request.model}: {e}")
                        if attempt == max_attempts - 1:
                            raise
                        continue

                    # Debug: Log the streaming start
                    logger.info(f"--- [STREAM STARTING] ---")
                    logger.info(f"Serving stream via: {actual_model}")

                    # Rebuild the generator to include the first chunk and add a completion log
                    async def rebuilt_generator():
                        full_content = ""
                        has_xml = False
                        tool_calls = []
                        xml_started = False
                        xml_completed = False
                        model_name = actual_model
                        
                        # Helper to extract content from chunk string
                        def get_content(c_str):
                            try:
                                if c_str == "[DONE]": return ""
                                import json
                                data = json.loads(c_str)
                                # Use 'or ""' to handle cases where 'content' key exists but value is 'null'
                                content = (data.get("choices", [{}])[0].get("delta", {}).get("content") or "")
                                return content
                            except: return ""

                        # Check first chunk for XML tool calls
                        first_content = get_content(first_chunk)
                        full_content += first_content
                        
                        if has_xml_tool_call(first_content):
                            logger.info("Detected XML tool calls in streaming response, converting...")
                            has_xml = True
                            xml_started = True
                        
                        # If XML started, don't yield the first chunk yet - wait until we have complete XML
                        if not xml_started:
                            yield first_chunk
                        
                        async for chunk in gen:
                            chunk_content = get_content(chunk)
                            full_content += chunk_content
                            
                            # Check for XML tool calls in accumulated content
                            if not has_xml and has_xml_tool_call(full_content):
                                logger.info("Detected XML tool calls in streaming response, converting...")
                                has_xml = True
                                xml_started = True
                            
                            # Check if we're still building XML content
                            if has_xml:
                                if "</function_calls>" in full_content:
                                    # XML is complete, convert to tool calls
                                    tool_calls = xml_to_tool_calls(full_content)
                                    if tool_calls:
                                        logger.info(f"Converted {len(tool_calls)} tool calls in streaming response")
                                        # Generate new chunk with tool_calls instead of XML
                                        import json
                                        import time
                                        # Create the initial chunk with tool_calls
                                        tool_call_chunk = {
                                            "id": "chatcmpl-123",
                                            "object": "chat.completion.chunk",
                                            "created": int(time.time()),
                                            "model": model_name,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {
                                                        "tool_calls": tool_calls
                                                    },
                                                    "finish_reason": None
                                                }
                                            ]
                                        }
                                        # Yield the tool call chunk
                                        yield f"data: {json.dumps(tool_call_chunk)}" + "\n"
                                        # Mark XML as completed
                                        xml_completed = True
                                    has_xml = False
                            
                            # Only yield the original chunk if we're not processing XML
                            if not xml_started:
                                yield chunk
                        
                        yield "[DONE]"
                        logger.info(f"--- [STREAM CONTENT] ---" )
                        logger.info(full_content)
                        logger.info(f"--- [STREAM FINISHED] ---" )

                    return EventSourceResponse(
                        rebuilt_generator(),
                        media_type="text/event-stream"
                    )
                else:
                    # Non-streaming response
                    response = await provider.chat_completion(
                        request, actual_model, timeout=current_timeout
                    )
                    
                    # Debug: Log the full response
                    logger.info(f"--- [JSON RESPONSE] ---")
                    logger.info(response.model_dump_json(indent=2))
                    logger.info(f"-----------------------")
                    
                    return JSONResponse(content=response.model_dump(exclude_none=True))
                    
            except Exception as e:
                if attempt == max_attempts - 1:
                    logger.error(f"Final specific model routing attempt failed: {e}")
                    raise
                
                # Retry will automatically fetch the next key via get_next_key()
                logger.warning(f"Specific model routing attempt {attempt + 1} failed: {e}. Retrying with next key...")
                continue
                
        raise Exception("All specific routing attempts failed.")

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error processing chat completion: {e}")
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "message": f"Upstream provider error: {str(e)}",
                    "type": "upstream_error",
                    "code": "provider_error",
                }
            },
        )


@router.get("/models", response_model=ModelListResponse)
async def list_models(mode: str = "free"):
    """
    List models with filtering:
    - mode=free (default): Only free models
    - mode=paid: Only paid models
    - mode=all: All available models
    """
    all_models = []
    
    # Process each already initialized provider
    for name, provider in _providers.items():
        try:
            p_models = await provider.list_models()
            for m in p_models:
                m["_gateway_provider"] = name
                all_models.append(m)
        except Exception as e:
            logger.warning(f"Could not fetch models from {name}: {e}")
            
    # Also include models from config.auto_models_cache to ensure persistence
    # (they are already scored and meta-tagged)
    cached_ids = {str(m.get("id")) for m in all_models}
    for m in config.auto_models_cache:
        if str(m.get("id")) not in cached_ids:
            all_models.append(m)

    # Filter based on mode
    from app.scoring import rank_models, filter_free_models, filter_paid_models
    
    if mode == "free":
        filtered = filter_free_models(all_models)
    elif mode == "paid":
        filtered = filter_paid_models(all_models)
    else:
        filtered = all_models
        
    # Rank the filtered set
    scored = rank_models(filtered, only_free=False)
    
    model_infos = []
    for m in scored:
        if not isinstance(m, dict) or not m.get("id"):
            continue
            
        score = m.get("_score", 0.0)
        p_name = m.get("_gateway_provider", "unknown")
        
        model_infos.append(
            ModelInfo(
                id=str(m.get("id")),
                created=int(m.get("created", int(time.time()))),
                owned_by=f"{p_name} | Score: {score:.4f}",
                score=score,
                context_length=m.get("context_length"),
                capabilities=m.get("supported_parameters")
            )
        )
            
    return ModelListResponse(data=model_infos)


@router.get("/models/{model_id:path}")
async def get_model(model_id: str):
    """Get information about a specific model."""
    # Strip formal curly braces if provided (e.g., {google/gemini...})
    if model_id.startswith("{") and model_id.endswith("}"):
        model_id = model_id[1:-1]
    
    # 1. Check static routes first
    if model_id in config.model_routes:
        route = config.model_routes[model_id]
        return ModelInfo(id=model_id, owned_by=route.provider).model_dump()

    # 2. Fallback: Search in the dynamic model cache
    for m in config.auto_models_cache:
        if m.get("id") == model_id:
            p_name = m.get("_gateway_provider", "unknown")
            score = m.get("_score", 0.0)
            return ModelInfo(
                id=str(m.get("id")),
                created=int(m.get("created", int(time.time()))),
                owned_by=f"{p_name} | Score: {score:.4f}",
                score=score,
                context_length=m.get("context_length"),
                capabilities=m.get("supported_parameters")
            ).model_dump()

    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
