"""
OpenAI-compatible request models.
"""

from typing import Any, Optional, Union

from pydantic import BaseModel, Field, model_validator


class ChatMessage(BaseModel):
    """A single message in a chat conversation."""

    role: str  # "system", "user", "assistant", "developer", "tool"
    content: Optional[Union[str, list[dict[str, Any]]]] = None
    name: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ResponseFormat(BaseModel):
    """Response format specification."""

    type: str = "text"  # "text" or "json_object"


class _ChatCompletionRequestBase(BaseModel):
    """Fields shared by single-model and multi-model chat requests."""

    model: Optional[str] = "auto"
    messages: list[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = None
    stop: Optional[Union[str, list[str]]] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    logit_bias: Optional[dict[str, float]] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    user: Optional[str] = None
    seed: Optional[int] = None
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[Union[str, dict[str, Any]]] = None
    parallel_tool_calls: Optional[bool] = None
    response_format: Optional[ResponseFormat] = None

    model_config = {"extra": "allow"}


class ChatCompletionRequest(_ChatCompletionRequestBase):
    """OpenAI-compatible chat completion request."""

    stream: Optional[bool] = False

    def to_provider_payload(self, actual_model: str) -> dict[str, Any]:
        """Convert to a provider-compatible payload dict, excluding None values."""
        data = self.model_dump(exclude_none=True)
        data["model"] = actual_model
        return data


class MultiChatCompletionRequest(_ChatCompletionRequestBase):
    """Request the same prompt from multiple targets using non-streaming aggregation."""

    models: Optional[list[str]] = Field(default=None, min_length=1)
    parallel: bool = True

    @model_validator(mode="before")
    @classmethod
    def reject_stream_parameter(cls, data):
        if isinstance(data, dict) and "stream" in data:
            raise ValueError(
                "The multi-model endpoint does not accept the stream parameter"
            )
        return data

    def to_single_request(self, model: str) -> ChatCompletionRequest:
        data = self.model_dump(
            exclude={"models", "parallel"},
            exclude_none=True,
        )
        data["model"] = model
        data["stream"] = False
        return ChatCompletionRequest(**data)
