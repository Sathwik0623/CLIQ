#!/usr/bin/env python3
"""
cliq.ingest — Document-ingestion pipeline for CLIQ's knowledge layer.

Phase 1A: parse Cisco command-reference documents (PDF/DOCX/TXT) into clean,
structured command entries ready for the storage layer (SQLite + Vector DB).

Public API:
    parse_document(path, platform)  -> (list[CommandEntry], ParseReport)
    format_preview(entries, limit)  -> str
    register_reader(ext, fn)        -> plug in a new file format
    register_layout(name, cls)      -> plug in a new document layout
"""

from cliq.ingest.schema import CommandEntry, ParseReport
from cliq.ingest.readers import (
    register_reader,
    read_document,
    supported_extensions,
    ReaderError,
    UnsupportedFormatError,
    ReaderDependencyError,
)
from cliq.ingest.doc_parser import (
    parse_document,
    format_preview,
    load_parser_config,
    register_layout,
    available_layouts,
    LayoutStrategy,
    ParserConfig,
)

__all__ = [
    "CommandEntry",
    "ParseReport",
    "parse_document",
    "format_preview",
    "load_parser_config",
    "register_reader",
    "register_layout",
    "available_layouts",
    "read_document",
    "supported_extensions",
    "LayoutStrategy",
    "ParserConfig",
    "ReaderError",
    "UnsupportedFormatError",
    "ReaderDependencyError",
]

# Track A: tree-structured, capability-tagged KB from command-reference PDFs.
from cliq.ingest.taxonomy import (  # noqa: E402
    classify_pdf,
    PdfClassification,
    CATEGORY_CAPABILITY,
    INGESTION_PRIORITY,
)
from cliq.ingest.tree import KnowledgeTree, TreeNode  # noqa: E402

__all__ += [
    "classify_pdf",
    "PdfClassification",
    "CATEGORY_CAPABILITY",
    "INGESTION_PRIORITY",
    "KnowledgeTree",
    "TreeNode",
]
