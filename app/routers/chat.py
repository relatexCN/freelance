import asyncio
import time
import uuid
import json
import logging
import httpx
from typing import Optional, AsyncGenerator, Any, List, Dict
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.models import (
    ChatMessage, 
    ChatCompletionRequest, 
    ChatCompletionResponse,
    ChatCompletionMessage,
    ChatChoice,
    UsageInfo,
    ChatCompletionStreamResponse,
    DeltaMessage,
    StreamChoice,
    ModelInfo,
    ModelListResponse
)
from app.config import config
from app.providers import get_provider
from app.utils.tool_converter import has_xml_tool_call, xml_to_tool_calls
from app.utils.tool_executor import ToolExecutor, format_tool_result

router = APIRouter()
logger = logging.getLogger("freelance")

async def _process_tool_calls(response_content: str, p_name: str, actual_id: str, request: ChatCompletionRequest) -> Any:
    """Process tool calls in model response and execute them."""
    # Check if response contains XML tool calls
    if not has_xml_tool_call(response_content):
        return None
    
    logger.info(f"[TOOL] Detected tool call in response from {p_name}/{actual_id}")
    
    # Parse XML tool calls to OpenAI format
    tool_calls = xml_to_tool_calls(response_content)
    if not tool_calls:
        logger.warning("[TOOL] Failed to parse tool calls from response")
        return None
    
    logger.info(f"[TOOL] Parsed {len(tool_calls)} tool call(s)")
    
    # Execute each tool call
    executor = ToolExecutor()
    tool_results = []
    
    for tool_call in tool_calls:
        tool_name = tool_call["function"]["name"]
        tool_call_id = tool_call["id"]
        
        try:
            # Parse arguments
            args = json.loads(tool_call["function"]["arguments"])
            logger.info(f"[TOOL] Executing {tool_name} with args: {args}")
            
            # Execute tool
            result = await executor.execute(tool_name, **args)
            
            # Format result
            formatted_result = format_tool_result(tool_call_id, result)
            tool_results.append(formatted_result)
            
            if result["success"]:
                logger.info(f"[TOOL] {tool_name} executed successfully")
            else:
                logger.warning(f"[TOOL] {tool_name} failed: {result.get('error')}")
                
        except Exception as e:
            logger.error(f"[TOOL] Error executing {tool_name}: {e}")
            tool_results.append({
                "tool_call_id": tool_call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": str(e), "result": None}, ensure_ascii=False)
            })
    
    return tool_results

def _make_error_response(message: str, status_code: int = 500, type: str = "upstream_error", code: Optional[str] = None):
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": type,
                "code": code or "internal_error"
            }
        }
    )

# Pre-initialize providers
_providers = {name: get_provider(p_cfg) for name, p_cfg in config.providers.items() if p_cfg.api_keys}

def _get_provider(name: str):
    """Fetch provider from global registry."""
    return _providers.get(name)

# Health checks are now handled directly by ProviderConfig objects.

def _process_non_stream_response(resp: Any) -> ChatCompletionResponse:
    """Helper to ensure response is valid pydantic model."""
    if isinstance(resp, ChatCompletionResponse):
        return resp
    # Fallback/Conversion for raw dicts
    return ChatCompletionResponse(**resp) if isinstance(resp, dict) else resp

def _get_model_size_level(model_id: str) -> str:
    """Determine model size level from model ID."""
    model_id_lower = model_id.lower()
    
    # Check for huge models (500B+) - e.g., 671b, 675b
    if any(x in model_id_lower for x in ["500b", "600b", "671b", "675b", "700b", "800b", "900b", "1t", "1000b"]):
        return "huge"
    
    # Check for extra-large models (100B-500B) - e.g., 100b, 405b
    if any(x in model_id_lower for x in ["100b", "128b", "175b", "200b", "300b", "400b", "405b", "5x"]):
        return "xlarge"
    
    # Check for large models (10B-100B) - e.g., 70b, 40b
    if any(x in model_id_lower for x in ["70b", "65b", "52b", "40b", "34b", "32b", "28b", "20b", "18b", "17b", "16b", "14b", "13b", "12b", "11b", "large", "3x", "4x"]):
        return "large"
    
    # Check for medium models (3B-10B)
    if any(x in model_id_lower for x in ["7b", "8b", "9b", "10b"]):
        return "medium"
    
    # Small models (<3B) or unrecognized
    return "small"

def _get_probe_timeout(model_id: str) -> int:
    """Determine first-token probe timeout based on model size."""
    size_level = _get_model_size_level(model_id)
    
    if size_level == "huge":
        return config.settings.stream_probe.defunct_model_timeout  # Use max timeout for huge models
    elif size_level == "xlarge":
        return config.settings.stream_probe.xlarge_model_timeout
    elif size_level == "large":
        return config.settings.stream_probe.large_model_timeout
    elif size_level == "medium":
        return config.settings.stream_probe.small_model_timeout
    else:
        # Small or unrecognized models - use default small timeout
        return config.settings.stream_probe.small_model_timeout

def _get_read_timeout(model_id: str) -> int:
    """Determine read timeout between chunks based on model size."""
    size_level = _get_model_size_level(model_id)
    
    if size_level == "huge":
        return config.settings.stream_probe.huge_model_read_timeout
    else:
        return config.settings.stream_probe.read_timeout

async def _stream_with_heartbeat(gen: AsyncGenerator, model_id: str, request: Request):
    """
    Wraps provider stream with heartbeat functionality and read timeout handling.
    Provider yields raw SSE strings (data: ...\n\n).
    """
    start_time = time.time()
    chunks_sent = 0
    last_activity_time = time.time()
    ping_interval = config.settings.stream_probe.ping_interval
    read_timeout = _get_read_timeout(model_id)  # Get model-specific read timeout
    
    logger.info(f"[*] Stream config for {model_id}: ping_interval={ping_interval}s, read_timeout={read_timeout}s")
    
    # Create a queue to coordinate between business data and heartbeat
    message_queue = asyncio.Queue()
    stop_event = asyncio.Event()
    timeout_event = asyncio.Event()  # Signal for read timeout
    
    async def heartbeat_task():
        """Background task to send heartbeat pings when idle and detect read timeout."""
        nonlocal last_activity_time
        try:
            while not stop_event.is_set():
                # Check if client is still connected
                if await request.is_disconnected():
                    logger.info(f"[HEARTBEAT] Client disconnected, stopping heartbeat for {model_id}")
                    break
                
                # Calculate time since last activity
                current_time = time.time()
                time_since_activity = current_time - last_activity_time
                
                # Check for read timeout
                if time_since_activity >= read_timeout:
                    logger.warning(f"[TIMEOUT] Read timeout detected for {model_id} (idle for {time_since_activity:.1f}s, threshold={read_timeout}s)")
                    timeout_event.set()
                    break
                
                if time_since_activity >= ping_interval:
                    # Send heartbeat as SSE comment (doesn't interfere with business data)
                    heartbeat = ": ping\n\n"
                    logger.info(f"[HEARTBEAT] Sending ping for {model_id} (idle for {time_since_activity:.1f}s)")
                    await message_queue.put(("heartbeat", heartbeat))
                    # Reset last activity time for heartbeat
                    last_activity_time = current_time
                
                # Check every 500ms to be responsive
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"[HEARTBEAT] Error in heartbeat task: {e}")
    
    async def business_data_task():
        """Task to process business data from upstream."""
        nonlocal last_activity_time
        nonlocal chunks_sent
        try:
            logger.info(f"[*] Starting stream for {model_id}")
            async for chunk in gen:
                # Check if timeout already occurred
                if timeout_event.is_set():
                    logger.warning(f"[*] Chunk received after timeout for {model_id}, ignoring")
                    continue
                
                # Check if client is still connected
                if await request.is_disconnected():
                    logger.warning(f"[*] Downstream client disconnected for {model_id}. Cancelling upstream request.")
                    break
                
                # Update last activity time
                last_activity_time = time.time()
                
                if chunks_sent == 0:
                    ttft = (time.time() - start_time) * 1000
                    logger.info(f"First chunk for {model_id} relayed at +{ttft:.2f}ms")
                
                # Log chunk size and content preview
                chunk_size = len(chunk)
                chunk_preview = chunk[:100] + "..." if chunk_size > 100 else chunk
                logger.debug(f"Relaying chunk {chunks_sent+1} ({chunk_size} bytes): {chunk_preview}")
                
                # Put business data into queue
                await message_queue.put(("business", chunk))
                chunks_sent += 1
                
        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                error_msg = str(e) or repr(e)
                logger.error(f"Stream relay interrupted for {model_id}: {error_msg}")
                error_chunk = {
                    "id": "stream-error",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {"content": f"\n\n[Gateway Connection Error: {type(e).__name__}]\n\n详细错误: {error_msg}"}, "finish_reason": "error"}]
                }
                error_chunk_str = f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
                logger.info(f"Sending error chunk: {error_chunk_str[:150]}...")
                await message_queue.put(("business", error_chunk_str))
                done_chunk = "data: [DONE]\n\n"
                logger.info(f"Sending done chunk: {done_chunk}")
                await message_queue.put(("business", done_chunk))
        finally:
            # Signal end of business data
            await message_queue.put(("end", None))
    
    # Start both tasks
    heartbeat_task = asyncio.create_task(heartbeat_task())
    business_task = asyncio.create_task(business_data_task())
    
    try:
        # Process messages from queue
        while True:
            # Wait for message with timeout
            try:
                msg_type, data = await asyncio.wait_for(message_queue.get(), timeout=read_timeout + 5)
            except asyncio.TimeoutError:
                logger.warning(f"[TIMEOUT] Queue timeout for {model_id}")
                break
            
            if msg_type == "end":
                logger.info(f"[*] Business data stream ended for {model_id}")
                break
            elif msg_type == "heartbeat":
                # Send heartbeat
                yield data
            elif msg_type == "business":
                # Send business data
                yield data
                
            message_queue.task_done()
            
            # Check if client is still connected
            if await request.is_disconnected():
                logger.warning(f"[*] Client disconnected, stopping stream for {model_id}")
                break
            
            # Check if timeout occurred
            if timeout_event.is_set():
                logger.warning(f"[*] Read timeout, stopping stream for {model_id}")
                break
                
    finally:
        # Clean up
        stop_event.set()
        
        # Wait for tasks to complete
        try:
            await asyncio.wait_for(heartbeat_task, timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("[HEARTBEAT] Heartbeat task timeout during cleanup")
        
        try:
            await asyncio.wait_for(business_task, timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("[*] Business data task timeout during cleanup")
        
        logger.info(f"--- [STREAM FINISHED] ({chunks_sent} chunks relayed) ---")

async def _mock_stream_from_response(resp: ChatCompletionResponse) -> AsyncGenerator[str, None]:
    """Wraps a standard ChatCompletionResponse into an SSE-compatible generator."""
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    
    logger.info(f"[*] Starting mock stream for {resp.model}")
    
    # Send content chunk
    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": resp.model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": resp.choices[0].message.content
                },
                "finish_reason": None
            }
        ]
    }
    chunk_str = json.dumps(chunk, ensure_ascii=False)
    sse_chunk = f"data: {chunk_str}\n\n"
    logger.info(f"Sending mock content chunk ({len(sse_chunk)} bytes): {chunk_str}")
    yield sse_chunk
    
    # Send finish chunk
    finish_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": resp.model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }
        ]
    }
    finish_chunk_str = json.dumps(finish_chunk, ensure_ascii=False)
    sse_finish_chunk = f"data: {finish_chunk_str}\n\n"
    logger.info(f"Sending mock finish chunk ({len(sse_finish_chunk)} bytes): {finish_chunk_str}")
    yield sse_finish_chunk
    
    logger.info(f"--- [STREAM FINISHED (MOCK)] ---")
    logger.info("Sending mock done chunk")
    yield "data: [DONE]\n\n"

@router.post("/chat/completions")
async def chat_completions(fastapi_request: Request, request: ChatCompletionRequest):
    model_name = (request.model or "auto").lower()
    
    # Request Content Logging
    last_msg = request.messages[-1].content if request.messages else ""
    logger.info(f"[*] REQ: {model_name} | Stream: {request.stream} | LastMsg: {last_msg}")
    
    models_to_try = []
    
    if model_name in ("auto", "free", "best-free"):
        # If cache is empty, we might be in the middle of a cold startup.
        # Wait up to 3 seconds before giving up with a 503.
        if not config.auto_models_cache:
            start_wait = time.time()
            while not config.auto_models_cache and (time.time() - start_wait) < 5.0:
                logger.warning(f"[*] REQ: {model_name} | Cache empty, waiting for background update...")
                await asyncio.sleep(1.5)
                
        if not config.auto_models_cache:
            logger.error(f"[*] REQ: {model_name} | FAILED: Model cache still empty after waiting. Sending 503.")
            return _make_error_response("Auto-routing cache is currently empty. Please wait a few seconds for initialization.", 503)
        
        # Check if providers have default_model configured
        # If so, use those; otherwise fall back to auto_models_cache
        providers_with_default = []
        for provider_name, p_cfg in config.providers.items():
            if p_cfg.default_model:
                providers_with_default.append(provider_name)
        
        if providers_with_default:
            # Use default models from providers
            logger.info(f"[*] REQ: {model_name} | Using provider default models: {providers_with_default}")
            for provider_name in providers_with_default:
                p_cfg = config.providers[provider_name]
                models_to_try.append((provider_name, p_cfg.default_model))
        else:
            # Fall back to auto_models_cache
            for m in config.auto_models_cache:
                models_to_try.append((m.get("_gateway_provider", config.default_provider), m["id"]))
    else:
        p_cfg, actual = config.resolve_model(model_name)
        if not p_cfg: return _make_error_response(f"Model {model_name} not found", 404)
        models_to_try.append((p_cfg.name, actual))

    max_retries = config.settings.max_retries
    errors, last_err = {}, None
    
    # 获取有效提供商总数，用于计算是否全部熔断
    active_providers = [p for p in config.providers.values() if p.api_keys]
    
    # Track platform failure state in this request
    platform_tracking = {}
    for p_name, _ in models_to_try:
        platform_tracking[p_name] = platform_tracking.get(p_name, 0) + 1
    platform_failures = {} # p_name -> failed_count

    for p_name, actual_id in models_to_try:
        p_cfg = config.providers.get(p_name)
        if not p_cfg or not p_cfg.api_keys:
            continue

        # 1. Check Circuit Breakers
        if p_cfg.is_platform_melted():
            continue
        if p_cfg.is_model_melted(actual_id):
            continue

        provider = _get_provider(p_name)
        provider_attempt = errors.get(p_name, 0) + 1
        
        if provider_attempt > max_retries:
            continue
        
        # Protocol Fallback Check
        should_stream = request.stream
        if should_stream and p_cfg.is_stream_melted(actual_id):
            logger.info(f"Streaming is cooling down for {p_name}/{actual_id}. Forcing fallback.")
            should_stream = False

        # Use tiered timeout based on model size
        probe_timeout = _get_probe_timeout(actual_id)

        try:
            if should_stream:
                # 初始化生成器（首包超时检测已移到 provider 内部）
                gen = provider.chat_completion_stream(request, actual_id, timeout=probe_timeout)
                
                # 先获取第一个 chunk，确保 provider 有可用的 key
                # 如果所有 key 都失败，这里会抛出异常，循环继续到下一个 provider
                first_chunk = None
                try:
                    first_chunk = await gen.__anext__()
                except StopAsyncIteration:
                    raise Exception(f"Empty response from {p_name}/{actual_id}")
                except Exception as e:
                    logger.error(f"[ERROR] Failed to get first chunk from {p_name}/{actual_id}: {e}")
                    raise
                
                # Success: Return the stream wrapped in Standard SSE headers
                async def final_gen():
                    logger.info(f"[*] Starting final stream for {actual_id}")
                    logger.info(f"Sending first chunk: {first_chunk[:100]}..." if len(first_chunk) > 100 else f"Sending first chunk: {first_chunk}")
                    yield first_chunk
                    await asyncio.sleep(0.01)
                    
                    try:
                        async for chunk in _stream_with_heartbeat(gen, actual_id, fastapi_request):
                            # 检查客户端是否断开
                            if await fastapi_request.is_disconnected():
                                logger.warning(f"[DISCONNECT] Client disconnected, canceling upstream for {p_name}/{actual_id}")
                                try:
                                    await gen.aclose()
                                except: pass
                                break
                            
                            yield chunk 
                            await asyncio.sleep(0.01)
                    except Exception as e:
                        logger.error(f"[ERROR] Stream relay error for {p_name}/{actual_id}: {e}")
                        try:
                            await gen.aclose()
                        except: pass
                        raise

                logger.info(f"[*] Returning StreamingResponse for {actual_id}")
                return StreamingResponse(
                    final_gen(), 
                    media_type="text/event-stream",
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                        "Access-Control-Allow-Origin": "*",
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type, Authorization"
                    }
                )
            else:
                api_key, key_idx = p_cfg.get_next_key()
                masked_key = f"{api_key[:6]}...{api_key[-4:]}" if api_key and len(api_key) > 10 else "no-key"
                logger.info(f"[*] REQ: {model_name} | Model: {actual_id} | API Key: {masked_key} | Stream: {request.stream} | Provider: {p_name}")
                
                resp = await provider.chat_completion(request, actual_id, timeout=config.settings.unified_timeout or 30)
                processed = _process_non_stream_response(resp)
                logger.info(f"[RESP] {p_name}/{actual_id} success (Batch Mode)")
                
                # Check for tool calls in response
                response_content = processed.choices[0].message.content if processed.choices and processed.choices[0].message else ""
                tool_results = await _process_tool_calls(response_content, p_name, actual_id, request)
                
                if tool_results:
                    # Tool calls were executed, return results
                    tool_result_text = "\n\n工具执行结果:\n"
                    for result in tool_results:
                        tool_result_text += f"```\n{result['content']}\n```\n\n"
                    
                    # Update response with tool results
                    processed.choices[0].message.content = tool_result_text
                    processed.choices[0].finish_reason = "tool_calls"
                
                if request.stream:
                    async def mock_gen():
                        status_chunk = {
                            "id": f"status-{uuid.uuid4().hex[:12]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": actual_id,
                            "choices": [{"index": 0, "delta": {"content": "\n\n[系统提示] 处理完成，正在返回结果...\n\n"}, "finish_reason": None}]
                        }
                        status_chunk_str = f"data: {json.dumps(status_chunk, ensure_ascii=False)}\n\n"
                        yield status_chunk_str
                        
                        async for c in _mock_stream_from_response(processed):
                            yield c
                            await asyncio.sleep(0.05)
                    return StreamingResponse(
                        mock_gen(), 
                        media_type="text/event-stream",
                        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
                    )
                return JSONResponse(content=processed.model_dump(exclude_none=True))

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                logger.warning(f"Bad Request (400) from {p_name}: {e.response.text}")
                return _make_error_response(f"Upstream Bad Request: {e.response.text}", 400, type="invalid_request_error")
            last_err = f"HTTP {e.response.status_code}"
            errors[p_name] = provider_attempt
            platform_failures[p_name] = platform_failures.get(p_name, 0) + 1
            logger.warning(f"[*] REQ: {model_name} | Attempt failed: {p_name}/{actual_id} -> {last_err}")
            if platform_failures[p_name] >= platform_tracking[p_name]:
                c_dur = config.settings.cooldown.provider_melt
                logger.error(f"Platform {p_name} exhausted all models. Melting for {c_dur}s.")
                p_cfg.record_platform_failure(c_dur)
            continue
        except (httpx.ConnectError, httpx.ConnectTimeout, asyncio.TimeoutError) as e:
            logger.warning(f"[RETRY] Connection failed for {p_name}/{actual_id}: {e}. Allowing retry.")
            last_err = str(e) or repr(e)
            errors[p_name] = provider_attempt
            platform_failures[p_name] = platform_failures.get(p_name, 0) + 1
            logger.warning(f"[*] REQ: {model_name} | Attempt failed: {p_name}/{actual_id} -> {last_err}")
            if platform_failures[p_name] >= platform_tracking[p_name]:
                c_dur = config.settings.cooldown.provider_melt
                logger.error(f"Platform {p_name} exhausted all models. Melting for {c_dur}s.")
                p_cfg.record_platform_failure(c_dur)
            continue
        except Exception as e:
            err_msg = str(e) or repr(e)
            # Check for Bad Request errors that have been wrapped in Exception
            if "Bad Request" in err_msg:
                logger.warning(f"Bad Request from {p_name}: {err_msg}")
                return _make_error_response(err_msg, 400, type="invalid_request_error")
            logger.error(f"[ERROR] Request failed for {p_name}/{actual_id}: {err_msg}")
            last_err = err_msg
            errors[p_name] = provider_attempt
            platform_failures[p_name] = platform_failures.get(p_name, 0) + 1
            logger.warning(f"[*] REQ: {model_name} | Attempt failed: {p_name}/{actual_id} -> {last_err}")
            if platform_failures[p_name] >= platform_tracking[p_name]:
                c_dur = config.settings.cooldown.provider_melt
                logger.error(f"Platform {p_name} exhausted all models. Melting for {c_dur}s.")
                p_cfg.record_platform_failure(c_dur)
            continue

    # Fallback response if everything failed
    error_msg = "⚠️ 系统提示：服务由于连续错误已全部触发熔断冷却，请稍后再试（约5分钟）。"
    if not active_providers:
        error_msg = "⚠️ 系统提示：目前没有已配置可用的服务商 API Key。"

    logger.warning(f"All providers melted or unavailable. Returning synthesized message.")

    if request.stream:
        async def mock_stream_error():
            chunk_1 = {"id": "error", "object": "chat.completion.chunk", "created": int(time.time()), "model": model_name, "choices": [{"index": 0, "delta": {"role": "assistant", "content": error_msg}, "finish_reason": None}]}
            chunk_2 = {"id": "error", "object": "chat.completion.chunk", "created": int(time.time()), "model": model_name, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(chunk_1, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(chunk_2, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(mock_stream_error(), media_type="text/event-stream")
    else:
        mock_resp = {
            "id": "error", "object": "chat.completion", "created": int(time.time()), "model": model_name,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": error_msg}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }
        return JSONResponse(content=mock_resp)

@router.get("/models", response_model=ModelListResponse)
async def list_models(mode: str = "all"):
    from app.scoring import rank_models
    scored = rank_models(config.auto_models_cache, only_free=(mode == "free"))
    return ModelListResponse(data=[
        ModelInfo(
            id=str(m["id"]), created=int(time.time()), 
            owned_by=f"{m.get('_gateway_provider','unknown')} | Score: {m.get('_score',0):.3f}",
            score=m.get("_score", 0.0), context_length=m.get("context_length"),
            capabilities=m.get("supported_parameters") or m.get("capabilities")
        )
        for m in scored if isinstance(m, dict) and m.get("id")
    ])

@router.get("/models/{model_id:path}")
async def get_model(model_id: str):
    if model_id.startswith("{"): model_id = model_id[1:-1]
    for m in config.auto_models_cache:
        if m.get("id") == model_id:
            return ModelInfo(
                id=str(m["id"]), created=int(time.time()), owned_by=m.get("_gateway_provider","cache"),
                score=m.get("_score", 0.0), context_length=m.get("context_length"),
                capabilities=m.get("supported_parameters") or m.get("capabilities")
            ).model_dump()
    return _make_error_response("Not found", 404)
