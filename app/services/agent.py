import asyncio
import time
import json
import uuid
import logging
from typing import List, Any, Optional, AsyncGenerator
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.models import ChatCompletionRequest, ChatMessage, ChatCompletionResponse
from app.config import config
from app.utils.tool_converter import has_xml_tool_call, xml_to_tool_calls
from app.utils.tool_executor import ToolExecutor, format_tool_result
from app.utils.responses import make_error_response
from app.services.router import RouterService, get_provider_instance
from app.services.streamer import StreamerService

logger = logging.getLogger("freelance.agent")

class AgentService:
    def __init__(self, fastapi_request: Request):
        self.fastapi_request = fastapi_request
        self.executor = ToolExecutor()

    async def _process_tool_calls(self, content: str) -> List[dict]:
        if not has_xml_tool_call(content):
            return []
        
        tool_calls = xml_to_tool_calls(content)
        if not tool_calls:
            return []
        
        results = []
        for tc in tool_calls:
            name = tc["function"]["name"]
            tc_id = tc["id"]
            try:
                args = json.loads(tc["function"]["arguments"])
                res = await self.executor.execute(name, **args)
                results.append(format_tool_result(tc_id, res))
            except Exception as e:
                results.append({
                    "tool_call_id": tc_id,
                    "role": "tool",
                    "content": json.dumps({"success": False, "error": str(e)})
                })
        return results

    async def run(self, request: ChatCompletionRequest):
        model_name = (request.model or "auto").lower()
        models_to_try = await RouterService.get_models_to_try(model_name)
        
        if not models_to_try:
            return make_error_response("No available models found", 404)

        max_iterations = getattr(config.settings, "max_iterations", 3)
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            logger.info(f"[*] Agent Turn {iteration} for {model_name}")
            
            found_turn_response = False
            last_err = None
            
            # Inner loop: Try each provider for the current turn
            for p_name, actual_id in models_to_try:
                if not RouterService.is_provider_available(p_name, actual_id):
                    continue
                
                provider = get_provider_instance(p_name)
                p_cfg = config.providers.get(p_name)
                
                try:
                    if request.stream:
                        return await self._handle_stream(request, provider, p_name, actual_id, iteration, max_iterations)
                    else:
                        from app.services.stats_service import stats_service
                        start_time = time.time()
                        resp = await provider.chat_completion(request, actual_id, timeout=config.settings.unified_timeout or 30)
                        latency = (time.time() - start_time) * 1000
                        stats_service.record_request(p_name, actual_id, latency_ms=latency)
                        
                        content = resp.choices[0].message.content or ""
                        tool_results = await self._process_tool_calls(content)
                        
                        if tool_results and iteration < max_iterations:
                            request.messages.append(resp.choices[0].message)
                            for tr in tool_results:
                                request.messages.append(ChatMessage(**tr))
                            found_turn_response = True
                            break
                        else:
                            return JSONResponse(content=resp.model_dump(exclude_none=True))
                            
                except Exception as e:
                    last_err = str(e)
                    logger.warning(f"Turn {iteration} failed on {p_name}/{actual_id}: {e}")
                    RouterService.record_failure(p_name, actual_id, e)
                    continue
            
            if not found_turn_response:
                break
                
        return make_error_response(f"Agent failed to produce response: {last_err}", 500)

    async def _handle_stream(self, request, provider, p_name, actual_id, iteration, max_iterations):
        probe_timeout = StreamerService.get_probe_timeout(actual_id)
        gen = provider.chat_completion_stream(request, actual_id, timeout=probe_timeout)
        
        try:
            first_chunk = await gen.__anext__()
        except Exception as e:
            raise Exception(f"Failed to get first chunk: {e}")

        async def agent_gen(current_gen, current_id, current_first, current_iter):
            full_content = ""
            
            # Helper to extract content from chunk
            def extract(c):
                if c.startswith("data: ") and not " [DONE]" in c:
                    try:
                        d = json.loads(c[6:])
                        return d["choices"][0]["delta"].get("content", "")
                    except: return ""
                return ""

            full_content += extract(current_first)
            yield current_first
            
            async for chunk in StreamerService.stream_with_heartbeat(current_gen, p_name, current_id, self.fastapi_request):
                if await self.fastapi_request.is_disconnected(): break
                full_content += extract(chunk)
                if not " [DONE]" in chunk:
                    yield chunk
            
            # Turn continuation check
            if has_xml_tool_call(full_content) and current_iter < max_iterations:
                # yield status
                status = {
                    "id": f"agent-{uuid.uuid4().hex[:8]}",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"content": "\n\n> [系统] 正在处理工具指令...\n"}, "finish_reason": None}]
                }
                yield f"data: {json.dumps(status)}\n\n"
                
                tool_results = await self._process_tool_calls(full_content)
                if tool_results:
                    request.messages.append(ChatMessage(role="assistant", content=full_content))
                    for tr in tool_results:
                        request.messages.append(ChatMessage(**tr))
                    
                    # Next turn stream
                    next_gen = provider.chat_completion_stream(request, current_id, timeout=probe_timeout)
                    try:
                        next_first = await next_gen.__anext__()
                        async for c in agent_gen(next_gen, current_id, next_first, current_iter + 1):
                            yield c
                        return
                    except: pass
            
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            agent_gen(gen, actual_id, first_chunk, iteration),
            media_type="text/event-stream"
        )
