"""
OpenAI-compatible provider implementation.
Provides an adapter for OpenRouter, Groq, Cerebras, GitHub Models, and other AI platforms that implement the OpenAI spec.
"""

import json
import logging
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
from app.providers.base import BaseProvider
from app.services.gateway_errors import GatewayError


logger = logging.getLogger("freelance.provider")


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
        payload = request.to_provider_payload(actual_model)
        payload["stream"] = False

        url = f"{self.config.base_url}/chat/completions"
        effective_timeout = timeout if timeout is not None else self.config.timeout
        
        num_keys = len(self.config.api_keys) or 1
        last_error = None
        
        for attempt in range(num_keys):
            api_key, key_idx = self.config.get_next_key()
            if not api_key:
                break
                
            masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "no-key"
            logger.info(f"[{self.config.name}] Requesting {actual_model} using key[{key_idx}]: {masked_key} (attempt {attempt+1}/{num_keys})")
            
            try:
                resp = await self.batch_client.post(
                    url, 
                    json=payload, 
                    headers=self._build_headers(api_key),
                    timeout=effective_timeout
                )
                
                if resp.status_code == 200:
                    data = resp.json()
                    if "error" in data:
                        err = data["error"]
                        err_msg = err.get("message") if isinstance(err, dict) else str(err)
                        raise Exception(f"Upstream API Error: {err_msg}")

                    choices_data = data.get("choices", [])
                    if not choices_data:
                        raise Exception("Upstream returned 200 OK but choices list is empty.")
                    
                    return self._parse_response(data, request.model)
                
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
        payload = request.to_provider_payload(actual_model)
        payload["stream"] = True

        url = f"{self.config.base_url}/chat/completions"
        effective_timeout = timeout if timeout is not None else self.config.timeout
        
        num_keys = len(self.config.api_keys) or 1
        last_error = None

        for attempt in range(num_keys):
            api_key, key_idx = self.config.get_next_key()
            if not api_key:
                break
                
            masked_key = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else "no-key"
            logger.info(f"[{self.config.name}] Streaming {actual_model} using key[{key_idx}]: {masked_key} (attempt {attempt+1}/{num_keys})")
            
            try:
                async with self.stream_client.stream(
                    "POST", 
                    url, 
                    json=payload, 
                    headers=self._build_headers(api_key),
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
                        ttft_recorded = False
                        
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
                        if first_chunk and first_chunk.startswith("data: "):
                            ttft = (time.time() - start_time) * 1000
                            logger.info(f"[{self.config.name}] TTFT: {ttft:.2f}ms | Model: {actual_model}")
                            yield f"{first_chunk}\n\n"
                            await asyncio.sleep(0.01)
                        
                        # Continue with remaining chunks
                        while True:
                            try:
                                line = await lines_iter.__anext__()
                                
                                line = line.strip()
                                if not line: continue
                                if line == "data: [DONE]": 
                                    yield "data: [DONE]\n\n"
                                    break
                                
                                if line.startswith("data: "):
                                    yield f"{line}\n\n"
                                    await asyncio.sleep(0.01)
                                
                            except StopAsyncIteration:
                                break
                        
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
