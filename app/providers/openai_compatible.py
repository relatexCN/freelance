"""
OpenAI-compatible provider implementation.
Provides an adapter for OpenRouter, Groq, Cerebras, GitHub Models, and other AI platforms that implement the OpenAI spec.
"""

import json
import logging
import re
import time
import uuid
import asyncio
from typing import Any, AsyncIterator, Optional

import httpx

from app.config import ProviderConfig, config
from app.models.request import ChatCompletionRequest
from app.models.response import (
    ChatChoice,
    ChatCompletionMessage,
    ChatCompletionResponse,
    ChatCompletionStreamResponse,
    DeltaMessage,
    StreamChoice,
    UsageInfo,
)
from app.core.providers.base import BaseProvider
from app.core.observability import log_event
from app.services.gateway_errors import GatewayError


logger = logging.getLogger("freelance.provider")

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_PASSTHROUGH_SKIP_HEADERS = _HOP_BY_HOP_HEADERS | {
    "host",
    "content-length",
    "accept-encoding",
    "authorization",
    "x-api-key",
    "x-goog-api-key",
    "cookie",
}


class OpenAICompatibleProvider(BaseProvider):
    """Provider for any OpenAI-compatible API."""

    def __init__(self, provider_config: ProviderConfig):
        super().__init__(provider_config)
        # 1. Initialize Stream Client (optimized for long-lived SSE)
        stream_pool = config.settings.connection_pool.stream
        self.stream_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=provider_config.timeout, write=20.0, pool=60.0),
            limits=httpx.Limits(
                max_keepalive_connections=stream_pool.max_keepalive, 
                max_connections=stream_pool.max_connections,
                keepalive_expiry=stream_pool.keepalive_expiry
            ),
            # Important for long-lived SSE: set retries to 0
            transport=httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=True
        )
        
        # 2. Initialize Batch Client (optimized for quick requests/retries)
        batch_pool = config.settings.connection_pool.batch
        self.batch_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=provider_config.timeout, write=10.0, pool=30.0),
            limits=httpx.Limits(
                max_keepalive_connections=batch_pool.max_keepalive, 
                max_connections=batch_pool.max_connections,
                keepalive_expiry=batch_pool.keepalive_expiry
            ),
            transport=httpx.AsyncHTTPTransport(retries=2),
            follow_redirects=True
        )
        
        logger.info(f"[*] Provider {provider_config.name} initialized with DUAL connection pools (Stream/Batch)")

    def _build_headers(self, api_key: Optional[str] = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://freelance-gateway.local",
            "X-Title": "Freelance API Gateway",
        }
        
        # If no key provided, get the next one from the pool
        if api_key is None:
            api_key, _ = self.config.get_next_key()
            
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _client_headers(self, request: ChatCompletionRequest) -> dict[str, str]:
        headers = getattr(request, "_gateway_headers", {}) or {}
        return {str(k).lower(): str(v) for k, v in headers.items()}

    def _should_passthrough_header(self, name: str) -> bool:
        lower_name = name.lower()
        if lower_name in _PASSTHROUGH_SKIP_HEADERS:
            return False

        rules = self.config.header_passthrough
        if not rules:
            return False

        for rule in rules:
            rule_s = str(rule).strip()
            if not rule_s:
                continue
            rule_l = rule_s.lower()
            if rule_s == "*":
                return True
            if rule_l.startswith("re:") or rule_l.startswith("regex:"):
                pattern = rule_s.split(":", 1)[1].strip()
                if pattern and re.search(pattern, name, flags=re.IGNORECASE):
                    return True
                continue
            if lower_name == rule_l:
                return True
        return False

    def _resolve_header_value(self, value: Any, api_key: str, client_headers: dict[str, str]) -> Optional[str]:
        text = str(value)
        stripped = text.strip()
        if stripped.startswith("{client_header:") and stripped.endswith("}"):
            header_name = stripped[len("{client_header:"):-1].strip().lower()
            return client_headers.get(header_name)
        return text.replace("{api_key}", api_key or "")

    def _set_header(self, headers: dict[str, str], name: str, value: str):
        lower_name = name.lower()
        for existing in list(headers.keys()):
            if existing.lower() == lower_name and existing != name:
                del headers[existing]
        headers[name] = value

    def _build_request_headers(self, request: ChatCompletionRequest, api_key: str) -> dict[str, str]:
        headers = self._build_headers(api_key)
        client_headers = self._client_headers(request)

        for name, value in client_headers.items():
            if self._should_passthrough_header(name):
                self._set_header(headers, name, value)

        for name, value in self.config.header_overrides.items():
            if not str(name).strip():
                continue
            resolved = self._resolve_header_value(value, api_key, client_headers)
            if resolved is not None and resolved.strip():
                self._set_header(headers, str(name), resolved)
        return headers

    def _delete_path(self, data: Any, path: str):
        parts = [p for p in str(path).split(".") if p]
        if not parts:
            return
        current = data
        for part in parts[:-1]:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                idx = int(part)
                current = current[idx] if 0 <= idx < len(current) else None
            else:
                return
            if current is None:
                return
        last = parts[-1]
        if isinstance(current, dict):
            current.pop(last, None)
        elif isinstance(current, list) and last.isdigit():
            idx = int(last)
            if 0 <= idx < len(current):
                current.pop(idx)

    def _set_path(self, data: Any, path: str, value: Any):
        parts = [p for p in str(path).split(".") if p]
        if not parts or not isinstance(data, dict):
            return
        current = data
        for part in parts[:-1]:
            if not isinstance(current, dict):
                return
            if part not in current or not isinstance(current[part], dict):
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    def _apply_body_rules(self, payload: dict[str, Any]) -> dict[str, Any]:
        if config.settings.tools.normalized_mode() == "disabled":
            for field in ("tools", "tool_choice", "parallel_tool_calls"):
                payload.pop(field, None)

        for field in self.config.disabled_fields:
            self._delete_path(payload, str(field))

        for override in self.config.param_overrides:
            if not isinstance(override, dict):
                continue
            op = str(override.get("op") or override.get("operation") or "set").lower()
            path = override.get("path") or override.get("field")
            if not path:
                continue
            if op == "delete":
                self._delete_path(payload, str(path))
            elif op == "set":
                self._set_path(payload, str(path), override.get("value"))
        return payload

    def _build_payload(self, request: ChatCompletionRequest, actual_model: str, stream: bool) -> dict[str, Any]:
        if self.config.pass_through_body:
            raw_body = getattr(request, "_gateway_raw_body", b"") or b""
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                logger.warning(f"[{self.config.name}] Failed to parse raw body for pass-through; falling back to normalized payload")
                payload = request.to_provider_payload(actual_model)
        else:
            payload = request.to_provider_payload(actual_model)

        payload["model"] = actual_model
        payload["stream"] = stream
        return self._apply_body_rules(payload)

    def _build_request_kwargs(self, request: ChatCompletionRequest, actual_model: str, api_key: str, stream: bool) -> dict[str, Any]:
        payload = self._build_payload(request, actual_model, stream)
        headers = self._build_request_headers(request, api_key)
        if self.config.pass_through_body or self.config.param_overrides or self.config.disabled_fields:
            return {
                "content": json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "headers": headers,
            }
        return {
            "json": payload,
            "headers": headers,
        }

    def _extract_error_message(self, resp: httpx.Response) -> str:
        try:
            data = resp.json()
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err.get("code") or data)
            if err:
                return str(err)
            return resp.text[:500]
        except Exception:
            return resp.text[:500]

    def _extract_structured_error(self, data: Any) -> Optional[tuple[str, Optional[int]]]:
        """Extract a non-empty OpenAI-style error from a successful JSON response."""
        if not isinstance(data, dict):
            return None

        error = data.get("error")
        if error is None or error == "" or error == {}:
            return None

        if not isinstance(error, dict):
            return str(error), None

        message = str(
            error.get("message")
            or error.get("detail")
            or error.get("code")
            or error
        )

        status_code = None
        for field in ("status_code", "status", "http_status", "code"):
            value = error.get(field)
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                continue
            if 400 <= candidate <= 599:
                status_code = candidate
                break

        return message, status_code

    def _extract_sse_structured_error(
        self, line: str
    ) -> Optional[tuple[str, Optional[int]]]:
        """Extract an OpenAI-style error embedded in an SSE data event."""
        stripped = (line or "").strip()
        if not stripped.startswith("data:"):
            return None

        payload = stripped[5:].strip()
        if not payload or payload == "[DONE]":
            return None

        try:
            data = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return self._extract_structured_error(data)

    def _classify_http_error(self, status_code: int, message: str, key_idx: int, actual_model: str) -> GatewayError:
        policy = config.settings.error_policy
        lower_msg = (message or "").lower()

        error_type = "HTTPError"
        error_code = f"http_{status_code}"
        cooldown = config.settings.cooldown.other_error
        disable_key = policy.should_disable_key(status_code)
        key_fatal = disable_key
        disable_model = policy.should_disable_model(status_code)
        disable_provider = policy.should_disable_provider(status_code)

        if status_code == 429:
            error_type = "RateLimit"
            cooldown = config.settings.cooldown.rate_limit_429
            disable_key = True
            key_fatal = False
        elif status_code == 401:
            error_type = "AuthError"
            cooldown = config.settings.cooldown.auth_error_401
        elif status_code == 403:
            error_type = "Forbidden"
            cooldown = config.settings.cooldown.forbidden_403
        elif status_code == 404:
            error_type = "ModelNotFound"
            cooldown = config.settings.cooldown.other_error
        elif 500 <= status_code <= 599:
            error_type = "ServerError"
            cooldown = config.settings.cooldown.provider_melt

        if any(k in lower_msg for k in ["quota", "insufficient_quota", "credit", "balance", "insufficient balance"]):
            error_type = "QuotaExceeded"
            error_code = "quota_exceeded"
            cooldown = config.settings.cooldown.quota_exceeded
            disable_key = True
            key_fatal = True

        retryable = policy.should_retry(status_code)
        return GatewayError(
            message=message or f"HTTP {status_code}",
            error_type=error_type,
            status_code=status_code,
            error_code=error_code,
            retryable=retryable,
            skip_retry=not retryable,
            key_index=key_idx,
            disable_key=disable_key,
            key_fatal=key_fatal,
            disable_model=disable_model,
            disable_provider=disable_provider,
            cooldown=cooldown,
            provider=self.config.name,
            model=actual_model,
        )

    def _classify_exception(self, exc: Exception, key_idx: int, actual_model: str) -> GatewayError:
        if isinstance(exc, GatewayError):
            return exc

        err_msg = str(exc) or repr(exc)
        lower_msg = err_msg.lower()
        error_type = type(exc).__name__
        error_code = "exception"
        cooldown = 120
        disable_key = False
        key_fatal = False
        disable_provider = False

        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError)):
            error_type = "Timeout" if "timeout" in lower_msg or isinstance(exc, asyncio.TimeoutError) else "NetworkError"
            error_code = "timeout_network"
            cooldown = min(config.settings.cooldown.timeout_network, 60)
            disable_key = True
            disable_provider = True
        elif any(k in lower_msg for k in ["quota", "insufficient_quota", "credit", "balance", "insufficient balance"]):
            error_type = "QuotaExceeded"
            error_code = "quota_exceeded"
            cooldown = config.settings.cooldown.quota_exceeded
            disable_key = True
            key_fatal = True

        return GatewayError(
            message=err_msg,
            error_type=error_type,
            error_code=error_code,
            retryable=True,
            key_index=key_idx,
            disable_key=disable_key,
            key_fatal=key_fatal,
            disable_provider=disable_provider,
            cooldown=cooldown,
            provider=self.config.name,
            model=actual_model,
        )

    def _record_gateway_error(self, err: GatewayError, actual_model: str):
        log_event(logger, "upstream_failed", logging.WARNING, provider=self.config.name,
                  model=actual_model, key_index=err.key_index, status_code=err.status_code,
                  error_type=err.error_type, error_code=err.error_code,
                  retryable=err.retryable, cooldown=err.cooldown)
        cooldown = err.cooldown or config.settings.cooldown.other_error
        if err.disable_key and err.key_index >= 0:
            self.config.record_key_failure(err.key_index, cooldown, is_fatal=err.key_fatal)
        if err.disable_model:
            self.config.record_model_failure(actual_model, cooldown)

    def _raise_exhausted(self, actual_model: str, last_error: Any):
        err = GatewayError.from_exception(last_error, provider=self.config.name, model=actual_model)
        raise GatewayError(
            message=f"[{self.config.name}] Exhausted all keys for {actual_model}. Last Error: {err.message}",
            error_type=err.error_type,
            status_code=err.status_code,
            error_code=err.error_code,
            retryable=err.retryable,
            skip_retry=err.skip_retry,
            key_index=err.key_index,
            disable_key=err.disable_key,
            key_fatal=err.key_fatal,
            disable_model=err.disable_model,
            disable_provider=err.disable_provider,
            cooldown=err.cooldown,
            provider=self.config.name,
            model=actual_model,
        )

    async def chat_completion(
        self, request: ChatCompletionRequest, actual_model: str, timeout: Optional[float] = None
    ) -> ChatCompletionResponse:
        """Non-streaming chat completion with internal multi-key retry logic."""
        url = f"{self.config.base_url}/chat/completions"
        effective_timeout = timeout if timeout is not None else self.config.timeout
        
        num_keys = len(self.config.api_keys) or 1
        last_error = None
        
        for attempt in range(num_keys):
            api_key, key_idx = self.config.get_next_key()
            if not api_key:
                break
                
            log_event(logger, "upstream_started", provider=self.config.name, model=actual_model,
                      key_index=key_idx, attempt=attempt + 1, stream=False)
            attempt_start = time.perf_counter()
            
            try:
                request_kwargs = self._build_request_kwargs(request, actual_model, api_key, stream=False)
                resp = await self.batch_client.post(
                    url,
                    **request_kwargs,
                    timeout=effective_timeout
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    structured_error = self._extract_structured_error(data)
                    if structured_error is not None:
                        err_msg, embedded_status = structured_error
                        err = self._classify_http_error(
                            embedded_status or 502,
                            f"Upstream API Error: {err_msg}",
                            key_idx,
                            actual_model,
                        )
                        logger.warning(
                            f"[{self.config.name}] {actual_model} returned HTTP 200 "
                            f"with a structured error: {err}"
                        )
                        self._record_gateway_error(err, actual_model)
                        raise err

                    choices_data = data.get("choices", [])
                    if not choices_data:
                        raise Exception("Upstream returned 200 OK but choices list is empty.")
                    
                    result = self._parse_response(data, request.model)
                    log_event(logger, "upstream_finished", provider=self.config.name,
                              model=actual_model, key_index=key_idx,
                              elapsed_ms=round((time.perf_counter() - attempt_start) * 1000, 3),
                              usage=result.usage.model_dump() if result.usage else None)
                    return result
                
                else:
                    err = self._classify_http_error(
                        resp.status_code,
                        self._extract_error_message(resp),
                        key_idx,
                        actual_model,
                    )
                    logger.warning(f"[{self.config.name}] {actual_model} failed: {err}")
                    self._record_gateway_error(err, actual_model)
                    last_error = err
                    if err.skip_retry or err.disable_model:
                        raise err
                
            except GatewayError as e:
                last_error = e
                if e.skip_retry or e.disable_model:
                    raise
            except Exception as e:
                err = self._classify_exception(e, key_idx, actual_model)
                last_error = err
                logger.warning(f"[{self.config.name}] Key[{key_idx}] failed: {err}")
                self._record_gateway_error(err, actual_model)
                if err.skip_retry or err.disable_model:
                    raise err
                
            if attempt < num_keys - 1:
                logger.info(f"[{self.config.name}] Attempt {attempt+1} failed ({last_error}), trying next key...")
                continue
            
        # If all keys failed with timeout, return to caller
        if "Timeout" in str(last_error) or "timeout" in str(last_error):
            logger.warning(f"[{self.config.name}] All keys timed out for {actual_model}. Letting router decide melting strategy.")

        self._raise_exhausted(actual_model, last_error)

    def _parse_response(self, data: dict, model_alias: str) -> ChatCompletionResponse:
        """Helper to parse OpenAI JSON into ChatCompletionResponse."""
        choices = []
        for c in data.get("choices", []):
            msg = c.get("message", {})
            choices.append(
                ChatChoice(
                    index=c.get("index", 0),
                    message=ChatCompletionMessage(
                        role=msg.get("role", "assistant"),
                        content=msg.get("content"),
                        tool_calls=msg.get("tool_calls"),
                    ),
                    logprobs=c.get("logprobs"),
                    finish_reason=c.get("finish_reason", "stop"),
                )
            )

        usage_data = data.get("usage", {})
        usage = UsageInfo(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        return ChatCompletionResponse(
            id=data.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}"),
            model=model_alias,
            choices=choices,
            usage=usage,
            system_fingerprint=data.get("system_fingerprint"),
        )

    async def chat_completion_stream(
        self, request: ChatCompletionRequest, actual_model: str, timeout: Optional[float] = None
    ) -> AsyncIterator[str]:
        """Streaming chat completion with internal multi-key retry logic."""
        url = f"{self.config.base_url}/chat/completions"
        effective_timeout = timeout if timeout is not None else self.config.timeout
        
        num_keys = len(self.config.api_keys) or 1
        last_error = None

        for attempt in range(num_keys):
            api_key, key_idx = self.config.get_next_key()
            if not api_key:
                break
                
            log_event(logger, "upstream_started", provider=self.config.name, model=actual_model,
                      key_index=key_idx, attempt=attempt + 1, stream=True)
            emitted = False
            
            try:
                request_kwargs = self._build_request_kwargs(request, actual_model, api_key, stream=True)
                async with self.stream_client.stream(
                    "POST", 
                    url, 
                    **request_kwargs,
                    timeout=httpx.Timeout(
                        connect=10.0,
                        read=effective_timeout,
                        write=20.0,
                        pool=60.0
                    )
                ) as resp:
                    if resp.status_code == 200:
                        resp.raise_for_status()
                        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
                        created = int(time.time())
                        
                        # 1. Prepare for TTFT (Heartbeat disabled temporarily)
                        start_time = time.time()
                        chunks_received = 0
                        bytes_received = 0
                        
                        # Get the iterator 
                        lines_iter = resp.aiter_lines().__aiter__()
                        
                        # First chunk timeout detection (within provider retry loop)
                        first_chunk = None
                        try:
                            first_chunk = await asyncio.wait_for(lines_iter.__anext__(), timeout=effective_timeout)
                        except asyncio.TimeoutError:
                            err = GatewayError(
                                message=f"First chunk timeout ({effective_timeout}s)",
                                error_type="StreamFirstChunkTimeout",
                                error_code="first_chunk_timeout",
                                retryable=True,
                                key_index=key_idx,
                                disable_key=True,
                                disable_provider=True,
                                cooldown=min(config.settings.cooldown.timeout_network, 60),
                                provider=self.config.name,
                                model=actual_model,
                            )
                            logger.error(f"[{self.config.name}] {err}")
                            self._record_gateway_error(err, actual_model)
                            last_error = err
                            continue  # Try next key
                        
                        # Process first chunk
                        first_chunk = first_chunk.strip()
                        if first_chunk and first_chunk.startswith("data:"):
                            structured_error = self._extract_sse_structured_error(first_chunk)
                            if structured_error is not None:
                                err_msg, embedded_status = structured_error
                                err = self._classify_http_error(
                                    embedded_status or 502,
                                    f"Upstream API Error: {err_msg}",
                                    key_idx,
                                    actual_model,
                                )
                                logger.warning(
                                    f"[{self.config.name}] {actual_model} returned HTTP 200 "
                                    f"with a structured stream error before content: {err}"
                                )
                                self._record_gateway_error(err, actual_model)
                                last_error = err
                                if err.skip_retry or err.disable_model:
                                    raise err
                                continue

                            ttft = (time.time() - start_time) * 1000
                            log_event(logger, "upstream_first_chunk", provider=self.config.name,
                                      model=actual_model, key_index=key_idx, elapsed_ms=round(ttft, 3))
                            chunks_received += 1
                            bytes_received += len(first_chunk)
                            logger.info(f"[{self.config.name}] First chunk received for {actual_model} at +{ttft:.2f}ms (len={len(first_chunk)})")
                            emitted = True
                            yield f"{first_chunk}\n\n"
                            if first_chunk == "data: [DONE]":
                                log_event(logger, "upstream_stream_finished", provider=self.config.name,
                                          model=actual_model, key_index=key_idx, outcome="done")
                                logger.info(f"[{self.config.name}] Received end signal for {actual_model} in first chunk")
                                logger.info(f"[{self.config.name}] Stream finished for {actual_model}: {chunks_received} chunks, {bytes_received} bytes received")
                                return
                            await asyncio.sleep(0.01)
                        
                        # Continue with remaining chunks
                        while True:
                            try:
                                line = await lines_iter.__anext__()
                                
                                line = line.strip()
                                if not line: continue
                                if line == "data: [DONE]": 
                                    log_event(logger, "upstream_stream_finished", provider=self.config.name,
                                              model=actual_model, key_index=key_idx, outcome="done")
                                    chunks_received += 1
                                    bytes_received += len(line)
                                    logger.info(f"[{self.config.name}] Received end signal for {actual_model} after {chunks_received} chunks")
                                    yield "data: [DONE]\n\n"
                                    break
                                
                                if line.startswith("data:"):
                                    chunks_received += 1
                                    bytes_received += len(line)
                                    structured_error = self._extract_sse_structured_error(line)
                                    if structured_error is not None:
                                        err_msg, embedded_status = structured_error
                                        err = self._classify_http_error(
                                            embedded_status or 502,
                                            f"Upstream API Error: {err_msg}",
                                            key_idx,
                                            actual_model,
                                        )
                                        logger.warning(
                                            f"[{self.config.name}] {actual_model} returned a "
                                            f"structured stream error after streaming began: {err}"
                                        )
                                        self._record_gateway_error(err, actual_model)
                                        yield f"{line}\n\n"
                                        yield "data: [DONE]\n\n"
                                        return
                                    emitted = True
                                    yield f"{line}\n\n"
                                    await asyncio.sleep(0.01)
                                
                            except StopAsyncIteration:
                                log_event(logger, "upstream_stream_finished", provider=self.config.name,
                                          model=actual_model, key_index=key_idx, outcome="eof")
                                break
                        
                        logger.info(f"[{self.config.name}] Stream finished for {actual_model}: {chunks_received} chunks, {bytes_received} bytes received")
                        return # Success

                    else:
                        await resp.aread()
                        err = self._classify_http_error(
                            resp.status_code,
                            self._extract_error_message(resp),
                            key_idx,
                            actual_model,
                        )
                        logger.warning(f"[{self.config.name}] Streaming {actual_model} failed: {err}")
                        self._record_gateway_error(err, actual_model)
                        last_error = err
                        if err.skip_retry or err.disable_model:
                            raise err
                        
            except GatewayError as e:
                last_error = e
                if e.skip_retry or e.disable_model:
                    raise
            except Exception as e:
                err = self._classify_exception(e, key_idx, actual_model)
                last_error = err
                logger.warning(f"[{self.config.name}] Stream Key[{key_idx}] failed: {err}")
                self._record_gateway_error(err, actual_model)
                if err.skip_retry or err.disable_model:
                    raise err
                
            if emitted:
                log_event(logger, "stream_retry_suppressed", logging.WARNING,
                          provider=self.config.name, model=actual_model,
                          reason="output_already_started")
                yield 'data: {"error":{"code":"upstream_stream_interrupted","message":"Upstream stream interrupted; retry suppressed"}}\n\n'
                yield "data: [DONE]\n\n"
                return
            if attempt < num_keys - 1:
                logger.info(f"[{self.config.name}] Stream attempt {attempt+1} failed ({last_error}), trying next key...")
                continue
        
        if "Timeout" in str(last_error) or "timeout" in str(last_error):
            logger.warning(f"[{self.config.name}] All keys timed out for streaming {actual_model}. Letting router decide fallback.")

        self._raise_exhausted(actual_model, last_error)

    async def list_models(self) -> list[dict[str, Any]]:
        """List available models with automatic key rotation."""
        url = f"{self.config.base_url}/models"
        api_key, _ = self.config.get_next_key()
        try:
            resp = await self.batch_client.get(url, headers=self._build_headers(api_key))
            resp.raise_for_status()
            data = resp.json()

            models = []
            for m in data.get("data", []):
                model_dict = {
                    "id": m.get("id", ""),
                    "object": "model",
                    "created": m.get("created", int(time.time())),
                    "owned_by": self.config.name,
                }
                # Add all other metadata to the model dict for scoring
                for k, v in m.items():
                    if k not in model_dict:
                        model_dict[k] = v
                models.append(model_dict)
            logger.info(f"[{self.config.name}] list_models: fetched {len(models)} models")
            return models
        except Exception as e:
            logger.warning(f"[{self.config.name}] list_models failed: {e}")
            return []
