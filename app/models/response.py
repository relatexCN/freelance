"""
OpenAI-compatible response models.
"""

import time
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatCompletionMessage(BaseModel):
    """Response message."""

    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None


class ChatChoice(BaseModel):
    """A single completion choice."""

    index: int = 0
    message: ChatCompletionMessage
    logprobs: Optional[Any] = None
    finish_reason: Optional[str] = "stop"


class UsageInfo(BaseModel):
    """Token usage information."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    completion_tokens_details: Optional[dict[str, int]] = None


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response."""

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    system_fingerprint: Optional[str] = None
    choices: list[ChatChoice] = []
    usage: Optional[UsageInfo] = None


# --- Streaming response models ---


class DeltaMessage(BaseModel):
    """Delta message for streaming."""

    role: Optional[str] = None
    content: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None


class StreamChoice(BaseModel):
    """A streaming choice."""

    index: int = 0
    delta: DeltaMessage
    logprobs: Optional[Any] = None
    finish_reason: Optional[str] = None


class ChatCompletionStreamResponse(BaseModel):
    """OpenAI-compatible streaming chunk."""

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    system_fingerprint: Optional[str] = None
    choices: list[StreamChoice] = []


class ModelInfo(BaseModel):
    """Model information for /v1/models endpoint."""

    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "freelance-gateway"
    score: Optional[float] = 0.0
    context_length: Optional[int] = None
    capabilities: Optional[list[str]] = None
    
    model_config = {"extra": "allow"}


class ModelListResponse(BaseModel):
    """Response for /v1/models endpoint."""

    object: str = "list"
    data: list[ModelInfo] = []
