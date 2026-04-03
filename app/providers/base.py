"""
Base provider abstract class.
All LLM providers must implement this interface.
"""

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Optional

from app.config import ProviderConfig
from app.models.request import ChatCompletionRequest
from app.models.response import ChatCompletionResponse


class BaseProvider(ABC):
    """Abstract base class for LLM API providers."""

    def __init__(self, config: ProviderConfig):
        self.config = config

    @abstractmethod
    async def chat_completion(
        self, request: ChatCompletionRequest, actual_model: str, timeout: Optional[float] = None
    ) -> ChatCompletionResponse:
        """Perform a non-streaming chat completion request."""
        ...

    @abstractmethod
    async def chat_completion_stream(
        self, request: ChatCompletionRequest, actual_model: str, timeout: Optional[float] = None
    ) -> AsyncIterator[str]:
        """Perform a streaming chat completion request. Yields SSE data strings."""
        ...

    @abstractmethod
    async def list_models(self) -> list[dict[str, Any]]:
        """List available models from this provider."""
        ...
