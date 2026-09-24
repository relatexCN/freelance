import time
import json
import logging
from typing import List, Optional
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.models import ChatCompletionRequest, ChatMessage, ChatCompletionResponse
from app.config import config
from app.core.observability import log_event
from app.utils.tool_converter import has_xml_tool_call, xml_to_tool_calls
from app.manager_agent.tools.executor import ToolExecutor, format_tool_result
from app.utils.responses import make_error_response
from app.services.router import RouterService, get_provider_instance
from app.services.streamer import StreamerService
from app.services.gateway_errors import GatewayError
from app.services.stream_fallback import can_fallback

logger = logging.getLogger("freelance.agent")

class AgentService:
    def __init__(self, fastapi_request: Request):
        self.fastapi_request = fastapi_request
        self.executor = ToolExecutor()

    def _tool_mode(self) -> str:
        return config.settings.tools.normalized_mode()

    async def _execute_tool_calls(self, tool_calls: List[dict]) -> List[dict]:
        results = []
        for tc in tool_calls:
            function = tc.get("function") or {}
            name = function.get("name")
            tc_id = tc.get("id")
            if not name or not tc_id:
                continue
            try:
                raw_args = function.get("arguments") or "{}"
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(args, dict):
                    args = {"value": args}
                res = await self.executor.execute(name, **args)
                results.append(format_tool_result(tc_id, res))
            except Exception as e:
                results.append({
                    "tool_call_id": tc_id,
                    "role": "tool",
                    "content": json.dumps({"success": False, "error": str(e)})
                })
        return results

    async def _process_xml_tool_calls(self, content: str) -> List[dict]:
        if not has_xml_tool_call(content):
            logger.debug(f'[TOOL CALL] No XML tool call detected')
            return []
        
        tool_calls = xml_to_tool_calls(content)
        if not tool_calls:
            logger.debug(f'[TOOL CALL] XML detected but no tool calls parsed')
            return []

        return await self._execute_tool_calls(tool_calls)

    async def _process_native_tool_calls(self, tool_calls: Optional[List[dict]]) -> List[dict]:
        if not tool_calls:
            return []
        logger.info(f"[TOOL CALL] Executing {len(tool_calls)} native tool call(s)")
        return await self._execute_tool_calls(tool_calls)

    def _append_assistant_message(self, request: ChatCompletionRequest, message):
        request.messages.append(ChatMessage(
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
        ))

    async def run(self, request: ChatCompletionRequest):
        if self._tool_mode() == 'local_agent':
            if request.n not in (None, 1):
                return make_error_response('Local agent supports n=1 only', 400,
                                           code='local_multiple_choices')
            # Local tool rounds mutate messages; never reuse the initial raw body.
            object.__setattr__(request, '_gateway_normalized_body', True)
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

        if request.stream and self._tool_mode() != "local_agent":
            from app.services.stream_fallback import stream_candidates
            # The requested model remains first; configured defaults provide fallbacks.
            gen = stream_candidates(request, self.fastapi_request, models_to_try)
            relay = StreamerService.stream_with_heartbeat(
                gen, 'routing', model_name, self.fastapi_request, initial_chunks=1,
                monitor_read_timeout=False)
            return StreamingResponse(relay, media_type="text/event-stream")

        max_iterations = getattr(config.settings, "max_iterations", 3)
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            logger.info(f"[*] Agent Turn {iteration} for {model_name}")
            
            found_turn_response = False
            tried_details = []
            last_err = None
            last_exception = None
            
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
                    log_event(logger, "provider_selected", provider=p_name, model=actual_id,
                              iteration=iteration, stream=request.stream)
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
                        
                        message = resp.choices[0].message
                        content = message.content or ""
                        tool_results = []
                        if self._tool_mode() == "local_agent":
                            if iteration >= max_iterations and (message.tool_calls or has_xml_tool_call(content)):
                                return make_error_response('Local agent iteration limit reached', 500,
                                                           code='agent_iteration_limit')
                            if not message.tool_calls and has_xml_tool_call(content):
                                from app.utils.tool_converter import extract_xml_block
                                converted = xml_to_tool_calls(content)
                                if not converted:
                                    return make_error_response('Invalid XML tool calls', 502, code='invalid_tool_calls')
                                before, _, after = extract_xml_block(content)
                                message.tool_calls = converted
                                message.content = (before or '') + (after or '')
                            tool_results = await self._process_native_tool_calls(message.tool_calls)

                        if tool_results and iteration < max_iterations:
                            self._append_assistant_message(request, message)
                            for tr in tool_results:
                                request.messages.append(ChatMessage(**tr))
                            found_turn_response = True
                            break
                        else:
                            # In local-agent mode, hide XML tool markup from final user-facing content.
                            if self._tool_mode() == "local_agent" and has_xml_tool_call(content):
                                from app.utils.tool_converter import extract_xml_block
                                text_before, _, text_after = extract_xml_block(content)
                                final_content = (text_before or "") + (text_after or "")
                                resp.choices[0].message.content = final_content.strip()
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
                status_code = 503 if not last_err else 502
                if (
                    isinstance(last_exception, GatewayError)
                    and last_exception.status_code is not None
                    and 400 <= last_exception.status_code <= 599
                ):
                    status_code = last_exception.status_code
                return make_error_response(err_msg, status_code)
                
        return make_error_response(f"Agent exceeded max iterations ({max_iterations})", 500)

    async def _handle_stream(self, request, provider, p_name, actual_id, iteration, max_iterations):
        from app.services.local_agent_stream import local_agent_stream
        gen = local_agent_stream(self, request, provider, p_name, actual_id, max_iterations)
        return StreamingResponse(StreamerService.stream_with_heartbeat(
            gen, p_name, actual_id, self.fastapi_request, initial_chunks=1,
            monitor_read_timeout=False), media_type='text/event-stream')
