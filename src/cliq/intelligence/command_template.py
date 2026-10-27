#!/usr/bin/env python3
"""
command_template.py — Value-stripping / template extraction for learned commands.

CLIQ learns the commands a device ACCEPTS so it can suggest "what runs here"
later. To keep that shared corpus *pollution-free by construction*, the variable
operands a user happened to type — interface names, IP addresses, masks, VLAN
IDs, AS numbers, hostnames, free-text WORDs — are stripped to typed placeholders
BEFORE storage. The store therefore holds reusable command **templates**, never
literals:

    show ip ospf neighbor GigabitEthernet1   ->  show ip ospf neighbor <interface>
    ip address 10.1.1.1 255.255.255.0        ->  ip address <ip> <mask>
    ping 10.1.1.1                            ->  ping <ip>
    spanning-tree vlan 10 priority 4096      ->  spanning-tree vlan <arg> priority <arg>

Two signals decide whether a token is a keyword (kept literal) or an operand
(placeholdered):

  1. **Shape** — interface names, IPv4/IPv6 addresses, netmasks and bare integers
     are recognised by form alone (high confidence, context-free).
  2. **?-help context** — for everything else, the device's own ``?``-help for the
     preceding command head lists the valid *keyword* continuations. A token that
     is not one of those keywords sits in an argument position and becomes
     ``<arg>``. This reuses the captured [HELP-CAPTURE] intelligence; it does not
     build a new parser.

Keywords (subcommands) are always kept literal so the template stays runnable and
dedupes cleanly. Placeholders use a small, stable vocabulary: ``<interface>``,
``<ip>``, ``<mask>``, ``<arg>``.

Pure / offline / no I/O — safe to call on the hot path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

# Placeholder vocabulary (stable — stored in the shared template DB).
PH_INTERFACE = "<interface>"
PH_IP = "<ip>"
PH_MASK = "<mask>"
PH_ARG = "<arg>"

_PLACEHOLDERS = frozenset({PH_INTERFACE, PH_IP, PH_MASK, PH_ARG})

# Interface name shapes: a known interface family (full name or common
# abbreviation) immediately followed by a slot number such as ``0``, ``0/0``,
# ``1/0/1``, ``0/0.100`` or ``1:2``. Case-insensitive. Bridge/virtual families
# (BDI, BVI, Bluetooth, Vlan, Port-channel, Tunnel, Loopback) are included so a
# switch interface is stripped just like a router one.
_INTERFACE_FAMILY = (
    r"(?:"
    r"gigabitethernet|tengigabitethernet|twentyfivegige|fortygigabitethernet|"
    r"fortygige|hundredgige|fastethernet|ethernet|appgigabitethernet|"
    r"port-?channel|portchannel|loopback|tunnel|vlan|bdi|bvi|bluetooth|"
    r"serial|pos|atm|dialer|virtual-?template|multilink|cellular|"
    r"gi|te|twe|fo|hu|fa|eth|po|lo|tu|vl|se|mgmt|management"
    r")"
)
_INTERFACE_RE = re.compile(
    rf"^{_INTERFACE_FAMILY}\d+(?:[/:]\d+)*(?:\.\d+)?$", re.IGNORECASE
)

# Dotted-quad (IPv4). Validity of each octet (0-255) is checked separately so a
# token like ``1.2.3.4`` is an address but ``999.1.1.1`` is left alone.
_DOTTED_QUAD_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

# IPv4 with CIDR prefix length (``10.0.0.0/24``) — address part placeholdered.
_CIDR_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})$")

# IPv6: at least two hextet groups separated by ':' (covers ``::`` and full
# forms). Must contain a hex digit so a lone ``::`` style typo is ignored.
_IPV6_RE = re.compile(r"^(?=.*[0-9A-Fa-f])[0-9A-Fa-f:]+:[0-9A-Fa-f:]*$")

# Bare integer or integer range (process IDs, AS numbers, VLAN IDs, priorities).
_INTEGER_RE = re.compile(r"^\d+(?:-\d+)?$")

# The set of contiguous IPv4 netmask octet values. A dotted-quad whose octets are
# all drawn from this set AND form a contiguous 1s-then-0s mask is a netmask.
_MASK_OCTETS = frozenset({0, 128, 192, 224, 240, 248, 252, 254, 255})


def _is_valid_ipv4(token: str) -> bool:
    if not _DOTTED_QUAD_RE.match(token):
        return False
    return all(0 <= int(o) <= 255 for o in token.split("."))


def _is_netmask(token: str) -> bool:
    """True when ``token`` is a contiguous IPv4 subnet mask (255.255.255.0 etc.).

    Distinguishes a mask from an ordinary address so ``ip address <ip> <mask>``
    keeps both operand kinds typed correctly. A wildcard mask (0.0.0.255) is the
    bitwise inverse and is also contiguous, so it is accepted as a mask too.
    """
    if not _is_valid_ipv4(token):
        return False
    octets = [int(o) for o in token.split(".")]
    if any(o not in _MASK_OCTETS for o in octets):
        return False
    # Contiguity: the 32-bit value must be 1*0* (netmask) or 0*1* (wildcard).
    bits = 0
    for o in octets:
        bits = (bits << 8) | o
    # Normalise wildcard to mask form for the contiguity test.
    if bits & 1:                      # likely wildcard (…0.255) -> invert
        bits = (~bits) & 0xFFFFFFFF
    if bits == 0:
        return True
    # A contiguous left-aligned mask satisfies: (~bits & (bits >> 1)) == 0 once
    # right-padded zeros are added back — simplest check: bits+lowbit is a power
    # of two boundary.
    inverted = (~bits) & 0xFFFFFFFF
    return (inverted & (inverted + 1)) == 0


def _shape_placeholder(token: str) -> Optional[str]:
    """Return the typed placeholder for a token recognised purely by shape, or
    None when the token has no operand shape (i.e. it may be a keyword)."""
    if token in _PLACEHOLDERS:
        return token                  # already a placeholder — keep as-is
    if _INTERFACE_RE.match(token):
        return PH_INTERFACE
    if _CIDR_RE.match(token):
        return PH_IP
    if _is_valid_ipv4(token):
        return PH_MASK if _is_netmask(token) else PH_IP
    if ":" in token and _IPV6_RE.match(token):
        return PH_IP
    if _INTEGER_RE.match(token):
        return PH_ARG
    return None


def _is_keyword_option(token: str, options: List[str]) -> bool:
    """True when ``token`` matches a device-help *keyword* option for the
    preceding head (abbreviation-tolerant), so it is kept literal.

    All-uppercase help entries (``WORD``, ``LINE``, ``A.B.C.D``) are argument
    *descriptors*, not keywords, so they never make a typed value look like a
    keyword.
    """
    tl = token.lower()
    for opt in options:
        if opt.isupper():             # WORD / LINE / A.B.C.D — arg descriptor
            continue
        ol = opt.lower()
        if ol == tl or ol.startswith(tl) or tl.startswith(ol):
            return True
    return False


@dataclass
class TemplateResult:
    """Outcome of stripping a literal command to a reusable template."""

    template: str                       # e.g. "show ip ospf neighbor <interface>"
    stripped: List[str] = field(default_factory=list)   # placeholders emitted, in order
    keyword_prefix: str = ""            # leading keyword run before the FIRST placeholder

    @property
    def changed(self) -> bool:
        return bool(self.stripped)


def extract_template(
    command: str,
    *,
    help_lookup: Optional[Callable[[str], Optional[List[str]]]] = None,
) -> TemplateResult:
    """Strip variable operands from ``command`` to a reusable template.

    ``help_lookup`` (optional) maps a normalized command *prefix* (the keywords
    typed so far, space-joined, lowercased) to the device's ``?``-help keyword
    options for that prefix, or None when no help is known. It refines the
    shape-based pass: a token that is neither a recognised shape nor a known
    keyword for its position becomes ``<arg>``.

    Returns a :class:`TemplateResult`. Operand-free commands are returned
    unchanged (``stripped == []``), so already-clean commands round-trip exactly.
    """
    tokens = (command or "").split()
    out: List[str] = []
    stripped: List[str] = []
    prefix_words: List[str] = []        # literal keywords seen so far (for help lookup)
    keyword_prefix_words: List[str] = []
    seen_placeholder = False

    for tok in tokens:
        ph = _shape_placeholder(tok)
        if ph is None and help_lookup is not None and prefix_words:
            # Help-context pass: is this an argument position (token is not one of
            # the device's keyword continuations for the prefix typed so far)?
            try:
                options = help_lookup(" ".join(prefix_words))
            except Exception:
                options = None
            if options and not _is_keyword_option(tok, options):
                ph = PH_ARG
        if ph is not None:
            out.append(ph)
            stripped.append(ph)
            seen_placeholder = True
            # Prefix tracking continues with the placeholder so a later help
            # lookup never treats an operand as a keyword head.
            prefix_words.append(ph)
        else:
            out.append(tok)
            prefix_words.append(tok)
            if not seen_placeholder:
                keyword_prefix_words.append(tok)

    return TemplateResult(
        template=" ".join(out),
        stripped=stripped,
        keyword_prefix=" ".join(keyword_prefix_words),
    )


def redact_args(stripped: List[str]) -> List[str]:
    """Return only the placeholder *types* for observability (never the values).

    The literal operand values (IPs, interface names, hostnames) are NEVER
    surfaced — only the typed placeholders that replaced them, so logs prove the
    strip happened without leaking what was stripped.
    """
    return list(stripped)


# ---------------------------------------------------------------------------
# DITA syntax-notation stripping (product-CR web-sourced commands).
#
# Cisco Command Reference (DITA HTML) syntax is documentation notation, not a
# runnable command: ``[ ]`` marks an optional group, ``{ }`` a required choice
# group, ``|`` separates alternatives, and ``<arg>`` / bare hyphenated words
# (e.g. ``range-of-multiplier``) are argument descriptors. None of that may
# ever reach the terminal — only the leading mandatory keyword stem is
# runnable. This is a *different* concern from ``extract_template`` above
# (which strips literal VALUES a user actually typed); this strips
# documentation NOTATION a user never typed in the first place.
# ---------------------------------------------------------------------------

_NOTATION_MARKERS = "[{<|"

# Common Cisco doc argument-descriptor suffixes for BARE trailing placeholder
# words that appear with no bracket notation at all (e.g. the doc syntax
# "qos queue-softmax-multiplier range-of-multiplier" has no brackets, but
# "range-of-multiplier" is still a variable, not a literal keyword). Only the
# single trailing token is ever checked — never a whole trailing run — so a
# legitimate hyphenated keyword earlier in the command (e.g. the preceding
# "queue-softmax-multiplier") is never mistaken for a placeholder.
_BARE_PLACEHOLDER_SUFFIXES = (
    "-id", "-name", "-number", "-index", "-value", "-string",
    "-address", "-mask", "-multiplier", "-range", "-size", "-length",
)


def _skip_bracket_group(text: str, start: int) -> int:
    """Return the index just past the ``[``/``{`` group opening at ``start``."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        if text[i] in "[{":
            depth += 1
        elif text[i] in "]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n   # unterminated group — consume the rest of the string


def _looks_like_bare_placeholder(token: str) -> bool:
    tl = token.lower()
    if tl.startswith("<") and tl.endswith(">"):
        return True
    return tl.endswith(_BARE_PLACEHOLDER_SUFFIXES)


def strip_syntax_notation(syntax: str) -> str:
    """Derive the mandatory, runnable base command from a DITA syntax string.

    Rule implemented: the runnable base is the leading run of mandatory
    keywords up to the FIRST optional/choice/argument marker (``[``, ``{``,
    ``<``, or ``|``). A leading optional group (marker at position 0, rare) is
    skipped entirely rather than truncating to nothing, so scanning continues
    into whatever mandatory text follows it. If no marker appears at all, a
    single trailing bare argument-descriptor word (see
    ``_BARE_PLACEHOLDER_SUFFIXES``) is dropped, since Cisco docs sometimes
    append a mandatory argument with no bracket notation.

    Examples:
        "show processes cpu platform history[ 1min| 5min| 5sec| 60min] "
        "location switch{ switch-number| active| standby} { 0| F0| FP active| R0}"
            -> "show processes cpu platform history"
        "snmp-server enable traps cpu [ threshold]"
            -> "snmp-server enable traps cpu"
        "qos queue-softmax-multiplier range-of-multiplier"
            -> "qos queue-softmax-multiplier"
        "show ip ospf neighbor" -> "show ip ospf neighbor"   (unchanged)

    Known limitation: a mandatory token that appears AFTER an optional group
    (e.g. "ip ospf [ area area-id] priority priority-value") is not recovered —
    only the leading mandatory run is kept, per the "up to the first marker"
    rule this function implements.
    """
    s = (syntax or "").strip()
    base = ""
    while s:
        idx = next((i for i, ch in enumerate(s) if ch in _NOTATION_MARKERS), -1)
        if idx == -1:
            base = s
            break
        if idx == 0:
            end = _skip_bracket_group(s, 0) if s[0] in "[{" else 1
            s = s[end:].lstrip()
            continue
        base = s[:idx].rstrip()
        break

    tokens = base.split()
    if tokens and _looks_like_bare_placeholder(tokens[-1]):
        tokens = tokens[:-1]
    return " ".join(tokens)


def clean_prefix(template: str) -> str:
    """Return the leading keyword run of ``template`` up to the first placeholder.

    Used to seed inline ghost completion from stored templates: a template such
    as ``show ip ospf neighbor <interface>`` ghosts as ``show ip ospf neighbor``
    (no ``<interface>`` ever rendered inline).
    """
    words: List[str] = []
    for w in (template or "").split():
        if w.startswith("<") and w.endswith(">"):
            break
        words.append(w)
    return " ".join(words)


def is_template(text: str) -> bool:
    """True when ``text`` contains at least one placeholder token."""
    return any(w in _PLACEHOLDERS for w in (text or "").split())
