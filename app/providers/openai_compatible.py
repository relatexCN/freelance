"""
OpenAI-compatible provider implementation.
Provides an adapter for OpenRouter, Groq, Cerebras, GitHub Models, and other AI platforms that implement the OpenAI spec.
"""

import json
import time
import uuid
from typing import Any, AsyncIterator, Optional

import httpx

from app.config import ProviderConfig
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


class OpenAICompatibleProvider(BaseProvider):
    """Provider for any OpenAI-compatible API."""

    def __init__(self, config: ProviderConfig):
        super().__init__(config)

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://freelance-gateway.local",
            "X-Title": "Freelance API Gateway",
        }
        api_key = self.config.get_next_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def chat_completion(
        self, request: ChatCompletionRequest, actual_model: str, timeout: Optional[float] = None
    ) -> ChatCompletionResponse:
        """Non-streaming chat completion via OpenAI-compatible endpoint."""
        payload = request.to_provider_payload(actual_model)
        payload["stream"] = False

        url = f"{self.config.base_url}/chat/completions"
        effective_timeout = timeout if timeout is not None else self.config.timeout

        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            resp = await client.post(url, json=payload, headers=self._build_headers())
            resp.raise_for_status()
            data = resp.json()

        # Endpoint returns OpenAI-compatible format, adapt minimally
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
            model=request.model,  # Return the alias
            choices=choices,
            usage=usage,
            system_fingerprint=data.get("system_fingerprint"),
        )

    async def chat_completion_stream(
        self, request: ChatCompletionRequest, actual_model: str, timeout: Optional[float] = None
    ) -> AsyncIterator[str]:
        """Streaming chat completion via OpenAI-compatible endpoint (SSE passthrough)."""
        payload = request.to_provider_payload(actual_model)
        payload["stream"] = True

        url = f"{self.config.base_url}/chat/completions"
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        effective_timeout = timeout if timeout is not None else self.config.timeout

        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            async with client.stream(
                "POST", url, json=payload, headers=self._build_headers()
            ) as resp:
                resp.raise_for_status()

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue

                    if line == "data: [DONE]":
                        # sse-starlette will handle the formatting
                        break

                    if line.startswith("data: "):
                        raw = line[6:]
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # Rewrite the model name to the alias
                        choices = []
                        for c in data.get("choices", []):
                            delta = c.get("delta", {})
                            choices.append(
                                StreamChoice(
                                    index=c.get("index", 0),
                                    delta=DeltaMessage(
                                        role=delta.get("role"),
                                        content=delta.get("content"),
                                        tool_calls=delta.get("tool_calls"),
                                    ),
                                    finish_reason=c.get("finish_reason"),
                                )
                            )

                        chunk = ChatCompletionStreamResponse(
                            id=data.get("id", completion_id),
                            created=data.get("created", created),
                            model=request.model,
                            choices=choices,
                            system_fingerprint=data.get("system_fingerprint"),
                        )
                        # Yield raw JSON only
                        yield chunk.model_dump_json()

    async def list_models(self) -> list[dict[str, Any]]:
        """List available models from the OpenAI-compatible endpoint."""
        url = f"{self.config.base_url}/models"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=self._build_headers())
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
