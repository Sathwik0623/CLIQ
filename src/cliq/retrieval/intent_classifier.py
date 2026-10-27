#!/usr/bin/env python3
"""
intent_classifier.py — Detect whether input is a command query or natural-language intent.
"""

from __future__ import annotations

import re

# Common Cisco CLI command verbs. When input starts with one, it's likely an exact/prefix query.
_CISCO_VERBS = frozenset(
    {
        "show",
        "display",
        "configure",
        "config",
        "set",
        "enable",
        "disable",
        "clear",
        "debug",
        "no",
        "exit",
        "end",
        "reload",
        "shutdown",
        "ip",
        "ipv6",
        "access-list",
        "line",
        "interface",
        "router",
        "vlan",
        "route",
        "neighbor",
        "version",
    }
)

# Heuristics: tokens that suggest natural-language intent rather than a command.
_INTENT_MARKERS = frozenset(
    {
        "how",
        "what",
        "where",
        "when",
        "why",
        "show",  # "show me ...", "can you show ..."
        "find",
        "get",
        "list",
        "display",
        "check",
        "verify",
        "confirm",
        "enable",
        "disable",
        "configure",
        "set",
        "use",
        "want",
        "need",
        "help",
    }
)


def classify_input(text: str) -> str:
    """
    Classify input as either 'exact' (command-like) or 'intent' (natural language).

    Returns: 'exact' or 'intent'.
    """
    text = text.strip().lower()
    if not text:
        return "exact"

    tokens = text.split()
    first_token = tokens[0] if tokens else ""

    # Rule 0: Presence of question marks or common question phrases → intent.
    if "?" in text or any(
        phrase in text
        for phrase in ["how to", "how can", "how do i", "what is", "can you", "show me"]
    ):
        return "intent"

    # Rule 1: Natural language usually has prose markers (articles, common verbs, etc.)
    prose_markers = {"the", "a", "an", "is", "are", "be", "have", "has", "to", "for"}
    if any(token in prose_markers for token in tokens[:3]):
        return "intent"

    # Rule 2: If first token is a known Cisco verb, it's a command query.
    if first_token in _CISCO_VERBS:
        return "exact"

    # Rule 3: Very short tokens are often commands (e.g., "show", "reload", "exit").
    if len(first_token) <= 3 and first_token.isalpha():
        return "exact"

    # Rule 4: Multiple "how/what" markers in first 3 tokens → intent.
    marker_count = sum(1 for token in tokens[:3] if token in _INTENT_MARKERS)
    if marker_count >= 2:
        return "intent"

    # Rule 5: Input looks like a command (starts with letters + spaces, no prose words)
    # If it's hyphenated, has brackets, or looks structured → command.
    if "[" in text or "]" in text or "-" in first_token:
        return "exact"

    # Default: ambiguous input; prefer intent for flexibility.
    return "intent"
