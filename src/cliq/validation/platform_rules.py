#!/usr/bin/env python3
"""
platform_rules.py — Device-capability classification (ADVISORY, not a gate).

The device is the source of truth for what is valid: if the live device's
``?``-help lists a command or the device accepts it (no ``% Invalid input`` /
``% Unrecognized``), the command is VALID and must NEVER be blocked by a
hardcoded table here.

Historically this module hard-blocked commands by platform (e.g. ``vlan`` on a
"router"), which wrongly rejected commands the device actually accepts — the
Cat8000v (IOS XE) both routes AND switches, so ``show vlan brief`` is valid on
it. That capability gate has been removed: :func:`is_valid_for_platform` is now
permissive. The prefix tables below are retained ONLY to classify a command's
capability (routing / switching / both) for SOFT suggestion ranking — they never
reject anything.
"""

from __future__ import annotations

import re

# Platform definitions (kept for reference; no longer used to gate validity).
VALID_PLATFORMS = frozenset({"iosxe", "iosxr", "iosv", "iosvl2"})

# Capability tokens.
_ROUTING = "routing"
_SWITCHING = "switching"
_BOTH = frozenset({_ROUTING, _SWITCHING})

# Command prefixes that indicate a SWITCHING-oriented command (for ranking).
SWITCH_ONLY_COMMAND_PREFIXES = frozenset({
    "vlan",                 # VLAN configuration
    "switchport",           # switchport commands
    "spanning-tree",        # STP (bridge technology)
    "mac address-table",    # MAC table learning
    "port-channel",         # LAG/EtherChannel
    "storm-control",        # broadcast storm control
    "errdisable",           # error disabled interface state
    "etherchannel",         # older etherchannel commands
})

# Command prefixes that indicate a ROUTING-oriented command (for ranking).
ROUTER_ONLY_COMMAND_PREFIXES = frozenset({
    "ip ospf",              # OSPF routing
    "ip eigrp",             # EIGRP routing
    "router ospf",          # OSPF router configuration
    "router eigrp",         # EIGRP router configuration
    "router bgp",           # BGP routing
    "redistribute",         # route redistribution
    "ip route",             # static routing
    "ip routing",           # global routing enable
    "ip multicast-routing", # multicast routing (primarily router-focused)
    "ip pim",               # PIM (multicast)
    "bgp",                  # BGP keywords (when used for routing)
})

# Commands more common on routers (still only used for ranking).
ROUTER_PREFERRED_PREFIXES = frozenset({
    "policy-map",           # QoS policy
    "class-map",            # QoS class
    "bandwidth",            # link bandwidth config
    "priority",             # QoS priority queue
})

_ROUTING_PREFIXES = ROUTER_ONLY_COMMAND_PREFIXES | ROUTER_PREFERRED_PREFIXES


def is_valid_for_platform(command: str, platform: str) -> tuple[bool, str]:
    """Capability validity check — now ALWAYS permissive (advisory-only).

    The device is the source of truth, so CLIQ never hard-blocks a command on a
    hardcoded platform-capability assumption. This always returns ``(True, "")``.
    Genuine *danger* commands are handled separately by the safety blocklist
    (see :mod:`cliq.validation.blocklist`), which is unchanged. Truly invalid
    commands are surfaced by the device's own error (``% Invalid input`` /
    ``% Unrecognized``), not by a fabricated "not supported" claim.
    """
    return True, ""


def command_capability(command: str) -> str:
    """Classify a command as ``'routing'``, ``'switching'`` or ``'both'``.

    Used only for SOFT suggestion ranking — never to block. A leading ``do`` or
    ``show`` is ignored so e.g. ``show vlan brief`` classifies like ``vlan``.
    """
    norm = (command or "").strip().lower()
    if norm.startswith("do "):
        norm = norm[3:].strip()
    body = norm
    if body.startswith("show "):
        body = body[5:].strip()
    for prefix in SWITCH_ONLY_COMMAND_PREFIXES:
        if body.startswith(prefix) or norm.startswith(prefix):
            return _SWITCHING
    for prefix in _ROUTING_PREFIXES:
        if body.startswith(prefix) or norm.startswith(prefix):
            return _ROUTING
    return "both"


def platform_capabilities(platform: str) -> frozenset[str]:
    """Best-effort capability set for a platform/node type.

    Returns a subset of ``{'routing', 'switching'}``. Crucially, this does NOT
    assume a platform maps to a single role: modern IOS XE platforms (Cat8000v /
    C8000v / CSR / ISR) both route AND switch. When the platform is unknown, the
    result is permissive (both) so the device — not this table — decides.
    """
    p = (platform or "").lower().strip()
    if not p or p == "unknown":
        return _BOTH
    # Pure layer-2 switch images.
    if "iosvl2" in p or "nexus" in p or "cat9k" in p or "cat3" in p or "cat2" in p:
        return frozenset({_SWITCHING})
    # Pure routers.
    if "iosxr" in p or "asr9" in p:
        return frozenset({_ROUTING})
    if p == "iosv" or p.startswith("iosv-") or p.startswith("iosv_"):
        return frozenset({_ROUTING})
    # Modern IOS XE platforms route AND switch.
    if any(h in p for h in ("iosxe", "cat8000", "cat8k", "c8000", "c8k", "csr", "isr", "cat9")):
        return _BOTH
    # Unknown specific platform → permissive.
    return _BOTH


def command_suits_platform(command: str, platform: str) -> bool:
    """True when a command's capability matches the platform (soft ranking).

    Always True for capability-neutral ('both') commands. Never used to block —
    only to rank capability-matching suggestions first.
    """
    cap = command_capability(command)
    if cap == "both":
        return True
    return cap in platform_capabilities(platform)


def get_device_type(platform: str) -> str:
    """Return a human label for a platform's capability.

    ``'router'`` (routing only), ``'switch'`` (switching only), ``'both'``
    (routes AND switches, e.g. Cat8000v), or ``'unknown'``.
    """
    p = (platform or "").lower().strip()
    if not p or p == "unknown":
        return "unknown"
    caps = platform_capabilities(platform)
    if caps == _BOTH:
        return "both"
    if _SWITCHING in caps:
        return "switch"
    return "router"


# ---------------------------------------------------------------------------
# PID (Product ID) → capability, for REAL hardware reached over an SSH console.
#
# A CML node type (``iosv`` / ``iosvl2``) is not present on real gear; instead
# the device advertises a PID in ``show version`` (e.g. ``ISR4331/K9``). We map
# that PID prefix to a capability class so VLAN/switching commands are gated
# OFF on a pure router (ISR/ASR/CSR-family) and switching platforms keep L2.
#
# ADVISORY ONLY — like everything in this module the mapping is used for SOFT
# suggestion ranking / capability gating, never to block: the live device's own
# ``?``-help and ``% Invalid`` remain the source of truth. Unknown PIDs return
# the permissive ``both`` set so the device decides.
# ---------------------------------------------------------------------------

# PID families that are PURE ROUTERS (no L2/switching command surface).
_PID_ROUTER_PREFIXES: tuple[str, ...] = (
    "ISR4",       # ISR 4000 series (ISR4331, ISR4431, ISR4451, ...)
    "ISR1",       # ISR 1000 series
    "ASR1",       # ASR 1000 series
    "ASR9",       # ASR 9000 (IOS-XR)
    "CSR1000",    # Cloud Services Router
    "C8000V",     # Catalyst 8000V (virtual router)
    "C8200",      # Catalyst 8200 edge router
    "C8300",      # Catalyst 8300 edge router
    "C8500",      # Catalyst 8500 edge router
    "C1111",      # ISR 1100 integrated router
    "C1112",
    "C1113",
    "C1116",
    "C1117",
    "C1121",
    "C1131",
    "CISCO39",    # 3900 ISR G2
    "CISCO29",    # 2900 ISR G2
    "CISCO19",    # 1900 ISR G2
)

# PID families that are SWITCHES (L2 / L2+L3). Kept coarse; these gate routing
# advisories appropriately while the device stays authoritative.
_PID_SWITCH_PREFIXES: tuple[str, ...] = (
    "WS-C",       # classic Catalyst (WS-C2960, WS-C3560, WS-C3750, ...)
    "C9200",      # Catalyst 9200
    "C9300",      # Catalyst 9300
    "C9400",      # Catalyst 9400
    "C9500",      # Catalyst 9500
    "C9600",      # Catalyst 9600
    "C3560",
    "C3750",
    "C2960",
    "N9K",        # Nexus 9000
    "N3K",        # Nexus 3000
    "N7K",        # Nexus 7000
    "N5K",        # Nexus 5000
)

# Extract the PID from a ``show version`` body. IOS-XE renders a line such as
#   ``Processor board ID FDO12345678``  (serial — NOT the PID) and
#   ``cisco ISR4331/K9 (1RU) processor with ...`` OR a
#   ``PID: ISR4331/K9`` line in the ``show inventory``-style tail. We match the
# explicit ``PID:`` form first, then the classic ``cisco <MODEL> ... processor``
# banner, so either output shape yields the model.
_PID_EXPLICIT_RE = re.compile(r"(?im)^\s*PID:\s*([A-Za-z0-9][A-Za-z0-9/\-]+)")
_PID_BANNER_RE = re.compile(
    r"(?im)^\s*cisco\s+([A-Za-z0-9][A-Za-z0-9/\-]+)\b.*?\bprocessor\b"
)
# ``license udi pid ISR4331/K9 sn FDO...`` — present in ``show running-config``
# (and ``show license udi``); a reliable fallback when a trimmed ``show version``
# lacks the banner/PID line.
_PID_LICENSE_UDI_RE = re.compile(
    r"(?im)\blicense\s+udi\s+pid\s+([A-Za-z0-9][A-Za-z0-9/\-]+)"
)


def parse_pid_from_show_version(output: str) -> str:
    """Extract the device PID (e.g. ``ISR4331/K9``) from ``show version`` text.

    Returns the raw PID string, or ``""`` when none can be found. Prefers an
    explicit ``PID:`` line (present in ``show version`` on newer IOS-XE and in
    ``show inventory``), then falls back to the classic ``cisco <MODEL> …
    processor`` banner line, and finally to a ``license udi pid …`` line if the
    banner is absent from a trimmed capture.
    """
    text = output or ""
    m = _PID_EXPLICIT_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _PID_BANNER_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _PID_LICENSE_UDI_RE.search(text)
    if m:
        return m.group(1).strip()
    return ""


def parse_pid_from_running_config(output: str) -> str:
    """Extract the PID from a ``show running-config`` body via ``license udi pid``.

    IOS-XE embeds the product ID in the running config as
    ``license udi pid ISR4331/K9 sn <serial>``. Used as a fallback identify path
    when ``show version`` parsing yields nothing. Returns ``""`` if not present.
    """
    m = _PID_LICENSE_UDI_RE.search(output or "")
    return m.group(1).strip() if m else ""



def capability_from_pid(pid: str) -> frozenset[str]:
    """Map a device PID to its capability set (subset of routing/switching).

    ADVISORY ONLY. Pure-router PIDs (ISR/ASR/CSR/C8000-router family) return
    ``{'routing'}`` so switching commands (``vlan``, ``switchport`` …) are gated
    OFF; switch PIDs return ``{'switching'}``; anything unrecognized returns the
    permissive ``both`` set so the live device — not this table — decides.
    """
    p = (pid or "").upper().strip()
    if not p:
        return _BOTH
    for prefix in _PID_ROUTER_PREFIXES:
        if p.startswith(prefix):
            # IOS-XR 9000 family is routing-only; the rest are IOS/IOS-XE routers.
            return frozenset({_ROUTING})
    for prefix in _PID_SWITCH_PREFIXES:
        if p.startswith(prefix):
            return frozenset({_SWITCHING})
    return _BOTH


def node_type_from_pid(pid: str) -> str:
    """Return a normalized node-type token for a PID (for cache/KB keying).

    Lowercased, slash-suffix stripped (``ISR4331/K9`` → ``isr4331``) so it keys
    the persistent command cache and did-you-mean platform reconciliation
    consistently. Empty PID → ``""``.
    """
    p = (pid or "").strip()
    if not p:
        return ""
    return p.split("/", 1)[0].strip().lower()


# ---------------------------------------------------------------------------
# Mode-transition awareness (advisory).
#
# Some commands don't RUN in a mode — they TRANSITION into one. 'configure
# terminal' enters global-config. Comparing such a command against the current
# mode as if it were an ordinary command produces a misleading "requires
# 'enable'" error when the user is ALREADY in config (or a child config mode):
# re-issuing it there is a redundant no-op, not a mode error.
#
# We model this with a target-mode map + a simple mode hierarchy so the advisor
# can recognise "already at or beyond the target mode" and stay quiet / hint
# gently, while still letting the command work as a valid forward transition
# from exec/enable.
# ---------------------------------------------------------------------------

# A mode-transition command mapped to the mode it ENTERS. Keyed by the canonical
# (expanded) command text; abbreviations resolve to these before lookup.
_MODE_TRANSITION_TARGETS: dict[str, str] = {
    "configure terminal": "config",
}


def mode_transition_target(command: str) -> str | None:
    """Return the mode a mode-transition command ENTERS, or ``None``.

    Tolerates a leading ``do `` and irregular whitespace; matches on the
    canonical expanded command text (e.g. ``configure terminal``).
    """
    norm = " ".join((command or "").strip().lower().split())
    if norm.startswith("do "):
        norm = norm[3:].strip()
    return _MODE_TRANSITION_TARGETS.get(norm)


def is_at_or_below_mode(current_mode: str, target_mode: str) -> bool:
    """True when ``current_mode`` IS the target mode or a descendant of it.

    Only ``config`` has descendants in the CLIQ mode model (``config-if``,
    ``config-subif``, ``config-router``, ``config-line``, ``config-vlan`` …),
    all of which render as ``config-*``.
    """
    cur = (current_mode or "").strip().lower()
    tgt = (target_mode or "").strip().lower()
    if cur == tgt:
        return True
    if tgt == "config":
        return cur.startswith("config-")
    return False


def is_redundant_mode_transition(command: str, current_mode: str) -> bool:
    """True when re-issuing a mode-transition command is a redundant no-op.

    e.g. ``configure terminal`` (or ``conf t`` / ``config t``) while ALREADY in
    ``config`` / ``config-if`` / ``config-subif``. In that case the advisor must
    NOT emit "requires 'enable'" — the user is already at/beyond the target mode.
    """
    target = mode_transition_target(command)
    return target is not None and is_at_or_below_mode(current_mode, target)

