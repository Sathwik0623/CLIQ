#!/usr/bin/env python3
"""
did_you_mean.py — Advisory-only "did you mean?" correction for commands the
DEVICE itself rejected (after Enter).

When an engineer presses Enter and the live device returns an error such as::

        R1#show running-config run
                                   ^
        % Invalid input detected at '^' marker.

CLIQ pinpoints the offending token, re-queries the device's OWN context-help
(``<valid prefix> ?``) to discover the authoritative valid tokens at that point,
and suggests the closest match — OR suggests dropping an extraneous trailing
word when the prefix is already a complete command.

Cardinal rules (mirroring the rest of CLIQ):
    * Advisory only — nothing is re-run. The suggestion is surfaced for the
      engineer to review and submit themselves.
    * Device-driven — valid tokens always come from the device's real ``?``
      help, never from a command-specific hardcoded table.

The functions here are pure (no I/O) except :func:`analyze_device_error`, which
takes a ``help_lookup`` callable so the device round-trip stays injectable and
unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from cliq.intelligence.learned_commands import is_accepted

# A line that is nothing but caret(s) and surrounding whitespace marks the
# column the device rejected (``        ^``).
_CARET_LINE_RE = re.compile(r"^\s*\^+\s*$")
# Device help row: ``keyword<2+ spaces>description``. Two+ spaces separate the
# token column from its description, mirroring the frontend help parser.
_HELP_ROW_RE = re.compile(r"^\s*(\S+)\s{2,}\S")
# A bare token is a valid CLI keyword candidate.
_TOKEN_RE = re.compile(r"^[A-Za-z][\w-]*$")
# The device's '% Incomplete command' signal: the command is valid so far but
# still needs at least one more argument.
_INCOMPLETE_RE = re.compile(r"%\s*incomplete command", re.IGNORECASE)
# A help row capturing BOTH the option token and its description. Unlike
# ``_HELP_ROW_RE`` this keeps argument placeholders such as ``WORD``,
# ``<1-4094>`` or ``A.B.C.D`` (which are exactly what a missing argument needs).
_HELP_ARG_ROW_RE = re.compile(r"^\s*(\S+)\s{2,}(\S.*?)\s*$")


@dataclass
class DidYouMean:
    """An advisory correction surfaced after the device rejected a command."""

    message: str
    fix: Optional[str]
    # Bug 5 — distinguishes a capability mismatch ("wrong_device") from a plain
    # typo so the frontend can colour/route it differently. Defaults to "typo"
    # so every existing construction keeps its current behaviour.
    kind: str = "typo"


# Bug 5 — capability keywords whose device-side rejection (the ``^`` caret
# landing on the keyword itself) is AUTHORITATIVE proof the device lacks that
# feature. Each maps to capability-appropriate alternatives the device DOES
# support. This is device-driven (the device rejected the token) — NOT a
# per-platform "a router has no VLANs" table. On an L2/L3 device the same
# command is ACCEPTED, so this path never runs there.
_CAPABILITY_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "vlan": ("show ip interface brief", "show interfaces"),
}

# Bug G — Platform-Dependent subsystems that are valid on OTHER platforms but
# not C8000v. Each maps to the C8000v-correct alternative. Used only to phrase a
# clearer advisory; the alternative is still confirmed against the device's own
# 'show platform hardware ?' option set before being suggested.
_PD_PLATFORM_ALTERNATIVES: dict[str, tuple[str, str]] = {
    "fed": ("qfp", "Cat9k-only"),
}


def _expand_capability_keyword(token: str) -> Optional[str]:
    """Resolve a (possibly abbreviated) rejected token to a capability keyword.

    Handles IOS abbreviations: ``vlan``/``vla``/``vl`` -> ``vlan``. Only an
    UNAMBIGUOUS prefix (matching exactly one capability keyword) resolves, and a
    minimum length of 2 keeps a bare single letter from ever matching.
    """
    t = (token or "").lower()
    if len(t) < 2:
        return None
    matches = [k for k in _CAPABILITY_ALTERNATIVES if k == t or k.startswith(t)]
    return matches[0] if len(matches) == 1 else None


def build_capability_hint(bad_token: str) -> Optional[DidYouMean]:
    """Bug 5 — capability-aware advisory when the device rejects a feature word.

    When the device's ``^`` caret lands on a known capability keyword (e.g.
    ``vlan``), the rejection itself proves the device cannot do it — so we offer
    capability-appropriate alternatives instead of a misleading typo "did you
    mean". Pure and advisory: ``fix`` stays ``None`` (the engineer chooses an
    alternative). Returns ``None`` for any other token so normal suggestion
    handling proceeds.
    """
    keyword = _expand_capability_keyword(bad_token)
    if keyword is None:
        return None
    alts = " \u00b7 ".join(_CAPABILITY_ALTERNATIVES[keyword])
    return DidYouMean(
        message=f"'{keyword}' isn't supported on this device. Try: {alts}",
        fix=None,
        kind="wrong_device",
    )


def build_pd_suggestion(
    valid_prefix: str, bad_token: str, options: list[str],
) -> Optional[DidYouMean]:
    """Bug G — turn a Platform-Dependent rejection into a C8000v alternative.

    When a known Cat9k-only subsystem (e.g. ``fed``) is rejected, suggest the
    C8000v-valid alternative (``qfp``) ONLY if the device's own option set
    actually lists it — keeping the device the source of truth. Advisory only;
    ``fix`` carries the corrected command so the engineer can run it. Returns
    None for any token that isn't a known PD mismatch."""
    info = _PD_PLATFORM_ALTERNATIVES.get((bad_token or "").lower())
    if info is None:
        return None
    alt, only_on = info
    opts_lower = {o.lower() for o in options}
    if alt not in opts_lower:
        return None
    fix = (f"{valid_prefix} {alt}").strip()
    return DidYouMean(
        message=f"'{bad_token}' isn't valid on C8000v ({only_on}). Did you mean: {alt}?",
        fix=fix,
        kind="wrong_device",
    )


def output_has_error(output: str | None) -> bool:
    """True when the device response carries a rejection marker."""
    return not is_accepted(output)


def is_incomplete_command(output: str | None) -> bool:
    """True when the device answered ``% Incomplete command`` (needs an arg)."""
    return bool(_INCOMPLETE_RE.search(output or ""))


# Bug I — a SYNTAX error means the device could not parse the command (so a
# "did you mean?" correction is appropriate). An OPERATIONAL message starts with
# '%' but is informational — the command WAS valid, it just had nothing to do
# (e.g. ``%OSPF: No router process 1``). Correction must NEVER fire there.
_SYNTAX_ERROR_RE = re.compile(
    r"%\s*(invalid input|unrecognized command|incomplete command|"
    r"ambiguous command|unknown command|bad mask|wrong number of args)",
    re.IGNORECASE,
)


def is_syntax_error(output: str | None, prompt: str = "") -> bool:
    """Classify ``output`` as a genuine SYNTAX rejection (eligible for "did you
    mean?"). True only when a ``^`` caret marker is present OR an explicit IOS
    syntax error string appears. A bare ``%`` operational line (e.g. ``%OSPF: No
    router process 1``) is NOT a syntax error. Device is the source of truth."""
    text = output or ""
    if _SYNTAX_ERROR_RE.search(text):
        return True
    for line in text.splitlines():
        if "^" in line and _CARET_LINE_RE.match(line):
            return True
    return False


def response_type(output: str | None) -> str:
    """Return ``"syntax"`` | ``"operational"`` | ``"ok"`` for observability."""
    if is_syntax_error(output):
        return "syntax"
    return "operational" if not is_accepted(output) else "ok"


def parse_help_arguments(page_text: str) -> list[tuple[str, str]]:
    """Extract ``(token, description)`` rows from a device ``?``-help page.

    Unlike :func:`parse_help_options`, this KEEPS argument placeholders such as
    ``WORD``, ``<1-4094>`` and ``A.B.C.D`` — the very tokens that describe a
    missing argument. Pager artifacts, ``<cr>`` and ``%`` error lines are
    dropped. Order is preserved; the first row is the most likely next arg.
    """
    args: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in (page_text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<cr>") or stripped.startswith("%"):
            continue
        if re.match(r"--+\s*more", stripped, re.IGNORECASE):
            continue
        m = _HELP_ARG_ROW_RE.match(line)
        if not m:
            continue
        token, desc = m.group(1), m.group(2).strip()
        low = token.lower()
        if low in seen:
            continue
        seen.add(low)
        args.append((token, desc))
    return args


def build_incomplete_hint(page_text: str) -> Optional[DidYouMean]:
    """Compose a 'needs argument' advisory from a ``command ?`` help page.

    Example output: ``Needs argument: WORD (VLAN IDs 1-4094)``. Advisory only —
    ``fix`` is None because the engineer must supply the value themselves.
    """
    args = parse_help_arguments(page_text)
    if not args:
        return None
    parts: list[str] = []
    for token, desc in args[:3]:
        parts.append(f"{token} ({desc})" if desc else token)
    return DidYouMean(message=f"Needs argument: {', '.join(parts)}", fix=None)


def parse_help_options(page_text: str) -> tuple[list[str], bool]:
    """Extract the valid next-token keywords from a device ``?``-help page.

    Returns ``(options, has_cr)`` where ``has_cr`` is True when ``<cr>`` is
    listed — the device's signal that the queried prefix is itself a complete,
    runnable command (so a trailing extra word can simply be dropped).
    """
    options: list[str] = []
    has_cr = False
    seen: set[str] = set()
    for line in (page_text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<cr>"):
            has_cr = True
            continue
        m = _HELP_ROW_RE.match(line)
        keyword = m.group(1) if m else stripped.split()[0]
        if _TOKEN_RE.match(keyword):
            low = keyword.lower()
            if low not in seen:
                seen.add(low)
                options.append(keyword)
    return options, has_cr


def _levenshtein(a: str, b: str) -> int:
    """Classic edit distance (small strings; cost is negligible)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + (ca != cb),
            ))
        prev = cur
    return prev[-1]


# Fuzzy-matcher hardening — well-known GLOBAL commands an engineer types
# constantly. The fuzzy matcher must never swap one of these (or an
# abbreviation of it) into an unrelated valid keyword that is merely one edit
# away (the classic ``show`` -> ``flow`` that destroys user intent).
PROTECTED_GLOBAL_COMMANDS: frozenset[str] = frozenset({
    "show", "configure", "ping", "traceroute", "copy", "write", "reload",
    "clear", "debug", "undebug", "telnet", "ssh", "interface", "router",
    "no", "do", "enable", "disable",
})


def _resolves_to_protected(token: str) -> Optional[str]:
    """Return the protected global command a token abbreviates, or ``None``.

    ``show``/``sho``/``sh`` -> ``show``. Only an UNAMBIGUOUS prefix (matching
    exactly one protected command) resolves; a bare single letter never does.
    """
    t = (token or "").strip().lower()
    if len(t) < 2:
        return None
    if t in PROTECTED_GLOBAL_COMMANDS:
        return t
    matches = [c for c in PROTECTED_GLOBAL_COMMANDS if c.startswith(t)]
    return matches[0] if len(matches) == 1 else None


def is_protected_substitution(original_token: str, suggested_token: str) -> bool:
    """True when replacing ``original_token`` with ``suggested_token`` would
    corrupt a well-known global command (e.g. ``show`` -> ``flow``).

    Rejected when the ORIGINAL token is (an abbreviation of) a protected global
    command AND the suggestion is a DIFFERENT word. Correctly completing an
    abbreviation of the SAME command (``sh`` -> ``show``) is always allowed.
    """
    protected = _resolves_to_protected(original_token)
    if protected is None:
        return False
    suggested = (suggested_token or "").strip().lower()
    if suggested == protected:
        return False
    if protected.startswith(suggested) or suggested.startswith(protected):
        return False
    return True


def best_matches(bad_token: str, options: list[str], limit: int = 3) -> list[str]:
    """Rank help options by closeness to ``bad_token`` (advisory suggestions).

    Prefix relationships rank highest (``rout`` -> ``route``), then small edit
    distances. Among options with the SAME edit distance, the one that reuses
    the most of the mistyped letters wins — so a transposition like ``valn``
    resolves to ``vlan`` (all four letters shared) rather than ``call`` (only
    two), which is equally distant but clearly not what was typed. Returns at
    most ``limit`` options, best first; empty when nothing is reasonably close
    (so the caller can fall back to listing/ dropping).

    Fuzzy-matcher hardening: an option that would turn a well-known GLOBAL
    command into an unrelated valid keyword (the classic ``show`` -> ``flow``)
    is rejected here at the source, protecting user intent.
    """
    bad = (bad_token or "").lower()
    if not bad:
        return []

    # Global-command protection — never substitute a protected global command's
    # token for an unrelated keyword one edit away (show -> flow).
    options = [
        opt for opt in options
        if not is_protected_substitution(bad_token, opt)
    ]

    prefix_hits: list[tuple[int, str]] = []
    edit_hits: list[tuple[int, int, int, int, str]] = []
    for opt in options:
        low = opt.lower()
        if low == bad:
            return [opt]
        if low.startswith(bad) or bad.startswith(low):
            # Shorter completions are the most likely intent.
            prefix_hits.append((abs(len(low) - len(bad)), opt))
            continue
        dist = _levenshtein(bad, low)
        # Only count genuinely close tokens (scales with token length).
        if dist <= max(1, min(len(bad), len(low)) // 2):
            # Tie-breakers (all "higher is better", so negated for ascending
            # sort): shared-letter count first (favours anagrams/transpositions
            # like valn->vlan), then common-prefix length (favours a matching
            # first letter), then a smaller length gap.
            edit_hits.append((
                dist,
                -_shared_char_count(bad, low),
                -_common_prefix_len(bad, low),
                abs(len(low) - len(bad)),
                opt,
            ))

    prefix_hits.sort(key=lambda t: (t[0], len(t[1])))
    edit_hits.sort(key=lambda t: t[:4])
    ranked = [o for _, o in prefix_hits] + [t[-1] for t in edit_hits]
    return ranked[:limit]


def _common_prefix_len(a: str, b: str) -> int:
    """Number of leading characters ``a`` and ``b`` share."""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def _shared_char_count(a: str, b: str) -> int:
    """Count of characters common to both strings (multiset intersection)."""
    from collections import Counter

    return sum((Counter(a) & Counter(b)).values())



def _caret_char_index(output: str, prompt: str) -> Optional[int]:
    """Character index (into the typed command) the device flagged, or None.

    The caret line's leading whitespace counts from the start of the echoed
    line, which begins with the prompt. So ``index_in_command = caret_col -
    len(prompt)``.
    """
    for line in (output or "").splitlines():
        if "^" in line and _CARET_LINE_RE.match(line):
            caret_col = line.index("^")
            idx = caret_col - len(prompt or "")
            return idx if idx >= 0 else None
    return None


def _char_index_to_token(command: str, char_index: int) -> Optional[int]:
    """Map a character offset within ``command`` to its token index."""
    pos = 0
    for i, tok in enumerate(command.split()):
        start = command.index(tok, pos)
        end = start + len(tok)
        if start <= char_index < end:
            return i
        pos = end
    return None


def locate_bad_token(
    command: str, output: str, prompt: str = "",
) -> Optional[tuple[str, str, int]]:
    """Return ``(valid_prefix, bad_token, token_index)`` for the rejected word.

    Uses the device's ``^`` caret column when it can be parsed cleanly; falls
    back to the last token (the overwhelmingly common case for an extra/typo'd
    trailing word).
    """
    tokens = command.split()
    if not tokens:
        return None

    idx: Optional[int] = None
    char_index = _caret_char_index(output, prompt)
    if char_index is not None:
        idx = _char_index_to_token(command, char_index)

    if idx is None or idx < 0 or idx >= len(tokens):
        idx = len(tokens) - 1

    bad_token = tokens[idx]
    valid_prefix = " ".join(tokens[:idx])
    return valid_prefix, bad_token, idx


def build_suggestion(
    valid_prefix: str,
    bad_token: str,
    options: list[str],
    has_cr: bool,
    trailing: tuple[str, ...] = (),
) -> Optional[DidYouMean]:
    """Compose an advisory suggestion from the device's valid options.

    ``trailing`` are the tokens the engineer typed AFTER the rejected word.
    They are preserved so the suggestion is the FULL corrected command (e.g.
    ``shw ip int br`` -> ``show ip int br``) rather than just the first fixed
    word — the caller then expands any abbreviations against the command DB.
    """
    prefix = (valid_prefix or "").strip()
    matches = best_matches(bad_token, options)
    if matches:
        parts = [p for p in (prefix, matches[0], *trailing) if p]
        fix = " ".join(parts).strip()
        return DidYouMean(message=f"Did you mean: {fix}?", fix=fix)
    if has_cr and prefix:
        # The prefix alone is a complete command — the trailing word is extra.
        return DidYouMean(message=f"Did you mean: {prefix}?", fix=prefix)
    if options:
        shown = ", ".join(options[:12])
        where = f" after '{prefix}'" if prefix else ""
        return DidYouMean(message=f"Valid options{where}: {shown}", fix=None)
    return None


def analyze_device_error(
    command: str,
    output: str,
    help_lookup: Callable[[str], str],
    prompt: str = "",
) -> Optional[DidYouMean]:
    """Top-level: turn a device-rejected command into an advisory suggestion.

    ``help_lookup`` is a callable that, given a valid prefix string, returns the
    device's ``?``-help page text for that prefix (the live device round-trip is
    injected so this stays unit-testable). Returns None when no useful
    suggestion can be formed — callers must treat None as "stay silent".
    """
    if not output_has_error(output):
        return None
    # Bug I — only a genuine SYNTAX rejection ('^' caret or an explicit IOS
    # syntax error) is eligible for a correction. An operational '%' message
    # (e.g. '%OSPF: No router process 1') means the command was VALID — the
    # device just had nothing to act on; stay silent.
    if not is_syntax_error(output):
        return None
    # Bug 3 — '% Incomplete command': the command is valid but needs another
    # argument. Ask the device what comes next (``<full command> ?``) and
    # surface the missing-argument descriptor it returns.
    if is_incomplete_command(output):
        try:
            page = help_lookup(command)
        except Exception:  # pragma: no cover - advisory path must never crash
            return None
        return build_incomplete_hint(page or "")
    located = locate_bad_token(command, output, prompt)
    if located is None:
        return None
    valid_prefix, bad_token, _idx = located
    # Tokens typed AFTER the rejected word — kept so the suggestion is the whole
    # corrected command, not just the first fixed token.
    trailing = tuple(command.split()[_idx + 1:])
    # Bug 5 — capability-aware: when the device's ^ caret lands on a known
    # capability keyword (e.g. 'vlan'), the rejection PROVES the device lacks
    # that feature. Surface capability-appropriate alternatives instead of a
    # misleading typo suggestion. Device-driven (only fires on a real rejection).
    capability = build_capability_hint(bad_token)
    if capability is not None:
        return capability
    try:
        page = help_lookup(valid_prefix)
    except Exception:  # pragma: no cover - advisory path must never crash
        return None
    # Bug 5 (caret-fallback) — the ^ caret often lands on the trailing token
    # (e.g. 'brief' in `show vlan brief`) rather than the capability keyword
    # itself, so build_capability_hint(bad_token) above misses it. When the
    # LAST token of the valid prefix IS a capability keyword AND the device's
    # `<prefix> ?` help comes back empty or as an error, the device has
    # AUTHORITATIVELY proven it lacks that feature (e.g. iosv rejects
    # `show vlan ?`). Surface the capability advisory. Still device-driven:
    # on an L2/L3 device `show vlan ?` returns real options, so this never
    # fires there and normal suggestion handling proceeds.
    prefix_tokens = valid_prefix.split()
    if prefix_tokens:
        last_keyword = _expand_capability_keyword(prefix_tokens[-1])
        if last_keyword is not None and (
            not (page or "").strip() or output_has_error(page)
        ):
            cap_fallback = build_capability_hint(last_keyword)
            if cap_fallback is not None:
                return cap_fallback
    options, has_cr = parse_help_options(page or "")
    # Bug G — Platform-Dependent token rejected. A known Cat9k-only subsystem
    # (e.g. 'fed') typed on a C8000v is rejected; surface the platform-correct
    # alternative sourced from the device's OWN '<prefix> ?' option set.
    pd = build_pd_suggestion(valid_prefix, bad_token, options)
    if pd is not None:
        return pd
    return build_suggestion(valid_prefix, bad_token, options, has_cr, trailing)
