#!/usr/bin/env python3
"""
candidate_promotion.py — Two-stage promotion for advisory ingestion candidates.

Product Command-Reference commands are ingested as ADVISORY candidates
(``source='product_cr'``, ``candidate_status='candidate'`` — "from Cisco
Cat9500 CR — verify on platform"). They only become a trusted suggestion once
the LIVE DEVICE accepts the equivalent command, mirroring the existing
device-accepted-command flow (``cliq.memory.command_cache`` /
``cliq.intelligence.command_template``).

This module is the shared promotion step: given a command the device just
accepted, value-strip it to a template (the same vocabulary product-CR
candidates were stored under) and, on an exact match for this platform,
flip that candidate row to ``candidate_status='trusted'``.

Pure / offline — no network I/O. Safe to call from the post-Enter (not
per-keystroke) path; failures are non-fatal by design (see
:func:`promote_on_device_acceptance`).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from cliq.intelligence.command_template import extract_template


def promote_on_device_acceptance(
    store: Any,
    exec_command: str,
    platform: str,
    *,
    help_lookup: Optional[Callable[[str], Optional[list[str]]]] = None,
) -> Optional[int]:
    """Promote a matching product-CR candidate after the device accepts a command.

    ``store`` is a :class:`cliq.ingest.storage.sqlite_store.CommandStore`.
    Returns the promoted row id, or ``None`` when no pending candidate matches
    (nothing to promote — the common case, so this is not an error).
    """
    if not exec_command or not platform:
        return None
    template = extract_template(exec_command, help_lookup=help_lookup).template
    if not template:
        return None
    row = store.find_candidate_by_template(template, platform)
    if row is None:
        return None
    row_id = int(row["id"])
    return row_id if store.promote_candidate(row_id) else None
