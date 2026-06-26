"""Provider package — the OpenAI-compatible client lives here."""
from .base import ProviderProfile, load_providers, resolve_api_key, active_profile
from .openai_compat import (
    OpenAICompatProvider,
    ChatMessage,
    ChatResult,
    StreamDelta,
    ToolSpec,
)
from .adapters import (
    ProviderAdapter,
    AnthropicProvider,
    AzureOpenAIProvider,
    GeminiProvider,
    OllamaNativeProvider,
    ZAiProvider,
    get_provider,
)

__all__ = [
    "ProviderProfile",
    "load_providers",
    "resolve_api_key",
    "active_profile",
    "OpenAICompatProvider",
    "ChatMessage",
    "ChatResult",
    "StreamDelta",
    "ToolSpec",
    "ProviderAdapter",
    "AnthropicProvider",
    "AzureOpenAIProvider",
    "GeminiProvider",
    "OllamaNativeProvider",
    "ZAiProvider",
    "get_provider",
]
