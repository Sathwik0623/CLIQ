#!/usr/bin/env python3
"""
taxonomy.py — PDF → tree-branch mapping & capability gating for the CLIQ KB.

Track A: ingest IOS-XE command-reference PDFs into a *tree-structured,
capability-tagged* knowledge base. This module is the single source of truth
for two decisions made at ingestion time:

  1. **Branch placement** — which ``category`` (top-level topic) and ``protocol``
     (specific topic) a PDF maps to. See :func:`classify_pdf`.
  2. **Capability gating** — the ``capability_req`` and, critically, the
     ``applies_to`` set that the resolver later uses to PRUNE branches per
     device. The one collision-risk branch is ``lan-switching`` — it must be
     tagged ``[l2-switch, l3-switch]`` so it is NEVER surfaced on a pure router.

Core principle (enforced downstream, tagged here): the KB is **enrichment
only**. Every node ingested from these PDFs is platform-independent (``PI``),
sourced from the doc set (``source="kb"``); the live device always wins.
``PD`` (platform-dependent) is reserved and is NEVER populated from these PDFs —
PD commands come from the device ``?``-help only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Capability classes a device may belong to. ``all`` is a sentinel meaning the
# node applies to every class (debug / mgmt / security / qos / system).
CAP_ALL: list[str] = ["all"]
CAP_ROUTER_L3: list[str] = ["router", "l3-switch"]
CAP_LAN: list[str] = ["l2-switch", "l3-switch"]   # GATED: never a pure router

#: category -> (capability_req, applies_to). The authoritative taxonomy table.
CATEGORY_CAPABILITY: dict[str, tuple[str, list[str]]] = {
    "ip-routing":    ("routing",  CAP_ROUTER_L3),
    "ip-multicast":  ("routing",  CAP_ROUTER_L3),
    "ip-services":   ("services", CAP_ROUTER_L3),
    "mpls":          ("routing",  CAP_ROUTER_L3),
    "lan-switching": ("switching", CAP_LAN),       # the collision-risk branch
    "debug":         ("none",     CAP_ALL),
    "network-mgmt":  ("mgmt",     CAP_ALL),
    "security":      ("security", CAP_ALL),
    "qos":           ("qos",      CAP_ALL),
    "system-mgmt":   ("mgmt",     CAP_ALL),
    "wan":           ("routing",  CAP_ROUTER_L3),
}

#: Ingestion priority (Part 6) — lower number = ingest earlier. Used only to
#: order a batch run; it does not affect tagging.
INGESTION_PRIORITY: list[str] = [
    "ip-routing",     # OSPF/BGP/EIGRP first (demo focus)
    "debug",
    "ip-services",
    "ip-multicast",
    "mpls",
    "qos",
    "security",
    "network-mgmt",
    "system-mgmt",
    "wan",            # last
    "lan-switching",  # gated; ingest but never surfaced on a pure router
]


@dataclass
class PdfClassification:
    """The tree-branch + capability tags assigned to one PDF."""

    category: str = ""
    protocol: str = ""
    capability_req: str = ""
    applies_to: list[str] = field(default_factory=list)
    command_type: str = "PI"          # these PDFs are always PI
    os_family: str = "ios-xe"
    source: str = "kb"
    skip: bool = False                # True for out-of-scope docs (voice/wireless)
    skip_reason: str = ""
    confident: bool = True            # False when matched only a generic fallback

    @property
    def priority(self) -> int:
        try:
            return INGESTION_PRIORITY.index(self.category)
        except ValueError:
            return len(INGESTION_PRIORITY)


# ---------------------------------------------------------------------------
# Out-of-scope documents — skipped entirely (not relevant to a C8k routing demo)
# ---------------------------------------------------------------------------
#: (keyword, reason). Matched first, before any inclusion rule.
SKIP_RULES: tuple[tuple[str, str], ...] = (
    ("cube", "voice (CUBE) — out of scope"),
    ("voice", "voice/video — out of scope"),
    ("video", "voice/video — out of scope"),
    ("telephony", "voice/telephony — out of scope"),
    ("unified-communications", "unified comms — out of scope"),
    ("wireless", "wireless/mobility — out of scope"),
    ("mobility", "wireless/mobility — out of scope"),
    ("wlan", "wireless LAN — out of scope"),
    ("wifi", "wireless — out of scope"),
)

# ---------------------------------------------------------------------------
# Inclusion rules — ordered, most specific first. Each: (keyword, category,
# protocol). Matching is token/normalised-substring based (see _matches).
# ---------------------------------------------------------------------------
PROTOCOL_RULES: tuple[tuple[str, str, str], ...] = (
    # ---- ip-routing -----------------------------------------------------
    ("ospf", "ip-routing", "ospf"),
    ("bgp", "ip-routing", "bgp"),
    ("eigrp", "ip-routing", "eigrp"),
    ("isis", "ip-routing", "isis"),
    ("is-is", "ip-routing", "isis"),
    ("rip", "ip-routing", "rip"),
    ("lisp", "ip-routing", "lisp"),
    ("nhrp", "ip-routing", "nhrp"),
    ("odr", "ip-routing", "odr"),
    ("performance-routing", "ip-routing", "performance-routing"),
    ("pfr", "ip-routing", "performance-routing"),
    ("ip-routing", "ip-routing", "pi-routing"),
    ("iprouting", "ip-routing", "pi-routing"),
    ("routing-protocol", "ip-routing", "pi-routing"),
    # ---- ip-multicast ---------------------------------------------------
    ("pim", "ip-multicast", "pim"),
    ("igmp", "ip-multicast", "igmp"),
    ("multicast", "ip-multicast", "multicast"),
    ("mcast", "ip-multicast", "multicast"),
    # ---- mpls -----------------------------------------------------------
    ("mpls", "mpls", "mpls"),
    ("evn", "mpls", "evn"),
    # ---- ip-services ----------------------------------------------------
    ("hsrp", "ip-services", "fhrp"),
    ("vrrp", "ip-services", "fhrp"),
    ("glbp", "ip-services", "fhrp"),
    ("fhrp", "ip-services", "fhrp"),
    ("dhcp", "ip-services", "ip-addressing"),
    ("nat", "ip-services", "ip-addressing"),
    ("ip-addressing", "ip-services", "ip-addressing"),
    ("addressing", "ip-services", "ip-addressing"),
    ("ipv6", "ip-services", "ipv6"),
    ("ip-application", "ip-services", "ip-app-services"),
    ("app-services", "ip-services", "ip-app-services"),
    ("ip-switching", "ip-services", "ip-switching"),
    ("ip-services", "ip-services", "ip-app-services"),
    ("ipservices", "ip-services", "ip-app-services"),
    # ---- lan-switching (GATED) -----------------------------------------
    ("spanning-tree", "lan-switching", "spanning-tree"),
    ("spanningtree", "lan-switching", "spanning-tree"),
    ("vlan", "lan-switching", "vlan"),
    ("trunk", "lan-switching", "trunk"),
    ("lan-switching", "lan-switching", "vlan"),
    ("lanswitching", "lan-switching", "vlan"),
    ("layer2", "lan-switching", "vlan"),
    # ---- debug ----------------------------------------------------------
    ("debug", "debug", "debug"),
    # ---- network-mgmt ---------------------------------------------------
    ("flexible-netflow", "network-mgmt", "flexible-netflow"),
    ("flexnetflow", "network-mgmt", "flexible-netflow"),
    ("netflow", "network-mgmt", "netflow"),
    ("snmp", "network-mgmt", "snmp"),
    ("cdp", "network-mgmt", "cdp"),
    ("ipsla", "network-mgmt", "ipsla"),
    ("ip-sla", "network-mgmt", "ipsla"),
    ("interface-hardware", "network-mgmt", "interface-hw"),
    ("interface-hw", "network-mgmt", "interface-hw"),
    ("interfaces-hardware", "network-mgmt", "interface-hw"),
    ("network-management", "network-mgmt", "network-mgmt"),
    # ---- security -------------------------------------------------------
    ("access-list", "security", "acl"),
    ("acl", "security", "acl"),
    ("crypto", "security", "crypto"),
    ("ipsec", "security", "vpn"),
    ("vpn", "security", "vpn"),
    ("security", "security", "security"),
    # ---- qos ------------------------------------------------------------
    ("nbar", "qos", "nbar"),
    ("qos", "qos", "qos"),
    # ---- system-mgmt ----------------------------------------------------
    ("configuration-fundamentals", "system-mgmt", "config-fundamentals"),
    ("config-fundamentals", "system-mgmt", "config-fundamentals"),
    ("fundamentals", "system-mgmt", "config-fundamentals"),
    ("basic-system", "system-mgmt", "basic-system-mgmt"),
    ("system-management", "system-mgmt", "basic-system-mgmt"),
    ("software-activation", "system-mgmt", "software-activation"),
    # ---- wan ------------------------------------------------------------
    ("broadband", "wan", "broadband"),
    ("carrier-ethernet", "wan", "carrier-ethernet"),
    ("atm", "wan", "atm"),
    ("wan", "wan", "wan"),
)

#: Keywords that are too short / collision-prone to match as a bare substring.
#: For these we require a whole-token (word-boundary) match in the filename.
_TOKEN_ONLY: frozenset[str] = frozenset(
    {"rip", "nat", "odr", "pfr", "evn", "cdp", "acl", "vpn", "qos", "atm",
     "wan", "pim", "snmp", "vlan", "ipv6", "mpls", "nbar"}
)


def _normalise(filename: str) -> tuple[set[str], str]:
    """Return (tokens, flat) for a filename.

    ``tokens`` are alphanumeric words split on any non-alphanumeric separator;
    ``flat`` is the lower-cased filename with all separators removed (so
    "ip-routing" and "iprouting" both match the "iprouting" form).
    """
    stem = re.sub(r"\.(pdf|txt|docx)$", "", filename.strip().lower())
    spaced = re.sub(r"[^a-z0-9]+", " ", stem)
    tokens = set(spaced.split())
    flat = spaced.replace(" ", "")
    return tokens, flat


def _matches(keyword: str, tokens: set[str], flat: str) -> bool:
    kw = keyword.replace("-", "").replace("_", "")
    if keyword in tokens or kw in tokens:
        return True
    if kw in _TOKEN_ONLY:
        return False                      # word-boundary only (already checked)
    return len(kw) >= 4 and kw in flat


def classify_pdf(
    filename: str,
    *,
    category: Optional[str] = None,
    protocol: Optional[str] = None,
) -> PdfClassification:
    """Classify a PDF filename into its tree branch + capability tags.

    Explicit ``category`` / ``protocol`` overrides win over auto-detection so an
    operator can guarantee correct placement for cryptically-named docs. When a
    category is known but its capability mapping is requested, the authoritative
    :data:`CATEGORY_CAPABILITY` table supplies ``capability_req`` / ``applies_to``
    — so gating is ALWAYS derived from the category, never guessed per-file.
    """
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    tokens, flat = _normalise(base)

    # 1) Out-of-scope skip (unless an explicit category forces ingestion).
    if category is None:
        for kw, reason in SKIP_RULES:
            if _matches(kw, tokens, flat):
                return PdfClassification(skip=True, skip_reason=reason)

    # 2) Resolve category/protocol.
    confident = True
    if category is None:
        category, protocol = _auto_category(tokens, flat, protocol)
        if category is None:
            return PdfClassification(
                skip=True,
                skip_reason="unrecognised topic — tag manually (path:category:protocol)",
                confident=False,
            )
    elif protocol is None:
        # Category given but no protocol — try to refine, else use category name.
        _, protocol = _auto_category(tokens, flat, None)
        if protocol is None:
            protocol = category
            confident = False

    if category not in CATEGORY_CAPABILITY:
        return PdfClassification(
            skip=True,
            skip_reason=f"unknown category '{category}'",
            confident=False,
        )

    capability_req, applies_to = CATEGORY_CAPABILITY[category]
    return PdfClassification(
        category=category,
        protocol=protocol or category,
        capability_req=capability_req,
        applies_to=list(applies_to),
        confident=confident,
    )


def _auto_category(
    tokens: set[str], flat: str, protocol: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Match the first inclusion rule; returns (category, protocol) or (None, None)."""
    for kw, cat, proto in PROTOCOL_RULES:
        if _matches(kw, tokens, flat):
            return cat, protocol or proto
    return None, None


def applies_to_for(category: str) -> list[str]:
    """Authoritative ``applies_to`` for a category (capability gating)."""
    return list(CATEGORY_CAPABILITY.get(category, ("none", CAP_ALL))[1])


def is_gated_to_switching(applies_to: list[str]) -> bool:
    """True if a node is LAN-switching-gated (excluded from a pure router)."""
    return "router" not in applies_to and "all" not in applies_to


# ---------------------------------------------------------------------------
# Per-command protocol derivation (multi-family branches).
#
# Single-topic PDFs (ospf/bgp/eigrp) map one PDF → one protocol. The LAN-switching
# command reference, however, is a SINGLE doc mixing several families (vlan,
# spanning-tree, trunk, etherchannel, switchport, vtp, mac-address-table). To keep
# protocol sub-branches meaningful — so ``tree.lookup(protocol_hint="spanning-tree")``
# returns STP commands rather than one flat "vlan" bucket — each command is mapped
# to its own family here. Order is most-specific-first; matching strips a leading
# show/no/default/do so "show spanning-tree" classifies like "spanning-tree".
# ---------------------------------------------------------------------------
COMMAND_FAMILY_RULES: tuple[tuple[str, str], ...] = (
    ("spanning-tree", "spanning-tree"),
    ("spanning", "spanning-tree"),
    ("etherchannel", "etherchannel"),
    ("port-channel", "etherchannel"),
    ("channel-group", "etherchannel"),
    ("channel-protocol", "etherchannel"),
    ("lacp", "etherchannel"),
    ("pagp", "etherchannel"),
    ("switchport", "switchport"),
    ("vtp", "vtp"),
    ("trunk", "trunk"),
    ("dot1q", "trunk"),
    ("dtp", "trunk"),
    ("mac-address-table", "mac-address-table"),
    ("mac address-table", "mac-address-table"),
    ("udld", "udld"),
    ("vlan", "vlan"),
)

#: Leading verbs stripped before family matching (so "show spanning-tree" → STP).
_FAMILY_LEAD_TOKENS: tuple[str, ...] = ("show ", "no ", "default ", "do ", "clear ", "debug ")


def derive_command_protocol(command_path: str, fallback: str) -> str:
    """Derive a per-command protocol within a multi-family branch.

    Maps one command (e.g. ``spanning-tree mode rapid-pvst``) to its family
    protocol (``spanning-tree``), falling back to the PDF-level ``fallback`` when
    no family keyword matches. Used for the LAN-switching branch so each command
    lands under its own protocol sub-branch (vlan / spanning-tree / trunk /
    etherchannel / switchport / vtp / mac-address-table) instead of one bucket.
    """
    norm = " ".join((command_path or "").lower().split())
    if not norm:
        return fallback
    body = norm
    for lead in _FAMILY_LEAD_TOKENS:
        if body.startswith(lead):
            body = body[len(lead):].strip()
            break
    for kw, proto in COMMAND_FAMILY_RULES:
        if kw in body or kw in norm:
            return proto
    return fallback


#: Categories whose commands span multiple families → derive protocol per command.
_MULTI_FAMILY_CATEGORIES: frozenset[str] = frozenset({"lan-switching"})


def protocol_for_command(category: str, command_path: str, pdf_protocol: str) -> str:
    """Return the protocol for one command, deriving per-command for multi-family
    branches (lan-switching) and using the PDF-level protocol otherwise."""
    if category in _MULTI_FAMILY_CATEGORIES:
        return derive_command_protocol(command_path, pdf_protocol)
    return pdf_protocol
