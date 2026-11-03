"""
CLIQ AI Provider Registry (Phase V).

Central registry managing AI provider lifecycle, model catalogs, and active provider selection.
Supports pluggable future providers without modifying the UI or chat orchestrator.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from cliq.ai.provider import AIProvider, ModelInfo
from cliq.ai.providers.meta import MetaProvider

log = logging.getLogger("cliq.ai.registry")


class ProviderRegistry:
    """Singleton registry for AI model providers."""

    _instance: Optional[ProviderRegistry] = None

    def __init__(self):
        self._providers: Dict[str, AIProvider] = {}
        self._default_provider_id: str = "meta"
        self._register_defaults()

    @classmethod
    def get_instance(cls) -> ProviderRegistry:
        if cls._instance is None:
            cls._instance = ProviderRegistry()
        return cls._instance

    def _register_defaults(self) -> None:
        # Register Meta as the primary active provider
        meta = MetaProvider()
        self.register(meta)

        # Register Gemini as a future integration provider (behind BaseAIProvider contract)
        try:
            from cliq.ai.providers.gemini import GeminiProvider
            gemini = GeminiProvider()
            self.register(gemini)
        except Exception as exc:
            log.debug("Gemini provider registration deferred: %s", exc)

    def register(self, provider: AIProvider) -> None:
        """Register a new AI provider instance."""
        self._providers[provider.provider_id] = provider
        log.info("Registered AI provider: %s (%s)", provider.provider_id, provider.display_name)

    def get_provider(self, provider_id: Optional[str] = None) -> AIProvider:
        """Get a provider by ID or return the default provider."""
        pid = provider_id or self._default_provider_id
        if pid not in self._providers:
            raise KeyError(f"AI provider '{pid}' is not registered. Available: {list(self._providers.keys())}")
        return self._providers[pid]

    def list_providers(self) -> List[Dict[str, Any]]:
        """List metadata for all registered providers."""
        return [
            {
                "id": p.provider_id,
                "name": p.display_name,
                "is_configured": p.is_configured(),
                "default_model": p.get_default_model(),
                "is_default": p.provider_id == self._default_provider_id,
            }
            for p in self._providers.values()
        ]

    def list_all_models(self) -> List[ModelInfo]:
        """List all models across all registered providers."""
        all_models: List[ModelInfo] = []
        for p in self._providers.values():
            all_models.extend(p.list_models())
        return all_models


# Global helper functions
def get_provider(provider_id: Optional[str] = None) -> AIProvider:
    return ProviderRegistry.get_instance().get_provider(provider_id)


def list_providers() -> List[Dict[str, Any]]:
    return ProviderRegistry.get_instance().list_providers()


def list_models() -> List[ModelInfo]:
    return ProviderRegistry.get_instance().list_all_models()

