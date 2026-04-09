# clients/llm/factory.py
"""UnifiedLLM wrapper and singleton cache."""

from typing import AsyncGenerator

from clients.llm.providers.azure  import AzureProvider
from clients.llm.providers.chutes import ChutesProvider
from clients.llm.types import LLMRequest, LLMChunk, LLMResponse, LLMProvider

_LLM_SINGLETON_CACHE: dict = {}


def get_llm_client(provider_type: str = "azure") -> "UnifiedLLM":
    """
    Singleton factory. Returns a cached instance per provider_type.
    
    Ensures that external callers using either the old path (clients.llm_client)
    or the new path (clients.llm.factory) get the same cached instance.
    """
    if provider_type not in _LLM_SINGLETON_CACHE:
        _LLM_SINGLETON_CACHE[provider_type] = UnifiedLLM(provider_type=provider_type)
    return _LLM_SINGLETON_CACHE[provider_type]


class UnifiedLLM:
    """Unified wrapper that delegates to the appropriate provider implementation."""
    
    def __init__(self, provider_type: str = "azure"):
        if provider_type == "azure":
            self.provider: LLMProvider = AzureProvider()
        elif provider_type == "chutes":
            self.provider = ChutesProvider()
        else:
            raise ValueError(f"Unsupported provider_type: {provider_type}")

    async def stream_chat(
        self, request: LLMRequest
    ) -> AsyncGenerator[LLMChunk, None]:
        async for chunk in self.provider.stream_chat(request):
            yield chunk

    async def complete_chat(self, request: LLMRequest) -> LLMResponse:
        return await self.provider.complete_chat(request)
