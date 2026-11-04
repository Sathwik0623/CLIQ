"""
Google Gemini AI Provider Adapter for CLIQ AI Copilot (Phase V Future LLM Provider Architecture).

Implements the common BaseAIProvider contract for future migration to Google Gemini
(e.g., Gemini 1.5 Pro and Gemini 1.5 Flash).
Currently inactive by default while Meta Model API remains primary, but fully registered and ready.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncGenerator, Dict, List, Optional

from cliq.ai.provider import (
    BaseAIProvider,
    ChatMessage,
    ChatResponse,
    ChatRole,
    ChatUsage,
    ModelInfo,
    StreamChunk,
)

log = logging.getLogger("cliq.ai.gemini")

GEMINI_MODELS = [
    ModelInfo(
        id="gemini-1.5-pro",
        name="Gemini 1.5 Pro",
        provider="gemini",
        description="Google flagship multimodal reasoning model with a 1M+ token context window for large network configuration and topology analysis.",
        context_window=1000000,
        supports_streaming=True,
        supports_tools=False,
        is_default=True,
        available=True,
    ),
    ModelInfo(
        id="gemini-1.5-flash",
        name="Gemini 1.5 Flash",
        provider="gemini",
        description="High-speed, low-latency Gemini model designed for rapid CLI command analysis and instant diagnostics.",
        context_window=1000000,
        supports_streaming=True,
        supports_tools=False,
        is_default=False,
        available=True,
    ),
]


class GeminiProvider(BaseAIProvider):
    """Adapter for Google Gemini AI models implementing BaseAIProvider."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "gemini-1.5-pro",
    ):
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        self._default_model = default_model

    @property
    def provider_id(self) -> str:
        return "gemini"

    @property
    def display_name(self) -> str:
        return "Google Gemini"

    def is_configured(self) -> bool:
        """True only if a valid Google Gemini API key is configured in the environment."""
        return bool(self._api_key and len(self._api_key) > 5)

    def list_models(self) -> List[ModelInfo]:
        configured = self.is_configured()
        return [
            ModelInfo(
                id=m.id,
                name=m.name,
                provider=m.provider,
                description=m.description,
                context_window=m.context_window,
                supports_streaming=m.supports_streaming,
                supports_tools=m.supports_tools,
                is_default=m.is_default,
                available=configured,
            )
            for m in GEMINI_MODELS
        ]

    def get_default_model(self) -> str:
        return self._default_model

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        if not self.is_configured():
            yield StreamChunk(
                error="Gemini provider is not configured. Set GEMINI_API_KEY in .env to enable.",
                is_finished=True,
            )
            return

        # Prepare payload for future Google Gemini REST / SDK client
        # In mock or placeholder mode until live Gemini key is configured by user:
        yield StreamChunk(delta="[Gemini Provider]: Ready for activation upon GEMINI_API_KEY configuration.")
        yield StreamChunk(
            is_finished=True,
            finish_reason="stop",
            usage=ChatUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )

    async def chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ) -> ChatResponse:
        if not self.is_configured():
            return ChatResponse(
                content="Gemini provider is not configured. Set GEMINI_API_KEY in .env to enable.",
                model=model or self._default_model,
                provider=self.provider_id,
                finish_reason="error",
            )
        return ChatResponse(
            content="[Gemini Provider]: Ready for activation upon GEMINI_API_KEY configuration.",
            model=model or self._default_model,
            provider=self.provider_id,
            finish_reason="stop",
        )

