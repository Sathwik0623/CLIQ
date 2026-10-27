#!/usr/bin/env python3
"""
tree.py — Capability-gated lookup over the tree-structured KB (Track A).

The PDF ingestion layer stores every command as a *node* in the ``commands``
table, tagged with ``category`` / ``protocol`` / ``capability_req`` /
``applies_to`` / ``command_type``. :class:`KnowledgeTree` is the stable,
read-only API the resolver engine calls to retrieve those nodes — gated by the
device's capability class and (optionally) scoped to a command path or protocol.

    tree = KnowledgeTree("./kb/cliq_kb.sqlite")
    nodes = tree.lookup("router", command_path="show ip ospf neighbor")
    nodes = tree.lookup("router", protocol_hint="ospf")

Guarantees / design:
  * **Enrichment only.** Every node carries ``source="kb"`` / ``command_type="PI"``.
    The tree NEVER claims authority — the resolver treats these as advisory
    context and the live device always wins. The tree just returns tagged nodes.
  * **Capability gating.** A node is returned for a capability class only if the
    class is in its ``applies_to`` (or the node is tagged ``all``). LAN-switching
    nodes (``[l2-switch, l3-switch]``) are therefore pruned for a pure router.
  * **Read-only.** No mutation; safe to share across device tabs/threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cliq.ingest.storage.sqlite_store import CommandStore, DEFAULT_KB_DB

# Recognised device capability classes (what a resolver passes in).
CAPABILITY_CLASSES: tuple[str, ...] = ("router", "l3-switch", "l2-switch")


@dataclass
class TreeNode:
    """One capability-tagged command node returned by :class:`KnowledgeTree`."""

    command_path: str
    description: str = ""
    syntax: str = ""
    category: str = ""
    protocol: str = ""
    capability_req: str = ""
    applies_to: list[str] = field(default_factory=list)
    command_type: str = "PI"
    mode: str = "exec"
    os_family: str = "ios-xe"
    source: str = "kb"
    source_pdf: str = ""
    sqlite_id: Optional[int] = None
    vector_id: str = ""
    source_url: str = ""
    os_release: str = ""
    topic_chapter: str = ""
    candidate_status: str = ""

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _row_to_node(row: dict) -> TreeNode:
    import json

    def _loads(value, default):
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    variants = _loads(row.get("syntax_variants"), [])
    applies = _loads(row.get("applies_to"), [])
    syntax = variants[0] if variants else row.get("command", "")
    return TreeNode(
        command_path=row.get("command", ""),
        description=row.get("description", "") or "",
        syntax=syntax,
        category=row.get("category", "") or "",
        protocol=row.get("protocol", "") or "",
        capability_req=row.get("capability_req", "") or "",
        applies_to=applies,
        command_type=row.get("command_type", "PI") or "PI",
        mode=row.get("mode", "exec") or "exec",
        os_family=row.get("os_family", "ios-xe") or "ios-xe",
        source=row.get("source", "kb") or "kb",
        source_pdf=row.get("source_pdf", "") or "",
        sqlite_id=row.get("id"),
        vector_id=row.get("vector_id", "") or "",
        source_url=row.get("source_url", "") or "",
        os_release=row.get("os_release", "") or "",
        topic_chapter=row.get("topic_chapter", "") or "",
        candidate_status=row.get("candidate_status", "") or "",
    )


class KnowledgeTree:
    """Capability-gated, read-only view of the ingested command tree."""

    def __init__(
        self,
        kb_db_path: str = DEFAULT_KB_DB,
        store: Optional[CommandStore] = None,
    ):
        self._store = store or CommandStore(kb_db_path)
        self._owns_store = store is None

    def lookup(
        self,
        capability_class: Optional[str] = None,
        command_path: Optional[str] = None,
        protocol_hint: Optional[str] = None,
        *,
        category: Optional[str] = None,
        command_type: Optional[str] = None,
        prefix: bool = True,
        limit: int = 50,
    ) -> list[TreeNode]:
        """Return tree nodes for a device capability class.

        Args:
            capability_class: ``router`` | ``l3-switch`` | ``l2-switch``. When
                provided, LAN-switching nodes are pruned for a pure ``router``.
                ``None`` disables gating (returns across all classes).
            command_path: exact command, or a prefix when ``prefix`` is True
                (e.g. "show ip ospf" → "show ip ospf neighbor").
            protocol_hint: scope to a single protocol (e.g. "ospf") — no
                cross-protocol bleed.
            category: scope to a top-level topic (e.g. "ip-routing").
            command_type: typically left ``None``; pass "PI" to be explicit.
            prefix: prefix-match the command_path (default True).
            limit: max nodes returned.

        The KB is enrichment only — these nodes are advisory; the live device is
        always the source of truth.
        """
        rows = self._store.query_nodes(
            capability_class=capability_class,
            command_path=command_path,
            protocol_hint=protocol_hint,
            category=category,
            command_type=command_type,
            prefix=prefix,
            limit=limit,
        )
        return [_row_to_node(r) for r in rows]

    # -- Convenience aggregates (for ingestion reports / verification) --------
    def protocols(self) -> dict[str, int]:
        """Map of protocol -> node count (ingested nodes only)."""
        return self._store.count_by_protocol()

    def categories(self) -> dict[str, int]:
        """Map of category -> node count (ingested nodes only)."""
        return self._store.count_by_category()

    def close(self) -> None:
        if self._owns_store:
            self._store.close()
