#!/usr/bin/env python3
"""
product_cr_taxonomy.py — Product Command-Reference book-chapter -> tree-branch
mapping & capability gating for the CLIQ KB.

Cisco product-specific Command References (Catalyst 9500, IOS XE 17.16.x etc.)
are static DITA HTML organised into book chapters via a TOC (``<ul id=
"bookToc">``). This module is the single source of truth for two decisions
made at ingestion time by ``cliq.ingest.product_cr_ingest``:

  1. **Chapter classification** — is a TOC entry a *command* chapter (worth
     crawling) or intro/prose ("Using the CLI", "Preface", "New and Changed
     Information")? See :func:`classify_chapter`.
  2. **Capability gating** — the ``capability_req`` / ``applies_to`` tags a
     command chapter maps to, reusing the SAME taxonomy vocabulary as the PDF
     ingester (:mod:`cliq.ingest.taxonomy`) so both feed one gated tree.

This module never fetches anything — pure string classification only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from cliq.ingest.taxonomy import CAP_ALL, CAP_LAN, CAP_ROUTER_L3

#: category -> (capability_req, applies_to). Mirrors taxonomy.CATEGORY_CAPABILITY
#: (kept as its own table since product-CR chapter titles are worded
#: differently than PDF filenames — e.g. "Layer 2/3" spans two capability
#: classes and has no PDF-filename equivalent).
CHAPTER_CATEGORY_CAPABILITY: dict[str, tuple[str, list[str]]] = {
    "ip-routing":    ("routing",   CAP_ROUTER_L3),
    "ip-multicast":  ("routing",   CAP_ROUTER_L3),
    "ip-services":   ("services",  CAP_ROUTER_L3),
    "mpls":          ("routing",   CAP_ROUTER_L3),
    "lan-switching": ("switching", CAP_LAN),
    "security":      ("security",  CAP_ALL),
    "qos":           ("qos",       CAP_ALL),
    "network-mgmt":  ("mgmt",      CAP_ALL),
    "system-mgmt":   ("mgmt",      CAP_ALL),
    "interface-hw":  ("none",      CAP_ALL),
}

# ---------------------------------------------------------------------------
# Chapter title -> (category, protocol) — ordered, most specific first. A
# chapter title is matched case-insensitively as a substring/word match.
# ---------------------------------------------------------------------------
CHAPTER_RULES: tuple[tuple[str, str, str], ...] = (
    # ---- Routing ----------------------------------------------------
    ("ospf", "ip-routing", "ospf"),
    ("bgp", "ip-routing", "bgp"),
    ("eigrp", "ip-routing", "eigrp"),
    ("is-is", "ip-routing", "isis"),
    ("isis", "ip-routing", "isis"),
    ("rip", "ip-routing", "rip"),
    ("ip routing", "ip-routing", "pi-routing"),
    ("routing commands", "ip-routing", "pi-routing"),
    # ---- IP Multicast -------------------------------------------------
    ("multicast", "ip-multicast", "multicast"),
    ("pim", "ip-multicast", "pim"),
    ("igmp", "ip-multicast", "igmp"),
    # ---- MPLS -----------------------------------------------------------
    ("mpls", "mpls", "mpls"),
    # ---- VLAN / LAN switching / Layer 2 --------------------------------
    ("vlan", "lan-switching", "vlan"),
    ("spanning tree", "lan-switching", "spanning-tree"),
    ("spanning-tree", "lan-switching", "spanning-tree"),
    ("etherchannel", "lan-switching", "etherchannel"),
    ("switching", "lan-switching", "vlan"),
    ("layer 2", "lan-switching", "vlan"),
    ("layer2", "lan-switching", "vlan"),
    # ---- Layer 3 (routing side of a "Layer 2/3" chapter) ---------------
    ("layer 3", "ip-routing", "pi-routing"),
    ("layer3", "ip-routing", "pi-routing"),
    # ---- Security ---------------------------------------------------
    ("access control", "security", "acl"),
    ("acl", "security", "acl"),
    ("security", "security", "security"),
    ("crypto", "security", "crypto"),
    ("aaa", "security", "aaa"),
    # ---- QoS ------------------------------------------------------------
    ("qos", "qos", "qos"),
    ("quality of service", "qos", "qos"),
    # ---- Interface / Hardware ------------------------------------------
    ("interface", "interface-hw", "interface-hw"),
    ("hardware", "interface-hw", "interface-hw"),
    # ---- System management ----------------------------------------------
    ("system management", "system-mgmt", "system-mgmt"),
    ("network management", "network-mgmt", "network-mgmt"),
    ("configuration fundamentals", "system-mgmt", "config-fundamentals"),
)

#: Intro/prose chapters — never crawled for commands. Matched as a substring
#: of the (lower-cased) chapter title.
PROSE_CHAPTER_KEYWORDS: tuple[str, ...] = (
    "using the cli",
    "using the command-line interface",
    "preface",
    "overview",
    "getting started",
    "new and changed",
    "related documentation",
    "obtaining documentation",
    "communications, services, and additional information",
    "cisco open source",
    "read me first",
    "trademarks",
    "full cisco trademarks",
)


@dataclass
class ChapterClassification:
    """The tree-branch + capability tags assigned to one book chapter."""

    title: str = ""
    category: str = ""
    protocol: str = ""
    capability_req: str = ""
    applies_to: list[str] = field(default_factory=list)
    is_command_chapter: bool = True
    skip_reason: str = ""
    confident: bool = True


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def is_prose_chapter(title: str) -> bool:
    """True when ``title`` is an intro/prose chapter (skip — no commands)."""
    norm = _norm_title(title)
    if not norm:
        return True
    return any(kw in norm for kw in PROSE_CHAPTER_KEYWORDS)


def classify_chapter(title: str) -> ChapterClassification:
    """Classify a book-TOC chapter title into its tree branch + capability tags.

    Prose/intro chapters are returned with ``is_command_chapter=False`` so the
    crawler skips fetching them entirely. Unrecognised *command* chapters fall
    back to a low-confidence ``system-mgmt``/``all`` tag rather than being
    dropped — an unmapped Cisco chapter still likely holds real commands.
    """
    norm = _norm_title(title)
    if is_prose_chapter(title):
        return ChapterClassification(
            title=title, is_command_chapter=False,
            skip_reason="intro/prose chapter — no commands",
        )

    for kw, category, protocol in CHAPTER_RULES:
        if kw in norm:
            capability_req, applies_to = CHAPTER_CATEGORY_CAPABILITY.get(
                category, ("none", list(CAP_ALL))
            )
            return ChapterClassification(
                title=title, category=category, protocol=protocol,
                capability_req=capability_req, applies_to=list(applies_to),
            )

    # Unrecognised command chapter — keep (don't silently drop real commands),
    # flagged low-confidence so an operator can review/re-tag manually.
    return ChapterClassification(
        title=title, category="system-mgmt", protocol="unclassified",
        capability_req="mgmt", applies_to=list(CAP_ALL), confident=False,
    )
