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
            logger.debug(f'[TOOL CALL] No XML tool call detected')
            return []
        
        tool_calls = xml_to_tool_calls(content)
        if not tool_calls:
            logger.debug(f'[TOOL CALL] XML detected but no tool calls parsed')
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
            tried_details = []
            skipped_platforms = set()
            last_err = None
            
            # Inner loop: Try each provider for the current turn
            for p_name, actual_id in models_to_try:
                if not RouterService.is_provider_available(p_name, actual_id):
                    # Skip silent if platform is already known to be melted this turn
                    if p_name in skipped_platforms:
                        continue
                        
                    status_msg = RouterService.get_provider_status_message(p_name, actual_id)
                    tried_details.append(f"{status_msg} (Skipped)")
                    
                    # If the whole platform is melted, mark it to avoid repetitive entries for other models
                    p_cfg = config.providers.get(p_name)
                    if p_cfg and p_cfg.is_platform_melted():
                        skipped_platforms.add(p_name)
                    continue
                
                provider = get_provider_instance(p_name)
                if not provider:
                    tried_details.append(f"{p_name} instance not found")
                    continue
                
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
                            # OpenAI Compliance: Strip XML from final content
                            if has_xml_tool_call(content):
                                from app.utils.tool_converter import extract_xml_block
                                text_before, _, text_after = extract_xml_block(content)
                                final_content = (text_before or "") + (text_after or "")
                                resp.choices[0].message.content = final_content.strip()
                            return JSONResponse(content=resp.model_dump(exclude_none=True))
                            
                except Exception as e:
                    last_err = str(e)
                    tried_details.append(f"{p_name}/{actual_id} error: {last_err}")
                    logger.warning(f"Turn {iteration} failed on {p_name}/{actual_id}: {e}")
                    RouterService.record_failure(p_name, actual_id, e)
                    continue
            
            if not found_turn_response:
                # Deduplicate details to avoid massive repetitive messages (backup check)
                unique_details = []
                seen_details = set()
                for d in tried_details:
                    if d not in seen_details:
                        unique_details.append(d)
                        seen_details.add(d)
                
                details_str = "; ".join(unique_details)
                err_msg = f"All providers failed for turn {iteration}. Details: {details_str}"
                return make_error_response(err_msg, 503 if not last_err else 500)
                
        return make_error_response(f"Agent exceeded max iterations ({max_iterations})", 500)

    async def _handle_stream(self, request, provider, p_name, actual_id, iteration, max_iterations):
        probe_timeout = StreamerService.get_probe_timeout(actual_id)
        gen = provider.chat_completion_stream(request, actual_id, timeout=probe_timeout)
        
        try:
            first_chunk = await gen.__anext__()
        except Exception as e:
            # Explicitly close the generator to ensure the upstream connection is severed
            if hasattr(gen, 'aclose'):
                await gen.aclose()
            raise Exception(f"Failed to get first chunk: {e}")

        async def agent_gen(current_gen, current_id, current_first, current_iter):
            full_content = ""
            is_suppressing = False
            tag_start_str = "<function_calls"
            tag_end_str = "</function_calls>"
            pending_buffer = ""
            
            # Metadata from provider (stable ID)
            request_id = None
            
            def extract_info(c):
                nonlocal request_id
                if c.startswith("data: ") and not "[DONE]" in c:
                    try:
                        d = json.loads(c[6:])
                        if "error" in d:
                            return "", False, True # Added is_error flag content, is_data, is_error
                        if "id" in d: request_id = d["id"]
                        choices = d.get("choices")
                        if choices and isinstance(choices, list) and len(choices) > 0:
                            content = choices[0].get("delta", {}).get("content", "")
                            return content if content is not None else "", True, False
                        return "", True, False
                    except: return "", True, False
                return "", False, False

            def make_synthetic_chunk(text, original_chunk_str):
                # If we must split/modify, try to preserve metadata from the original chunk
                if not text: return ""
                try:
                    d = json.loads(original_chunk_str[6:])
                    d["choices"][0]["delta"]["content"] = text
                    return f"data: {json.dumps(d)}\n\n"
                except:
                    # Fallback to a plain but consistent chunk
                    chunk_data = {
                        "id": request_id or f"chatcmpl-{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]
                    }
                    return f"data: {json.dumps(chunk_data)}\n\n"

            async def process_chunk(chunk_str):
                nonlocal full_content, is_suppressing, pending_buffer, request_id
                
                # Skip non-data pings or end-of-stream markers
                if not chunk_str.startswith("data: ") or "[DONE]" in chunk_str:
                    yield chunk_str
                    return
                
                delta, is_data, is_error = extract_info(chunk_str)
                if is_error:
                    yield chunk_str
                    return
                
                # Accumulate RAW content for the model history and internal parsing
                full_content += delta
                
                # Process text with lookahead for tags
                active_text = pending_buffer + delta
                pending_buffer = ""
                
                while active_text:
                    if not is_suppressing:
                        tag_start_idx = active_text.find(tag_start_str)
                        if tag_start_idx != -1:
                            # Start tag located
                            pre_tag = active_text[:tag_start_idx]
                            if pre_tag:
                                yield make_synthetic_chunk(pre_tag, chunk_str)
                            is_suppressing = True
                            active_text = active_text[tag_start_idx:]
                        else:
                            # No full tag start. Check for partial tag start at the tail.
                            found_partial_idx = -1
                            for i in range(len(tag_start_str) - 1, 0, -1):
                                suffix = tag_start_str[:i]
                                if active_text.endswith(suffix):
                                    found_partial_idx = len(active_text) - i
                                    break
                            
                            if found_partial_idx != -1:
                                pending_buffer = active_text[found_partial_idx:]
                                safe_text = active_text[:found_partial_idx]
                                if safe_text:
                                    yield make_synthetic_chunk(safe_text, chunk_str)
                                active_text = "" # Consumed into buffer
                            else:
                                # Entire chunk is safe text
                                if delta == active_text:
                                    yield chunk_str
                                else:
                                    yield make_synthetic_chunk(active_text, chunk_str)
                                active_text = ""
                    else:
                        # Suppression mode active: Look for closing tag
                        tag_end_idx = active_text.find(tag_end_str)
                        if tag_end_idx != -1:
                            is_suppressing = False
                            # Continue processing anything AFTER the closing tag
                            active_text = active_text[tag_end_idx + len(tag_end_str):]
                        else:
                            # Entire chunk is within a suppressed block
                            active_text = ""

            # 1. Process the very first chunk from the provider through the same logic
            async for c in process_chunk(current_first):
                yield c

            # 2. Main relay loop
            async for chunk in StreamerService.stream_with_heartbeat(current_gen, p_name, current_id, self.fastapi_request):
                if await self.fastapi_request.is_disconnected(): break
                async for c in process_chunk(chunk):
                    yield c
            
            # 3. Final flush
            if is_suppressing:
                logger.warning(f'[STREAM] Stream ended while still in suppression mode!')
            if not is_suppressing and pending_buffer:
                yield make_synthetic_chunk(pending_buffer, current_first)
                full_content += pending_buffer
            
            # Turn continuation check
            if has_xml_tool_call(full_content) and current_iter < max_iterations:
                tool_results = await self._process_tool_calls(full_content)
                if tool_results:
                    request.messages.append(ChatMessage(role="assistant", content=full_content))
                    for tr in tool_results:
                        request.messages.append(ChatMessage(**tr))
                    
                    next_gen = provider.chat_completion_stream(request, current_id, timeout=probe_timeout)
                    try:
                        next_first = await next_gen.__anext__()
                        async for c in agent_gen(next_gen, current_id, next_first, current_iter + 1):
                            yield c
                        return
                    except Exception as e:
                        if hasattr(next_gen, 'aclose'):
                            await next_gen.aclose()
                        logger.error(f'[STREAM] Failed to continue stream after tool call: {e}')
            
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            agent_gen(gen, actual_id, first_chunk, iteration),
            media_type="text/event-stream"
        )
