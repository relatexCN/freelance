"""Own candidate selection for the entire stream, not just its first packet."""
import asyncio
import json
import logging
import time
import uuid

import httpx

from app.config import config
from app.core.observability import log_event
from app.core.response_audit import ResponseAudit
from app.services.gateway_errors import GatewayError
from app.services.router import RouterService, get_provider_instance
from app.services.streamer import StreamerService
from app.services.stats_service import stats_service

logger = logging.getLogger('freelance.stream_fallback')


def can_fallback(error):
    return (error.status_code in {408, 429, 500, 502, 503, 504, 524}
            or error.error_code in {'timeout_network', 'first_chunk_timeout', 'stream_incomplete',
                                   'upstream_stream_interrupted', 'stream_idle_timeout',
                                   'first_output_timeout', 'candidate_first_chunk_timeout',
                                   'candidate_idle_timeout'})


async def stream_candidates(request, http_request, candidates):
    response_id = 'chatcmpl-' + uuid.uuid4().hex
    created = int(time.time())
    model = request.model
    switched = False
    failed = False
    tried = set()
    attempted_count = 0
    skipped_count = 0
    failures = []

    def packet(content):
        return 'data: ' + json.dumps({'id': response_id, 'object': 'chat.completion.chunk',
            'created': created, 'model': model, '_gateway_notice': True, 'choices': [
                {'index': 0, 'delta': {'content': content}, 'finish_reason': None}]}, ensure_ascii=False) + '\n\n'

    def terminal(code, message, **details):
        return 'data: ' + json.dumps({'error': {'code': code, 'message': message, **details}}, ensure_ascii=False) + '\n\n'

    for provider_name, actual_model in candidates:
        if (provider_name, actual_model) in tried:
            continue
        tried.add((provider_name, actual_model))
        if await http_request.is_disconnected():
            return
        if not RouterService.is_provider_available(provider_name, actual_model):
            skipped_count += 1
            log_event(logger, 'fallback_candidate_skipped', provider=provider_name, model=actual_model,
                      reason='unavailable',
                      retry_after_seconds=RouterService.candidate_retry_after(provider_name, actual_model))
            continue
        provider = get_provider_instance(provider_name)
        if provider is None:
            skipped_count += 1
            continue
        attempted_count += 1
        if failed:
            switched = True
            # Do not repeat untrusted provider errors or credentials in the notice.
            yield packet('\n\n【当前模型调用失败，切换新模型】\n'
                         '以下由备用模型重新回答；前面的未完成内容仅供参考。\n\n')
            log_event(logger, 'stream_model_switch', provider=provider_name, model=actual_model)
        log_event(logger, 'provider_selected', provider=provider_name, model=actual_model,
                  iteration=1, stream=True)
        probe = StreamerService.get_probe_timeout(actual_model)
        gen = provider.chat_completion_stream(request, actual_model, timeout=probe)
        first = True
        tools_sent = False
        output_seen = False
        output_timeout = config.settings.reliability.first_output_timeout_seconds
        pending = []
        pending_size = 0
        error = None
        started = time.monotonic()
        candidate_audit = ResponseAudit()
        candidate_state = dict(provider=provider_name, model=actual_model, started=started, active=True)
        if hasattr(http_request, 'scope'):
            http_request.scope['_gateway_candidate'] = candidate_state
        try:
            while True:
                timeout = (StreamerService.get_candidate_probe_timeout(
                    actual_model, len(getattr(getattr(provider, 'config', None), 'api_keys', [])))
                    if first else StreamerService.get_read_timeout(actual_model))
                output_limited = False
                if output_timeout and not output_seen:
                    remaining = output_timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise GatewayError('No answer output within candidate budget',
                                           error_code='first_output_timeout')
                    output_limited = remaining <= timeout
                    timeout = min(timeout, remaining)
                try:
                    chunk = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
                except asyncio.TimeoutError:
                    if output_limited:
                        raise GatewayError('No answer output within candidate budget',
                                           error_code='first_output_timeout') from None
                    raise GatewayError('Candidate stream wait timed out',
                                       error_code='candidate_first_chunk_timeout' if first else 'candidate_idle_timeout',
                                       timeout_phase='candidate_first_chunk' if first else 'candidate_idle',
                                       exception_type='TimeoutError') from None
                if first:
                    log_event(logger, 'first_chunk_received', provider=provider_name, model=actual_model,
                              elapsed_ms=round((time.monotonic() - started) * 1000, 3))
                    first = False
                if not chunk.startswith('data:'):
                    yield chunk
                    continue
                raw = chunk[5:].strip()
                if raw == '[DONE]':
                    if not pending or not output_seen:
                        raise GatewayError('Stream ended without a finish reason', error_code='stream_incomplete')
                    for ending in pending:
                        yield ending
                    if hasattr(provider, 'config'):
                        provider.config.reset_consecutive_failures()
                        provider.config.record_model_success(actual_model)
                    candidate_audit.done = True
                    stats_service.record_request(provider_name, actual_model,
                                                 latency_ms=(time.monotonic() - started) * 1000)
                    yield 'data: [DONE]\n\n'
                    return
                data = json.loads(raw)
                # This extension is gateway-owned, never trust an upstream marker.
                if '_gateway_notice' in data:
                    data.pop('_gateway_notice')
                    chunk = 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
                candidate_audit.inspect(data)
                if data.get('error'):
                    upstream_error = data['error']
                    status = upstream_error.get('code')
                    try:
                        status_code = int(status)
                    except (TypeError, ValueError):
                        status_code = None
                    raise GatewayError('Upstream stream failed', status_code=status_code,
                                       error_code=str(status or 'upstream_error'))
                if switched:
                    data.update(id=response_id, created=created, model=model)
                    chunk = 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'
                else:
                    response_id = data.get('id', response_id)
                    created = data.get('created', created)
                    model = data.get('model', model)
                for choice in data.get('choices') or []:
                    delta = choice.get('delta') or {}
                    content = delta.get('content')
                    output_seen |= bool((isinstance(content, str) and content.strip())
                                        or delta.get('tool_calls') or delta.get('function_call'))
                if pending or any(choice.get('finish_reason') for choice in data.get('choices') or []):
                    # A failed candidate must not send a terminal finish/usage frame.
                    pending_size += len(chunk)
                    if pending_size > 262144 or len(pending) >= 32:
                        raise GatewayError('Too many terminal frames', error_code='stream_incomplete')
                    pending.append(chunk)
                else:
                    for choice in data.get('choices') or []:
                        delta = choice.get('delta') or {}
                        if delta.get('tool_calls') or delta.get('function_call'):
                            tools_sent = True
                    yield chunk
        except StopAsyncIteration:
            error = GatewayError('Upstream ended unexpectedly', error_code='stream_incomplete')
        except (asyncio.TimeoutError, httpx.TransportError):
            error = GatewayError('Upstream stream timeout or connection failure', error_code='timeout_network')
        except GatewayError as exc:
            error = exc
        except (ValueError, TypeError, AttributeError):
            error = GatewayError('Invalid upstream stream data', error_code='invalid_stream')
        except Exception:
            error = GatewayError('Upstream stream failed', error_code='upstream_error')
        finally:
            try:
                await gen.aclose()
            finally:
                candidate_state['active'] = False
                candidate_audit.error |= error is not None
                log_event(logger, 'stream_candidate_summary', provider=provider_name, model=actual_model,
                          error_code=error.error_code if error else None,
                          elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                          **candidate_audit.summary(True, None, candidate_audit.done))
        log_event(logger, 'stream_candidate_failed', provider=provider_name, model=actual_model,
                  error_code=error.error_code, status_code=error.status_code, tools_sent=tools_sent,
                  exception_type=error.exception_type, timeout_phase=error.timeout_phase,
                  elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                  first_output_seen=output_seen)
        RouterService.record_failure(provider_name, actual_model, error)
        failures.append({'provider': provider_name, 'model': actual_model,
                         'code': error.error_code, 'timeout_phase': error.timeout_phase})
        if tools_sent:
            yield terminal('fallback_blocked_after_tool_call',
                           '模型调用失败；已发送工具调用，为避免重复执行，未切换模型。')
            yield 'data: [DONE]\n\n'
            return
        if not can_fallback(error):
            yield terminal(error.error_code, '模型调用失败，此错误不允许自动切换模型。')
            yield 'data: [DONE]\n\n'
            return
        failed = True
    waits = [RouterService.candidate_retry_after(p, m) for p, m in tried]
    known_waits = [wait for wait in waits if wait is not None]
    retry_after = min(known_waits) if known_waits else None
    reason = 'candidates_failed' if attempted_count else 'candidates_unavailable'
    message = ('所有已尝试候选模型均失败，其余候选不可用。' if attempted_count
               else '当前没有可调用的候选模型，未发起上游调用。')
    if retry_after:
        message += f'请至少等待 {retry_after} 秒后重试（不保证上游已恢复）。'
    else:
        message += '请检查上游连接及模型配置。'
    details = dict(reason=reason, attempted_count=attempted_count, skipped_count=skipped_count,
                   candidate_errors=failures)
    if retry_after is not None:
        details['retry_after_seconds'] = retry_after
    log_event(logger, 'stream_candidates_exhausted', **details)
    yield terminal('all_stream_candidates_failed', message, **details)
    yield 'data: [DONE]\n\n'
