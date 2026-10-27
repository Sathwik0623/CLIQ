#!/usr/bin/env python3
"""
hybrid_validator.py — device-first sub-keyword validation (ACCEPT-only).

CLIQ's cardinal rule: **the device is the source of truth.** CLIQ must never
block a command the device would accept. The static argument validator
(:mod:`cliq.intelligence.arg_validator`) is a fast, offline regex heuristic that
occasionally rejects a perfectly valid sub-keyword — e.g. it flagged
``show interface switchport`` as ``Invalid value for 'interface': 'switchport'``
even though CML accepts it (empty output is a valid success).

This hybrid layer fixes that class of false-positive. It is **ACCEPT-only**:

  * **Fast path** — a static per-head allowlist of well-known sub-keywords.
    A hit short-circuits with ``accepted=True`` and NO device round-trip (so
    we never issue a ``?``-query on the keystroke path). The allowlist may only
    accept early — it never rejects.
  * **Device fallback** — on an allowlist miss, query the device's own
    ``<prefix> ?`` help; if the token is listed there, accept it.
  * **Only the device rejects** — a token is treated as invalid solely when the
    device itself answers with ``% Invalid input`` / a ``^`` marker. Empty
    output is SUCCESS, never an error.

The functions are pure except for the injected ``help_lookup`` callable, so the
device round-trip stays unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from cliq.intelligence.learned_commands import is_accepted

# ──────────────────────────────────────────────────────────────────────────
# Static fast-path allowlist of known sub-keywords, keyed by command head.
#
# The "head" is the command token immediately preceding the questioned
# sub-keyword (e.g. for ``show interface switchport`` the head is ``interface``
# and the questioned token is ``switchport``). ACCEPT-ONLY: presence here lets
# us skip a device ``?``-query; absence NEVER rejects — the device decides.
# ──────────────────────────────────────────────────────────────────────────
_INTERFACE_SUBKEYWORDS = frozenset({
    "switchport", "status", "description", "trunk", "counters", "summary",
    "accounting", "stats", "capabilities", "transceiver", "pruning",
    "mac-address", "flowcontrol", "link", "monitor",
})

SUBKEYWORD_ALLOWLIST: dict[str, frozenset[str]] = {
    "interface": _INTERFACE_SUBKEYWORDS,
    "interfaces": _INTERFACE_SUBKEYWORDS,
    "vlan": frozenset({"brief", "id", "name", "summary", "group"}),
    "ip": frozenset({
        "interface", "route", "protocols", "ospf", "bgp", "eigrp", "arp",
        "nat", "dhcp", "access-lists", "mroute", "cef", "traffic",
    }),
    "spanning-tree": frozenset({
        "brief", "summary", "detail", "active", "root", "vlan", "interface",
    }),
    "mac": frozenset({"address-table"}),
    "show": frozenset(),  # head 'show' alone never gates a sub-keyword
}


@dataclass
class SubkeywordDecision:
    """Outcome of a hybrid sub-keyword check.

    ``accepted`` is True when the token should be allowed through WITHOUT a
    user-facing block. ``source`` records WHY for observability:
    ``allowlist`` | ``device_help`` | ``empty`` | ``device_invalid`` |
    ``unknown``. Note ``device_invalid`` / ``unknown`` still must NOT hard-block
    the live path — the device's real execution output is the final authority.
    """

    accepted: bool
    source: str
    detail: str = ""


def allowlist_accepts(head: str, token: str) -> bool:
    """True when ``token`` is a known sub-keyword for ``head`` (fast, offline)."""
    allow = SUBKEYWORD_ALLOWLIST.get((head or "").strip().lower())
    if not allow:
        return False
    return (token or "").strip().lower() in allow


def help_lists_token(token: str, help_text: str) -> bool:
    """True when the device ``?``-help page lists ``token`` as a valid option."""
    # Imported lazily to avoid a heavy import on the keystroke path.
    from cliq.intelligence.did_you_mean import parse_help_options

    options, _ = parse_help_options(help_text or "")
    tl = (token or "").strip().lower()
    if not tl:
        return False
    return any(o.lower() == tl or o.lower().startswith(tl) for o in options)


def output_is_success(output: Optional[str]) -> bool:
    """True when device output is a success — empty output counts as SUCCESS.

    Only an IOS rejection marker (``% Invalid input`` / ``% Incomplete`` / a
    ``^`` caret, via :func:`is_accepted`) is a failure.
    """
    return is_accepted(output)


def classify_subkeyword(
    head: str,
    token: str,
    help_lookup: Optional[Callable[[], str]] = None,
) -> SubkeywordDecision:
    """Decide whether a sub-keyword should be accepted early (ACCEPT-only).

    Order: allowlist fast-path → device ``?``-help fallback (only if
    ``help_lookup`` is provided and the allowlist missed). Never rejects on its
    own — a ``device_invalid`` / ``unknown`` result simply means "let the device
    execution decide", so the caller must not hard-block on it.
    """
    tok = (token or "").strip()
    if not tok:
        return SubkeywordDecision(True, "empty")

    if allowlist_accepts(head, tok):
        return SubkeywordDecision(True, "allowlist", detail=f"{head}->{tok}")

    if help_lookup is not None:
        try:
            help_text = help_lookup()
        except Exception:  # pragma: no cover - advisory path must never crash
            help_text = ""
        if help_text:
            if help_lists_token(tok, help_text):
                return SubkeywordDecision(True, "device_help", detail=f"{head}->{tok}")
            if not is_accepted(help_text):
                return SubkeywordDecision(False, "device_invalid", detail=f"{head}->{tok}")

    return SubkeywordDecision(False, "unknown", detail=f"{head}->{tok}")


def head_token(full_command: str) -> str:
    """Return the last token of a matched command head (the allowlist key).

    For ``show interface`` this is ``interface`` — the token a questioned
    sub-keyword would follow.
    """
    parts = (full_command or "").split()
    return parts[-1] if parts else ""
