"""Validate each local tool round before execution; emit one terminal response."""
import asyncio
import json
import time
import uuid

from app.models import ChatMessage
from app.services.gateway_errors import GatewayError
from app.services.router import RouterService
from app.services.stats_service import stats_service
from app.services.streamer import StreamerService
from app.utils.tool_converter import has_xml_tool_call, extract_xml_block, xml_to_tool_calls


async def local_agent_stream(agent, request, provider, provider_name, model, max_iterations):
    """Buffer bounded rounds so tool frames and intermediate DONE cannot leak."""
    response_id = 'chatcmpl-' + uuid.uuid4().hex
    for turn in range(max_iterations):
        if await agent.fastapi_request.is_disconnected():
            return
        started = time.monotonic()
        gen = provider.chat_completion_stream(request, model,
                    timeout=StreamerService.get_probe_timeout(model))
        text_parts, calls = [], {}
        done, finish, first, size = False, None, True, 0
        error = None
        try:
            while True:
                timeout = (StreamerService.get_probe_timeout(model) if first
                           else StreamerService.get_read_timeout(model))
                try:
                    chunk = await asyncio.wait_for(anext(gen), timeout)
                except asyncio.TimeoutError:
                    raise GatewayError('Local stream wait timed out', error_code=(
                        'candidate_first_chunk_timeout' if first else 'candidate_idle_timeout')) from None
                first = False
                size += len(chunk.encode('utf-8'))
                if size > 1048576:
                    raise GatewayError('Local tool round exceeds buffer limit', error_code='local_round_too_large')
                if await agent.fastapi_request.is_disconnected():
                    return
                if not chunk.startswith('data:'):
                    continue
                raw = chunk[5:].strip()
                if raw == '[DONE]':
                    done = True
                    break
                data = json.loads(raw)
                if data.get('error'):
                    code = str(data['error'].get('code') or 'upstream_error')
                    raise GatewayError('Upstream local tool round failed', error_code=code,
                                       status_code=int(code) if code.isdigit() else None)
                for choice in data.get('choices') or []:
                    if choice.get('index', 0) != 0:
                        raise GatewayError('Local agent supports one choice', error_code='local_multiple_choices')
                    finish = choice.get('finish_reason') or finish
                    delta = choice.get('delta') or {}
                    text_parts.append(delta.get('content') or '')
                    for call in delta.get('tool_calls') or []:
                        index = call.get('index', 0)
                        merged = calls.setdefault(index, {'id': '', 'type': 'function',
                                                         'function': {'name': '', 'arguments': ''}})
                        if call.get('id'):
                            merged['id'] = call['id']
                        for field in ('name', 'arguments'):
                            merged['function'][field] += (call.get('function') or {}).get(field) or ''
            content = ''.join(text_parts)
            if not done or not finish or (not content.strip() and not calls):
                raise GatewayError('Incomplete local tool round', error_code='stream_incomplete')
            if calls and finish != 'tool_calls':
                raise GatewayError('Incomplete tool calls', error_code='invalid_tool_calls')
            if not calls and has_xml_tool_call(content):
                converted = xml_to_tool_calls(content) if finish == 'stop' else []
                if not converted:
                    raise GatewayError('Invalid XML tool calls', error_code='invalid_tool_calls')
                calls = dict(enumerate(converted))
                before, _, after = extract_xml_block(content)
                content = (before or '') + (after or '')
            for call in calls.values():
                if not call['id'] or not call['function']['name']:
                    raise GatewayError('Missing tool identity', error_code='invalid_tool_calls')
                json.loads(call['function']['arguments'] or '{}')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, GatewayError) else GatewayError(
                'Invalid or incomplete local stream', error_code='stream_incomplete')
        finally:
            await gen.aclose()
        if error:
            RouterService.record_failure(provider_name, model, error)
            yield 'data: ' + json.dumps({'error': {'code': error.error_code, 'message': error.message}}) + '\n\n'
            yield 'data: [DONE]\n\n'
            return
        if hasattr(provider, 'config'):
            provider.config.record_model_success(model)
            provider.config.reset_consecutive_failures()
        stats_service.record_request(provider_name, model, latency_ms=(time.monotonic() - started) * 1000)
        wants_tools = bool(calls)
        if wants_tools:
            if turn + 1 >= max_iterations:
                yield 'data: {"error":{"code":"agent_iteration_limit","message":"Local agent iteration limit reached"}}\n\n'
                yield 'data: [DONE]\n\n'
                return
            results = await agent._process_native_tool_calls(list(calls.values()))
            if results:
                request.messages.append(ChatMessage(role='assistant', content=content,
                                                    tool_calls=list(calls.values()) or None))
                request.messages.extend(ChatMessage(**result) for result in results)
                continue
            before, _, after = extract_xml_block(content)
            content = (before or '') + (after or '')
        yield 'data: ' + json.dumps({'id': response_id, 'object': 'chat.completion.chunk',
            'created': int(time.time()), 'model': request.model,
            'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': content},
                         'finish_reason': finish}]}, ensure_ascii=False) + '\n\n'
        yield 'data: [DONE]\n\n'
        return
