"""
CLIQ AI Provider Abstraction Layer (Phase V).

Defines the common provider interface, message schemas, streaming contracts,
and token usage telemetry. All LLM providers (Meta, OpenAI, Gemini, Anthropic,
Local) implement this contract, ensuring complete independence between the UI,
orchestrator, and inference engine.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional


class ChatRole(str, enum.Enum):
    SYSTEM = "system"
    DEVELOPER = "developer"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class ChatMessage:
    role: ChatRole | str
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "role": self.role.value if isinstance(self.role, ChatRole) else str(self.role),
            "content": self.content,
        }
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d


@dataclass
class ModelInfo:
    id: str
    name: str
    provider: str
    description: str
    context_window: int = 128000
    supports_streaming: bool = True
    supports_tools: bool = True
    is_default: bool = False
    available: bool = True


@dataclass
class ChatUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class StreamChunk:
    delta: str = ""
    is_finished: bool = False
    finish_reason: Optional[str] = None
    usage: Optional[ChatUsage] = None
    model: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ChatResponse:
    content: str
    model: str
    provider: str
    finish_reason: str = "stop"
    usage: Optional[ChatUsage] = None


class AIProvider(abc.ABC):
    """Abstract base class for all CLIQ AI Copilot providers."""

    @property
    @abc.abstractmethod
    def provider_id(self) -> str:
        """Unique identifier for the provider (e.g. 'meta', 'openai')."""
        ...

    @property
    @abc.abstractmethod
    def display_name(self) -> str:
        """Human-readable provider name."""
        ...

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True if the provider has valid credentials / environment setup."""
        ...

    @abc.abstractmethod
    def list_models(self) -> List[ModelInfo]:
        """Return the list of supported models for this provider."""
        ...

    @abc.abstractmethod
    def get_default_model(self) -> str:
        """Return the default model identifier for this provider."""
        ...

    @abc.abstractmethod
    async def chat_stream(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream chat completions token by token using an async generator."""
        ...

    @abc.abstractmethod
    async def chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ) -> ChatResponse:
        """Non-streaming complete chat invocation."""
        ...



# Canonical alias per architectural specification
BaseAIProvider = AIProvider
