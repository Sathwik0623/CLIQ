"""
retrieval — Phase 2 command retrieval layer.

Provides semantic + exact-match retrieval against the Phase 1 knowledge base.
"""

from cliq.retrieval.assistant import IntelligentCommandAssistant
from cliq.retrieval.query import CommandRetriever
from cliq.retrieval.schema import (
    IntelligentCommandResult,
    IntelligentResultGroup,
    IntelligentRetrievalConfig,
    IntelligentRetrievalResponse,
    MemoryRecord,
    MemoryScope,
    OutcomeStatus,
    PastResolution,
    RetrievalCandidate,
    RetrievalQuery,
    RetrievalResult,
)

__all__ = [
    "CommandRetriever",
    "IntelligentCommandAssistant",
    "IntelligentCommandResult",
    "IntelligentResultGroup",
    "IntelligentRetrievalConfig",
    "IntelligentRetrievalResponse",
    "MemoryRecord",
    "MemoryScope",
    "OutcomeStatus",
    "PastResolution",
    "RetrievalQuery",
    "RetrievalResult",
    "RetrievalCandidate",
]
