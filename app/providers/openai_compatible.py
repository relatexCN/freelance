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
                
                elif resp.status_code == 429:
                    c_dur = config.settings.cooldown.rate_limit_429
                    logger.warning(f"[{self.config.name}] Key[{key_idx}] hit 429 Rate Limit. Cooling down for {c_dur}s.")
                    self.config.record_key_failure(key_idx, c_dur)
                    last_error = f"Rate Limit (429) on key {key_idx}"
                elif resp.status_code == 401:
                    c_dur = config.settings.cooldown.auth_error_401
                    logger.error(f"[{self.config.name}] Key[{key_idx}] returned 401 (Unauthorized). Cooling down for {c_dur}s.")
                    self.config.record_key_failure(key_idx, c_dur, is_fatal=True)
                    last_error = f"HTTP 401 on key {key_idx}"
                elif resp.status_code == 403:
                    c_dur = config.settings.cooldown.forbidden_403
                    logger.error(f"[{self.config.name}] Key[{key_idx}] returned 403 (Forbidden). Cooling down for {c_dur}s.")
                    self.config.record_key_failure(key_idx, c_dur, is_fatal=True)
                    last_error = f"HTTP 403 on key {key_idx}"
                elif resp.status_code == 404:
                    c_dur = config.settings.cooldown.other_error # Fallback to other_error or 401? User asked for 3h
                    logger.warning(f"[{self.config.name}] Key[{key_idx}] returned 404. Model {actual_model} likely unavailable. Cooling down for {c_dur}s.")
                    self.config.record_model_failure(actual_model, c_dur)
                    last_error = f"HTTP 404 (Model Not Found) on key {key_idx}"
                elif resp.status_code == 400:
                    # Pass-through 400 Bad Request directly to client without melting
                    resp.raise_for_status()
                else:
                    resp.raise_for_status()
                
            except (httpx.HTTPStatusError, httpx.RequestError, asyncio.TimeoutError, Exception) as e:
                err_msg = str(e) or repr(e)
                last_error = err_msg
                
                # Check for Quota Exceeded in the error message
                lower_err = err_msg.lower()
                if any(k in lower_err for k in ["quota", "insufficient_quota", "credit", "balance", "insufficient balance"]):
                    c_dur = config.settings.cooldown.quota_exceeded
                    logger.error(f"[{self.config.name}] Key[{key_idx}] quota exceeded: {err_msg}. Cooling down for {c_dur}s.")
                    self.config.record_key_failure(key_idx, c_dur, is_fatal=True)
                    # For total failure, re-raise or raise a specific error to allow router fallback
                    raise Exception(f"Quota Exceeded: {err_msg}")

                # If it's a 400, re-raise to exit provider loop and stop melting
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 400:
                    raise e

                # Connection/Timeout handling
                cooldown = 120 # Default
                if isinstance(e, (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError)):
                    cooldown = 60 # Shorter cooldown for network blips
                    logger.warning(f"[{self.config.name}] Key[{key_idx}] network/timeout error: {type(e).__name__}. Cooldown {cooldown}s.")
                else:
                    logger.warning(f"[{self.config.name}] Key[{key_idx}] failed: {err_msg}. Cooldown {cooldown}s.")
                
                self.config.record_key_failure(key_idx, cooldown)
                
            if attempt < num_keys - 1:
                logger.info(f"[{self.config.name}] Attempt {attempt+1} failed ({last_error}), trying next key...")
                continue
            
        # If all keys failed with timeout, return to caller
        if "Timeout" in str(last_error) or "timeout" in str(last_error):
            logger.warning(f"[{self.config.name}] All keys timed out for {actual_model}. Letting router decide melting strategy.")

        raise Exception(f"[{self.config.name}] Exhausted all keys for {actual_model}. Last Error: {last_error}")

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
                            logger.error(f"[{self.config.name}] First chunk timeout for {actual_model} using key[{key_idx}] ({effective_timeout}s)")
                            self.config.record_key_failure(key_idx, 60)  # Short cooldown for timeout
                            last_error = f"First chunk timeout ({effective_timeout}s)"
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

                    elif resp.status_code == 429:
                        c_dur = config.settings.cooldown.rate_limit_429
                        logger.warning(f"[{self.config.name}] Stream Key[{key_idx}] hit 429. Cooling down {c_dur}s.")
                        self.config.record_key_failure(key_idx, c_dur)
                        last_error = f"Rate Limit (429) on key {key_idx}"
                    elif resp.status_code == 401:
                        c_dur = config.settings.cooldown.auth_error_401
                        logger.error(f"[{self.config.name}] Stream Key[{key_idx}] returned 401. Cooling down {c_dur}s.")
                        self.config.record_key_failure(key_idx, c_dur, is_fatal=True)
                        last_error = f"HTTP 401"
                    elif resp.status_code == 403:
                        c_dur = config.settings.cooldown.forbidden_403
                        logger.error(f"[{self.config.name}] Stream Key[{key_idx}] returned 403. Cooling down {c_dur}s.")
                        self.config.record_key_failure(key_idx, c_dur, is_fatal=True)
                        last_error = f"HTTP 403"
                    elif resp.status_code == 404:
                        c_dur = config.settings.cooldown.other_error
                        logger.warning(f"[{self.config.name}] Stream Key[{key_idx}] returned 404 for {actual_model}. Disabling model for {c_dur}s.")
                        self.config.record_model_failure(actual_model, c_dur)
                        last_error = f"HTTP 404 (Model Not Found)"
                    elif resp.status_code == 400:
                        # Bad Request - return error message to client
                        try:
                            error_detail = resp.json()
                            error_msg = error_detail.get("error", {}).get("message", str(resp.status_code))
                        except:
                            error_msg = f"Bad Request: {resp.text[:200]}"
                        logger.warning(f"[{self.config.name}] Stream Key[{key_idx}] returned 400: {error_msg}")
                        raise Exception(f"Bad Request: {error_msg}")
                    else:
                        resp.raise_for_status()
                        
            except (httpx.HTTPStatusError, httpx.RequestError, asyncio.TimeoutError, Exception) as e:
                err_msg = str(e) or repr(e)
                last_error = err_msg
                
                # Check for Quota Exceeded in the error message
                lower_err = err_msg.lower()
                if any(k in lower_err for k in ["quota", "insufficient_quota", "credit", "balance"]):
                    c_dur = config.settings.cooldown.quota_exceeded
                    logger.error(f"[{self.config.name}] Stream Key[{key_idx}] quota exceeded: {err_msg}. Cooling down for {c_dur}s.")
                    self.config.record_key_failure(key_idx, c_dur, is_fatal=True)
                    raise Exception(f"Quota Exceeded")

                # Handle 400 Bad Request separately - don't retry, return error to client
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 400:
                    try:
                        error_detail = e.response.json()
                        error_msg = error_detail.get("error", {}).get("message", str(e.response.status_code))
                    except:
                        error_msg = f"Bad Request: {e.response.text[:200]}"
                    logger.warning(f"[{self.config.name}] Stream Key[{key_idx}] returned 400: {error_msg}")
                    raise Exception(f"Bad Request: {error_msg}")

                cooldown = 120
                if isinstance(e, (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError)):
                    cooldown = 60
                logger.warning(f"[{self.config.name}] Stream Key[{key_idx}] failed: {type(e).__name__}. Cooldown {cooldown}s.")
                self.config.record_key_failure(key_idx, cooldown)
                
            if attempt < num_keys - 1:
                logger.info(f"[{self.config.name}] Stream attempt {attempt+1} failed ({last_error}), trying next key...")
                continue
        
        if "Timeout" in str(last_error) or "timeout" in str(last_error):
            logger.warning(f"[{self.config.name}] All keys timed out for streaming {actual_model}. Letting router decide fallback.")

        raise Exception(f"[{self.config.name}] Exhausted all keys for streaming {actual_model}. Last Error: {last_error}")
        
        if last_error:
            raise Exception(f"[{self.config.name}] Streaming failed: {last_error}")

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
            return models
        except Exception:
            return []
