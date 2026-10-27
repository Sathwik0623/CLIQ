#!/usr/bin/env python3
"""
text_repair.py — Word-boundary reconstruction for extracted document prose.

Some PDFs (notably Cisco command references) store body paragraphs as runs of
glyphs with NO inter-word space characters in the text stream. pypdf — in both
its default and ``layout`` extraction modes — therefore returns concatenated
text such as ``IPaddressofthemulticastgroup``. The glyphs carry no per-character
positioning fine enough to recover the spaces geometrically, so the only viable
local (no-cloud) repair is structural + dictionary-based word segmentation.

Two signals are exploited, in order:

  1. **Letter casing is preserved** even though spaces are stripped, so word and
     acronym boundaries survive as case transitions:
       * ``ProtocolIndependentMulticast`` -> ``Protocol Independent Multicast``
         (lowercase -> uppercase boundary), and
       * ``IPaddress`` / ``VRFtable`` -> ``IP address`` / ``VRF table``
         (acronym-run -> lowercase-word boundary).
     All-uppercase tokens are treated as acronyms and kept intact (so ``VRF``
     never degrades to ``V RF``).
  2. The remaining all-lowercase clusters (``addressofthemulticastgroup``,
     ``usethe``) are segmented with the offline ``wordninja`` language model,
     then a small compound-repair map re-joins networking terms the generic
     English model over-splits (``multi``+``cast`` -> ``multicast``).

``wordninja`` is an optional dependency: when it is absent this module degrades
to a no-op (extraction quality drops but ingestion never fails), matching the
codebase's optional-dependency pattern.

Applied ONLY to description / syntax-description prose — command lines are
already clean in the source and must not be passed through here.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

log = logging.getLogger("ingest.text_repair")

_OCR_PHRASE_REPAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bipo\s+s\s+pf\b", re.IGNORECASE), "ip ospf"),
    (re.compile(r"\bipv6os\s+pf\b", re.IGNORECASE), "ipv6 ospf"),
    (re.compile(r"\bshow\s+ip\s+m\s+route\b", re.IGNORECASE), "show ip mroute"),
    (re.compile(r"\bshow\s+ipv6\s+m\s+route\b", re.IGNORECASE), "show ipv6 mroute"),
    (re.compile(r"\bCisco\s+IOSXE(?:G|\s+G)?\s+i\s+br\s+altar(?=\d|\b)", re.IGNORECASE), "Cisco IOS XE Gibraltar "),
    (re.compile(r"\bIOSXE(?:G|\s+G)?\s+i\s+br\s+altar(?=\d|\b)", re.IGNORECASE), "IOS XE Gibraltar "),
    (re.compile(r"\bLISPI\s+n\s+stance\b", re.IGNORECASE), "LISP Instance"),
    (re.compile(r"\bLISPS\s+er\s+vice\b", re.IGNORECASE), "LISP Service"),
    (re.compile(r"\bIPV\s+ideo\b", re.IGNORECASE), "IP Video"),
    (re.compile(r"\bIP\s+v6\b", re.IGNORECASE), "IPv6"),
)

# Maximal runs of letters; everything between runs (spaces, digits, punctuation,
# hyphens, brackets, pipes) is preserved verbatim by re.sub.
_LETTER_RUN_RE = re.compile(r"[A-Za-z]+")

# Insert a boundary between an acronym run and a following lowercase word
# ("IPaddress" -> "IP address", "VRFtable" -> "VRF table"). An acronym glued to a
# following Capitalised word ("LISPInstance") is structurally identical to an
# acronym glued to a lowercase word ("VRFtable"), so only a dictionary could
# tell them apart; the lowercase-word case is far more common in this corpus,
# so the whole uppercase run is kept intact as the acronym.
_ACRONYM_WORD_RE = re.compile(r"([A-Z]{2,})([a-z])")
# Insert a boundary at a lowercase -> uppercase transition
# ("ProtocolIndependent" -> "Protocol Independent").
_CAMEL_RE = re.compile(r"([a-z])([A-Z])")

# Readability spacing for description prose only: PDF extraction frequently glues
# a parenthesised acronym to its neighbours ("First(OSPF)neighbor"). Inserting a
# space around the parentheses restores readability without touching command
# lines (which never request readable spacing).
_PAREN_OPEN_RE = re.compile(r"([A-Za-z0-9])\(")
_PAREN_CLOSE_RE = re.compile(r"\)([A-Za-z0-9])")

# Lowercase clusters shorter than this are left untouched: genuine short words
# are single dictionary tokens that the segmenter would keep whole anyway, and
# skipping them protects short lowercase domain tokens (vrf, rloc, eid) that the
# generic model would otherwise fragment into single letters ("v rf", "r loc").
_MIN_SEGMENT_LEN = 5

# Networking / compound terms the generic English model splits apart, keyed on
# the lowercased adjacent fragment pair. Re-joined preserving original casing.
_COMPOUND_PAIRS = frozenset(
    {
        ("multi", "cast"),
        ("uni", "cast"),
        ("any", "cast"),
        ("broad", "cast"),
        ("time", "out"),
        ("trust", "point"),
        ("reach", "ability"),
        ("stack", "wise"),
        ("romm", "on"),
        ("loop", "back"),
        ("break", "point"),
        ("check", "point"),
        ("up", "time"),
        ("down", "time"),
        ("run", "time"),
        ("life", "time"),
        ("keep", "alive"),
        ("fail", "over"),
        ("pass", "through"),
        ("sub", "interface"),
        ("sub", "net"),
        ("super", "net"),
        ("name", "server"),
        ("name", "space"),
        ("data", "plane"),
        ("data", "path"),
        ("mid", "plane"),
        ("back", "plane"),
        ("line", "card"),
        ("play", "book"),
        ("allow", "list"),
        ("block", "list"),
        ("white", "list"),
        ("black", "list"),
        ("auto", "state"),
        ("auto", "negotiation"),
        ("host", "name"),
        ("user", "name"),
        ("over", "lay"),
        ("under", "lay"),
    }
)

# Networking protocol/feature acronyms the generic English model over-splits
# (``eigrp`` -> ``ei grp``, ``netflow`` -> ``net flow``). These tokens are
# treated as ATOMIC: whenever wordninja fragments a lowercase cluster, any run
# of adjacent fragments whose concatenation matches one of these keywords is
# re-joined (preserving original casing). Stored lowercased; matching is
# case-insensitive. This protects command_path tokens so exact-tier retrieval
# (e.g. ``show eigrp`` -> ``show eigrp``) keeps working.
#
# Re-joining only fires on fragments the segmenter itself produced, so ordinary
# English is never corrupted: words like "crisis" (contains "isis") or
# "combination" (contains "nat") stay whole because wordninja keeps them whole
# and never emits the fragments that would recombine into a protected token.
_PROTECTED_ACRONYMS = frozenset(
    {
        "eigrp", "ospf", "ospfv3", "isis", "bgp", "rip", "pim", "igmp",
        "lisp", "nhrp", "odr", "vrrp", "hsrp", "glbp", "mpls", "ldp",
        "evn", "vrf", "vlan", "stp", "cdp", "lldp", "snmp", "netflow",
        "qos", "nbar", "acl", "nat", "dhcp", "ipv6", "ipsla", "sla",
        "cef", "qfp", "fed",
        # LAN-switching families (Track A — lan-switching PDF). Protected so the
        # desegmenter never shatters them (e.g. "etherchannel" -> "ether channel",
        # "switchport" -> "switch port", "dot1q" -> "dot 1 q").
        "switchport", "etherchannel", "portchannel", "dot1q", "vtp", "dtp",
        "udld", "rstp", "pvst", "mstp", "rpvst", "lacp", "pagp",
        # Spanning-tree single-word keywords the generic model splits
        # ("bpduguard" -> "bpd u guard", "portfast" -> "port fast"). Each is one
        # atomic CLI token in IOS, so re-joining keeps exact-tier retrieval intact.
        "bpduguard", "bpdufilter", "portfast", "uplinkfast", "backbonefast",
        "loopguard", "spantree",
    }
)

# Most protected acronyms are 1 syllable; the generic model rarely shatters one
# into more than four fragments. Bounding the merge window keeps the rejoin
# linear and avoids accidentally swallowing neighbouring real words.
_MAX_ACRONYM_FRAGMENTS = 4

# Token shape used to count protected acronyms for observability.
_ACRONYM_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

_wordninja = None          # cached module handle
_wordninja_loaded = False  # True once we have attempted the import


def _load_wordninja():
    """Lazily import wordninja, caching the result. Returns the module or None."""
    global _wordninja, _wordninja_loaded
    if not _wordninja_loaded:
        _wordninja_loaded = True
        try:
            import wordninja  # type: ignore

            _wordninja = wordninja
        except ImportError:
            log.warning(
                "wordninja is not installed; concatenated PDF prose will not be "
                "re-spaced. Install the 'ingest' extra to enable word-boundary "
                "reconstruction."
            )
            _wordninja = None
    return _wordninja


def _repair_compounds(parts: List[str]) -> List[str]:
    """Re-join domain compounds that the language model over-splits, preserving
    the original casing (e.g. ['Multi', 'cast'] -> ['Multicast'])."""
    out: List[str] = []
    i = 0
    n = len(parts)
    while i < n:
        if i + 1 < n and (parts[i].lower(), parts[i + 1].lower()) in _COMPOUND_PAIRS:
            out.append(parts[i] + parts[i + 1])
            i += 2
            continue
        out.append(parts[i])
        i += 1
    return out


def _repair_acronyms(parts: List[str]) -> List[str]:
    """Re-join consecutive fragments that recombine into a protected networking
    acronym, preserving original casing (e.g. ['ei', 'grp'] -> ['eigrp']).

    Greedy longest-window match so multi-fragment splits collapse in one step.
    """
    out: List[str] = []
    i = 0
    n = len(parts)
    while i < n:
        matched_end = -1
        upper = min(n, i + _MAX_ACRONYM_FRAGMENTS)
        for j in range(upper, i, -1):
            if "".join(parts[i:j]).lower() in _PROTECTED_ACRONYMS:
                matched_end = j
                break
        if matched_end != -1:
            out.append("".join(parts[i:matched_end]))
            i = matched_end
        else:
            out.append(parts[i])
            i += 1
    return out


def _segment_cluster(cluster: str) -> str:
    """Segment a single all-lowercase / mixed cluster into spaced words."""
    ninja = _load_wordninja()
    if ninja is None:
        return cluster
    parts = ninja.split(cluster)
    if len(parts) <= 1:
        return cluster
    # Protect networking acronyms BEFORE generic compound repair so a split
    # acronym (``ei``+``grp``) is rebuilt atomically, never as separate words.
    parts = _repair_acronyms(parts)
    return " ".join(_repair_compounds(parts))


def _repair_run(run: str) -> str:
    """Reconstruct word boundaries within one maximal alphabetic run."""
    # 1. Split on case transitions so acronyms and capitalised words separate.
    spaced = _ACRONYM_WORD_RE.sub(r"\1 \2", run)
    spaced = _CAMEL_RE.sub(r"\1 \2", spaced)
    tokens = spaced.split(" ")

    repaired: List[str] = []
    for tok in tokens:
        if not tok:
            continue
        # All-uppercase tokens are acronyms (VRF, ETR, EXEC) — keep intact.
        if len(tok) >= 2 and tok.isupper():
            repaired.append(tok)
        elif len(tok) >= _MIN_SEGMENT_LEN:
            repaired.append(_segment_cluster(tok))
        else:
            repaired.append(tok)
    return " ".join(p for p in repaired if p)


def _apply_ocr_phrase_repairs(text: str) -> str:
    """Apply conservative phrase-level OCR repairs seen in Cisco references."""
    repaired = text
    for pattern, replacement in _OCR_PHRASE_REPAIRS:
        repaired = pattern.sub(replacement, repaired)
    repaired = re.sub(r"\bGibraltar(?=\d)", "Gibraltar ", repaired)
    repaired = re.sub(r"\bIOS XE(?=\d)", "IOS XE ", repaired)
    return re.sub(r"\s+", " ", repaired).strip()


def desegment_text(text: Optional[str], *, readable: bool = False) -> str:
    """Reconstruct word boundaries in concatenated prose.

    Existing whitespace and all non-letter characters (digits, punctuation,
    hyphens, brackets) are preserved; only alphabetic runs are repaired. Returns
    the input unchanged when it is empty.

    When ``readable`` is set (description prose only), parentheses glued to
    adjacent words are also re-spaced (``First(OSPF)neighbor`` ->
    ``First (OSPF) neighbor``). Command/syntax lines never set this flag so
    their CLI structure is never altered.
    """
    if not text:
        return text or ""
    repaired = _apply_ocr_phrase_repairs(text)
    if _load_wordninja() is not None:
        repaired = _LETTER_RUN_RE.sub(lambda m: _repair_run(m.group(0)), repaired)
        repaired = _apply_ocr_phrase_repairs(repaired)
    if readable:
        repaired = _PAREN_OPEN_RE.sub(r"\1 (", repaired)
        repaired = _PAREN_CLOSE_RE.sub(r") \1", repaired)
        repaired = re.sub(r"\s+", " ", repaired).strip()
    return repaired


def repair_command_text(text: Optional[str]) -> str:
    """Repair command and syntax lines without changing their CLI structure."""
    if not text:
        return text or ""
    repaired = desegment_text(text)
    repaired = re.sub(r"\s+", " ", repaired).strip()
    return repaired


def count_protected_acronyms(text: Optional[str]) -> int:
    """Return how many whitelist networking acronyms appear as tokens in ``text``.

    Used for ingestion observability — quantifies how many command tokens are
    kept atomic by the acronym-protection guard.
    """
    if not text:
        return 0
    return sum(
        1
        for tok in _ACRONYM_TOKEN_RE.findall(text)
        if tok.lower() in _PROTECTED_ACRONYMS
    )
