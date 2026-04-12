from .base import BaseProvider
from .openai_compatible import OpenAICompatibleProvider

def get_provider(p_cfg) -> BaseProvider:
    """Factory function to get the correct provider instance."""
    # Currently all providers are OpenAI compatible
    # In the future, this can branch out based on p_cfg.provider_type
    return OpenAICompatibleProvider(p_cfg)

__all__ = ["BaseProvider", "OpenAICompatibleProvider", "get_provider"]
