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
from app.services.gateway_errors import GatewayError

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
            last_err = None
            
            # Inner loop: Try each provider for the current turn
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
                    if request.stream:
                        return await self._handle_stream(request, provider, p_name, actual_id, iteration, max_iterations)
                    else:
                        from app.services.stats_service import stats_service
                        start_time = time.time()
                        resp = await provider.chat_completion(request, actual_id, timeout=config.settings.unified_timeout or 30)
                        latency = (time.time() - start_time) * 1000
                        stats_service.record_request(p_name, actual_id, latency_ms=latency)
                        p_cfg = config.providers.get(p_name)
                        if p_cfg:
                            p_cfg.reset_consecutive_failures()
                        
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
                    RouterService.record_failure(p_name, actual_id, e)
                    if isinstance(e, GatewayError) and e.skip_retry:
                        status_code = e.status_code if e.status_code is not None else 500
                        return make_error_response(
                            last_err,
                            status_code=status_code,
                            type=e.error_type,
                            code=e.error_code,
                        )
                    continue
            
            if not found_turn_response:
                # If we've exhausted all providers this turn, return a detailed error
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
                err_msg = f"All providers failed for turn {iteration}. Details: {details_str}"
                return make_error_response(err_msg, 503 if not last_err else 500)
                
        return make_error_response(f"Agent exceeded max iterations ({max_iterations})", 500)

    async def _handle_stream(self, request, provider, p_name, actual_id, iteration, max_iterations):
        probe_timeout = StreamerService.get_probe_timeout(actual_id)
        gen = provider.chat_completion_stream(request, actual_id, timeout=probe_timeout)
        
        try:
            first_chunk = await gen.__anext__()
        except Exception as e:
            raise Exception(f"Failed to get first chunk: {e}")

        p_cfg = config.providers.get(p_name)
        if p_cfg:
            p_cfg.reset_consecutive_failures()

        async def agent_gen(current_gen, current_id, current_first, current_iter):
            full_content = ""
            is_suppressing = False
            tag_start_str = "<function_calls"
            tag_end_str = "</function_calls>"
            
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

            # 1. Process the very first chunk from the provider
            delta, is_data, is_error = extract_info(current_first)
            full_content += delta
            
            if is_data and tag_start_str in delta:
                # Rare: Tag in the first chunk. Enter suppression.
                is_suppressing = True
                pre_tag = delta.split(tag_start_str)[0]
                if pre_tag:
                    yield make_synthetic_chunk(pre_tag, current_first)
            else:
                # Normal: Relay first chunk as-is to establish ID/Role
                yield current_first

            # 2. Main relay loop
            pending_buffer = ""
            async for chunk in StreamerService.stream_with_heartbeat(current_gen, p_name, current_id, self.fastapi_request):
                if await self.fastapi_request.is_disconnected(): break
                
                # Relay non-data pings immediately
                if not chunk.startswith("data: ") or "[DONE]" in chunk:
                    yield chunk
                    continue
                
                delta, is_data, is_error = extract_info(chunk)
                if is_error:
                    # CRITICAL: Relay error chunks directly so client knows why it failed
                    yield chunk
                    continue
                
                if not is_suppressing:
                    # Lookahead logic: Check if current stream is heading towards a tag
                    potential_new = pending_buffer + delta
                    
                    if tag_start_str in potential_new:
                        # TAG DETECTED!
                        is_suppressing = True
                        tag_start_idx = potential_new.find(tag_start_str)
                        
                        # Yield everything before the tag
                        pre_tag_text = potential_new[:tag_start_idx]
                        if pre_tag_text:
                            yield make_synthetic_chunk(pre_tag_text, chunk)
                        
                        # Full content keeps the tag for internal processing
                        full_content += delta
                        pending_buffer = "" # Clear buffer as we've entered suppression mode
                    elif any(tag_start_str.startswith(potential_new[i:]) for i in range(len(potential_new))):
                        # POTENTIAL TAG START: Keep in buffer, don't yield yet
                        pending_buffer = potential_new
                    else:
                        # SAFE: No tag detected or possible. Flush buffer + current delta.
                        if pending_buffer:
                            yield make_synthetic_chunk(pending_buffer, chunk)
                            pending_buffer = ""
                        yield chunk
                        full_content += delta
                else:
                    # SUPPRESSING: Just accumulate for internal agent logic
                    full_content += delta
                    if tag_end_str in full_content:
                        is_suppressing = False
                        tag_end_idx = full_content.find(tag_end_str) + len(tag_end_str)
                        # Note: We don't yield post-tag content here immediately as it might 
                        # contain ANOTHER tag start. The next iteration will handle it.
                        # For now, just reset is_suppressing and let the next chunk flush.
            
            # 3. Final flush of any pending text that wasn't part of a tag
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
                        logger.error(f'[STREAM] Failed to continue stream after tool call: {e}')
            
            yield f"event: stream_end\ndata: {{\"content_length\": {len(full_content)}}}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            agent_gen(gen, actual_id, first_chunk, iteration),
            media_type="text/event-stream"
        )
