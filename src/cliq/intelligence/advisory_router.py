#!/usr/bin/env python3
"""
advisory_router.py — Unified post-Enter advisory priority engine for CLIQ.

CLIQ shows advisories in an SSH terminal both BEFORE the user presses Enter
(context-aware: it knows the CLI mode and intent) and AFTER Enter (parsing the
device's raw error output). Historically the post-Enter handler DROPPED the
mode/intent context the pre-Enter engine already had and just echoed the raw
parser-completion options ("Exec, access-profile, alps..." / every interface
type). This module fixes that by:

  * CARRYING the pre-Enter verdict (current CLI mode + detected intent) into the
    post-Enter decision instead of re-parsing the raw ``^`` error, and
  * enforcing a STRICT advisory priority so only the single highest-value
    advisory is ever surfaced:

        1. Destructive-action confirmation (red y/n)   [highest — safety first]
        2. Mode/context fix (carried pre-Enter state; includes 'do' prefix tip)
        3. Abbreviation expansion (expand + confirm)
        4. Incomplete-command argument hint
        5. Fuzzy "Did you mean" — ONLY if 1–4 produce nothing

The module is PURE and injectable (device I/O arrives via a ``help_lookup``
callable and an optional ``expand_fn``) so every tier is unit-testable offline.

Cardinal rules (mirroring the rest of CLIQ):
    * Advisory only — nothing is auto-run, auto-corrected or auto-deleted. A
      destructive confirmation surfaces a y/n the engineer must consciously
      answer; execution stays under user control.
    * Offline-first — no external/cloud calls; only the local tree-structured
      Knowledge Base and the device's own ``?``-help.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from cliq.intelligence.did_you_mean import (
    DidYouMean,
    analyze_device_error,
    build_incomplete_hint,
    is_incomplete_command,
    is_protected_substitution,
    is_syntax_error,
    output_has_error,
    PROTECTED_GLOBAL_COMMANDS,
)

# ---------------------------------------------------------------------------
# Terminal ANSI colours. Used ONLY for destructive-action confirmations so the
# danger stands out; every other advisory keeps the plain 💡 / ⚡ format.
# ---------------------------------------------------------------------------
ANSI_RED = "\x1b[31m"
ANSI_BOLD_RED = "\x1b[1;31m"
ANSI_RESET = "\x1b[0m"


# Advisory tier identifiers (also used as the observability label so logs read
# ``tier=destructive`` / ``tier=mode_context`` etc.).
TIER_DESTRUCTIVE = "destructive"
TIER_MODE_CONTEXT = "mode_context"
TIER_ABBREVIATION = "abbreviation"
TIER_INCOMPLETE = "incomplete"
TIER_FUZZY = "fuzzy"

# Numeric priority (lower fires first) — mirrors the strict order above so a
# caller can compare/sort tiers if needed.
TIER_PRIORITY: dict[str, int] = {
    TIER_DESTRUCTIVE: 1,
    TIER_MODE_CONTEXT: 2,
    TIER_ABBREVIATION: 3,
    TIER_INCOMPLETE: 4,
    TIER_FUZZY: 5,
}


@dataclass
class Advisory:
    """A single surfaced advisory plus which priority tier produced it."""

    tier: str                    # one of the TIER_* constants
    kind: str                    # frontend routing hint (wrong_mode / typo / ...)
    message: str                 # plain text (💡 lightbulb / advisory card)
    fix: Optional[str] = None    # one-key insertable corrected command, if any
    # Destructive confirmations carry a red-wrapped variant for a raw terminal
    # write; non-destructive advisories leave this None.
    ansi_message: Optional[str] = None
    danger: bool = False         # True only for destructive confirmations

    @property
    def priority(self) -> int:
        return TIER_PRIORITY.get(self.tier, 99)


# ===========================================================================
# TIER 1 — Destructive-action confirmation engine
# ===========================================================================
#
# Any deletion / negation / destructive command must surface a RED y/n
# confirmation naming the exact object being changed, BEFORE the engineer
# commits to it. The command list is CONFIGURABLE/EXTENSIBLE via the module
# frozensets below — add a verb to broaden coverage without touching the logic.

# Standalone destructive verbs (first token). ``no`` (negation) is handled
# separately so its object can be named dynamically.
DESTRUCTIVE_HEADS: frozenset[str] = frozenset({
    "shutdown",   # admin-down an interface
    "clear",      # clear counters / tables / sessions
    "erase",      # erase nvram / startup-config
    "reload",     # reboot the device
    "delete",     # delete a file
    "format",     # format a filesystem
})

# Multi-word destructive commands (matched as a leading phrase).
DESTRUCTIVE_PHRASES: tuple[tuple[str, ...], ...] = (
    ("write", "erase"),
    ("no", "shutdown"),  # listed so it is NOT mis-phrased as a plain deletion
)

# A leading ``no`` negates/removes config — always treated as destructive so
# the engineer confirms the object being torn down.
NEGATION_HEAD = "no"


def is_destructive_command(command: str) -> bool:
    """True when ``command`` deletes, negates or is otherwise destructive.

    Covers any ``no ...`` negation plus the extensible standalone-verb and
    phrase lists above. Purely lexical (offline, no device round-trip) so it is
    safe on the fast keystroke path.
    """
    toks = (command or "").strip().lower().split()
    if not toks:
        return False
    if toks[0] == NEGATION_HEAD:
        return True
    if toks[0] in DESTRUCTIVE_HEADS:
        return True
    for phrase in DESTRUCTIVE_PHRASES:
        if tuple(toks[: len(phrase)]) == phrase:
            return True
    return False


# Location suffixes derived from the current CLI mode, so the confirmation can
# say "…on this subinterface" / "…on this interface".
_MODE_LOCATION: dict[str, str] = {
    "config-subif": " on this subinterface",
    "config-if": " on this interface",
}


def _location_for_mode(mode: str) -> str:
    return _MODE_LOCATION.get((mode or "").strip().lower(), "")


def describe_destructive_object(command: str, mode: str = "") -> tuple[str, str]:
    """Return ``(verb, object_description)`` naming what a command changes.

    Dynamically parses the affected object (IP / interface / config item) from
    the command so the confirmation is specific, e.g.::

        no ip address 192.168.10.1 255.255.255.0  (config-subif)
            -> ("DELETE", "IP 192.168.10.1 255.255.255.0 on this subinterface")

    Falls back to a generic phrasing for anything not specially recognised.
    """
    toks = (command or "").strip().split()
    low = [t.lower() for t in toks]
    loc = _location_for_mode(mode)

    # ``no shutdown`` re-enables — negation, but not a deletion. Phrase honestly.
    if low[:2] == ["no", "shutdown"]:
        return "RE-ENABLE", f"this interface{loc or ''}".strip()

    # ``no ip address [ip mask]`` — the canonical subinterface case.
    if low[:3] == ["no", "ip", "address"]:
        operands = " ".join(toks[3:]).strip()
        obj = f"IP {operands}" if operands else "the IP address"
        return "DELETE", f"{obj}{loc}"

    # Generic ``no <rest>`` negation — name the config item being removed.
    if low[:1] == ["no"]:
        rest = " ".join(toks[1:]).strip()
        obj = f"'{rest}'" if rest else "this configuration"
        return "REMOVE", f"{obj}{loc}"

    # ``shutdown`` — admin-down the current interface.
    if low[:1] == ["shutdown"]:
        return "SHUT DOWN", f"this interface{loc or ''}".strip()

    if low[:2] == ["write", "erase"]:
        return "ERASE", "the startup configuration"

    if low[:1] == ["erase"]:
        target = " ".join(toks[1:]).strip() or "the startup configuration"
        return "ERASE", target

    if low[:1] == ["reload"]:
        return "RELOAD", "the entire device"

    if low[:1] == ["clear"]:
        target = " ".join(toks[1:]).strip() or "device state"
        return "CLEAR", target

    if low[:1] == ["delete"]:
        target = " ".join(toks[1:]).strip() or "a file"
        return "DELETE", target

    if low[:1] == ["format"]:
        target = " ".join(toks[1:]).strip() or "a filesystem"
        return "FORMAT", target

    # Unknown destructive verb — keep it safe and generic.
    return "CHANGE", (" ".join(toks) or "this configuration")


def build_destructive_confirmation(
    command: str, mode: str = "",
) -> Optional[Advisory]:
    """Build the RED y/n confirmation advisory for a destructive command.

    Returns ``None`` when ``command`` is not destructive. The plain ``message``
    is used by the 💡 lightbulb / advisory card; ``ansi_message`` carries the
    same text wrapped in terminal red for a raw console write. Advisory only —
    CLIQ never auto-runs the deletion; the engineer answers the y/n themselves.
    """
    if not is_destructive_command(command):
        return None
    verb, obj = describe_destructive_object(command, mode)
    core = f"You are about to {verb} {obj} \u2014 confirm? (y/n)"
    return Advisory(
        tier=TIER_DESTRUCTIVE,
        kind=TIER_DESTRUCTIVE,
        message=core,
        fix=None,
        ansi_message=f"{ANSI_BOLD_RED}{core}{ANSI_RESET}",
        danger=True,
    )


# ===========================================================================
# TIER 1 — Blocking destructive-action CONFIRMATION GATE
# ===========================================================================
#
# The gate is the ACTIVE, stateful counterpart to the passive advisory above.
# It lives in the terminal-server input path: when the engineer presses Enter
# on a destructive command, the gate HOLDS the command (it is never forwarded
# to the device) and surfaces the red y/n confirmation. Only an explicit 'y'
# releases the held command to the device; 'n' discards it; anything else
# re-prompts while keeping the command held. Pure and offline — the device I/O
# (actually forwarding the released command) is performed by the caller, so the
# whole state machine is unit-testable without a live session.

# Explicit affirmative / negative answers. A single keystroke is the norm, but
# the spelled-out words are accepted too. Everything ELSE re-prompts.
CONFIRM_YES: frozenset[str] = frozenset({"y", "yes"})
CONFIRM_NO: frozenset[str] = frozenset({"n", "no"})

# Gate resolution outcomes (also used as observability outcome labels).
CONFIRM_FORWARD = "forward"    # 'y' — release the held command to the device
CONFIRM_DISCARD = "discard"    # 'n' — drop the held command, never sent
CONFIRM_REPROMPT = "reprompt"  # any other key / bare Enter — keep it held
CONFIRM_IDLE = "idle"          # no command is currently held for this device


def resolve_confirmation_answer(answer: str) -> str:
    """Classify a raw keystroke answer into a gate decision.

    Returns one of ``CONFIRM_FORWARD`` / ``CONFIRM_DISCARD`` /
    ``CONFIRM_REPROMPT``. Case-insensitive; only an explicit yes/no resolves —
    every other key (including a bare Enter) re-prompts so the destructive
    command is never released or discarded by accident.
    """
    a = (answer or "").strip().lower()
    if a in CONFIRM_YES:
        return CONFIRM_FORWARD
    if a in CONFIRM_NO:
        return CONFIRM_DISCARD
    return CONFIRM_REPROMPT


@dataclass
class HeldCommand:
    """A destructive command held by the gate pending the operator's y/n."""

    command: str
    mode: str = ""
    raw: bool = False


class ConfirmationGate:
    """Stateful, blocking destructive-action gate, keyed per device.

    Usage (mirrors the server wiring):

        gate = ConfirmationGate()

        # On Enter, before forwarding to the device:
        advisory = gate.intercept(device_id, command, mode)
        if advisory is not None:
            show_red_prompt(advisory.ansi_message)   # HOLD — do NOT forward
        else:
            forward_to_device(command)               # not destructive

        # On the next keystroke while a command is held:
        outcome, held, reprompt = gate.resolve(device_id, key)
        if outcome == CONFIRM_FORWARD:
            forward_to_device(held.command)          # device performs the action
        elif outcome == CONFIRM_DISCARD:
            return_to_prompt()                       # never forwarded
        elif outcome == CONFIRM_REPROMPT:
            show_red_prompt(reprompt.ansi_message)   # still held

    Purely in-memory and offline; nothing is ever auto-executed.
    """

    def __init__(self) -> None:
        self._held: dict[str, HeldCommand] = {}

    def is_pending(self, device_id: str) -> bool:
        """True while a destructive command is held awaiting confirmation."""
        return device_id in self._held

    def held(self, device_id: str) -> Optional[HeldCommand]:
        """The command currently held for ``device_id`` (or ``None``)."""
        return self._held.get(device_id)

    def intercept(
        self, device_id: str, command: str, mode: str = "", *, raw: bool = False,
    ) -> Optional[Advisory]:
        """Hold ``command`` and return its red confirmation if destructive.

        Returns ``None`` when the command is NOT destructive (the caller should
        forward it to the device normally). When it IS destructive the command
        is recorded as held for ``device_id`` and the red y/n ``Advisory`` is
        returned so the caller can render it; the command must NOT be forwarded
        until :meth:`resolve` returns ``CONFIRM_FORWARD``. ``raw`` inserts
        (explicit config-block lines) bypass the gate.
        """
        if raw:
            return None
        advisory = build_destructive_confirmation(command, mode)
        if advisory is None:
            return None
        self._held[device_id] = HeldCommand(command=command, mode=mode, raw=raw)
        return advisory

    def resolve(
        self, device_id: str, answer: str,
    ) -> tuple[str, Optional[HeldCommand], Optional[Advisory]]:
        """Apply a keystroke answer to the held command for ``device_id``.

        Returns ``(outcome, held, reprompt)`` where ``outcome`` is one of the
        ``CONFIRM_*`` constants:

          * ``CONFIRM_FORWARD`` — 'y': the held command is popped and returned
            so the caller forwards it to the device.
          * ``CONFIRM_DISCARD`` — 'n': the held command is popped and returned
            (for logging) but must NEVER be forwarded.
          * ``CONFIRM_REPROMPT`` — any other key: the command stays held and a
            fresh red ``Advisory`` is returned to re-render the prompt.
          * ``CONFIRM_IDLE`` — nothing was held (no-op).
        """
        held = self._held.get(device_id)
        if held is None:
            return (CONFIRM_IDLE, None, None)
        decision = resolve_confirmation_answer(answer)
        if decision == CONFIRM_FORWARD:
            self._held.pop(device_id, None)
            return (CONFIRM_FORWARD, held, None)
        if decision == CONFIRM_DISCARD:
            self._held.pop(device_id, None)
            return (CONFIRM_DISCARD, held, None)
        # Any other key / bare Enter: keep it held and re-prompt.
        return (
            CONFIRM_REPROMPT,
            held,
            build_destructive_confirmation(held.command, held.mode),
        )

    def clear(self, device_id: str) -> None:
        """Drop any held command for ``device_id`` (e.g. on disconnect)."""
        self._held.pop(device_id, None)


# ===========================================================================
# TIER 5 (guard) — Fuzzy-matcher protection for well-known global commands
# ===========================================================================
#
# The fuzzy "did you mean" matcher must never swap a well-known GLOBAL command
# into an unrelated valid keyword that happens to be one edit away (the classic
# ``show`` -> ``flow`` failure that destroys user intent). The protection
# primitives (``PROTECTED_GLOBAL_COMMANDS`` / ``is_protected_substitution``)
# live in :mod:`cliq.intelligence.did_you_mean` — the fuzzy matcher itself — so
# they filter at the source; they are re-exported here for callers/tests.
__all_protection__ = (PROTECTED_GLOBAL_COMMANDS, is_protected_substitution)


# ===========================================================================
# TIER 3 — Abbreviation expansion (expand + confirm)
# ===========================================================================

# Cisco interface short names → canonical long names, so a device-rejected /
# ambiguous ``g0/1`` is confirmed as ``GigabitEthernet0/1`` instead of dumping
# every interface TYPE. Keys are lower-cased alpha prefixes.
_INTERFACE_ALIASES: tuple[tuple[str, str], ...] = (
    ("tengigabitethernet", "TenGigabitEthernet"),
    ("gigabitethernet", "GigabitEthernet"),
    ("fastethernet", "FastEthernet"),
    ("hundredgige", "HundredGigE"),
    ("fortygige", "FortyGigE"),
    ("twentyfivegige", "TwentyFiveGigE"),
    ("tengig", "TenGigabitEthernet"),
    ("tengige", "TenGigabitEthernet"),
    ("gigabit", "GigabitEthernet"),
    ("portchannel", "Port-channel"),
    ("port-channel", "Port-channel"),
    ("loopback", "Loopback"),
    ("tunnel", "Tunnel"),
    ("serial", "Serial"),
    ("ethernet", "Ethernet"),
    ("vlan", "Vlan"),
    ("gig", "GigabitEthernet"),
    ("gi", "GigabitEthernet"),
    ("te", "TenGigabitEthernet"),
    ("fa", "FastEthernet"),
    ("po", "Port-channel"),
    ("lo", "Loopback"),
    ("tu", "Tunnel"),
    ("se", "Serial"),
    ("eth", "Ethernet"),
    ("vl", "Vlan"),
    ("g", "GigabitEthernet"),
    ("f", "FastEthernet"),
    ("e", "Ethernet"),
    ("s", "Serial"),
)

# An interface token: an alpha prefix immediately followed by a digit-based
# id (``g0/1``, ``Gi1/0/2``, ``te0/0/0.100``).
_INTERFACE_TOKEN_RE = re.compile(r"^([A-Za-z][A-Za-z-]*)(\d[\d/.:]*)$")


def expand_interface_token(token: str) -> Optional[str]:
    """Expand a short interface name to its canonical long form, or ``None``.

    ``g0/1`` -> ``GigabitEthernet0/1``; ``te0/0/0.100`` ->
    ``TenGigabitEthernet0/0/0.100``. Returns ``None`` for a token that isn't a
    recognisable abbreviated interface (so callers leave it untouched).
    """
    m = _INTERFACE_TOKEN_RE.match((token or "").strip())
    if not m:
        return None
    alpha, tail = m.group(1).lower(), m.group(2)
    for prefix, canonical in _INTERFACE_ALIASES:
        if alpha == prefix:
            return f"{canonical}{tail}"
    return None


def expand_command_abbreviations(
    command: str, expand_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Expand keyword + interface abbreviations across a full command line.

    ``expand_fn`` (optional) resolves keyword abbreviations against the command
    model (e.g. ``sh`` -> ``show``); interface tokens are expanded locally via
    :func:`expand_interface_token`. Returns the fully-expanded command when it
    DIFFERS from the input, else ``None`` (nothing to confirm).
    """
    raw = (command or "").strip()
    if not raw:
        return None

    keyword_expanded = raw
    if expand_fn is not None:
        try:
            candidate = expand_fn(raw)
        except Exception:  # pragma: no cover - advisory path must never crash
            candidate = None
        if candidate:
            keyword_expanded = candidate

    out_tokens: list[str] = []
    for tok in keyword_expanded.split():
        iface = expand_interface_token(tok)
        out_tokens.append(iface if iface else tok)
    expanded = " ".join(out_tokens)

    if expanded and expanded.lower() != raw.lower():
        return expanded
    return None


def build_abbreviation_advisory(
    command: str,
    mode: str = "",
    expand_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[Advisory]:
    """Tier 3 — expand abbreviations and CONFIRM the intended command.

    Instead of dumping every interface TYPE for a device-rejected abbreviated
    line (``sh running-config int g0/1``), surface the expanded command for the
    engineer to confirm. Returns ``None`` when nothing expands.
    """
    expanded = expand_command_abbreviations(command, expand_fn)
    if expanded is None:
        return None
    return Advisory(
        tier=TIER_ABBREVIATION,
        kind="typo",
        message=f"Did you mean: {expanded}?",
        fix=expanded,
    )


# ===========================================================================
# TIER 2 — Mode / context fix carried from the pre-Enter verdict
# ===========================================================================

# Canonical enter-command for each config mode, used to phrase a mode-switch fix
# ("Switch to config mode: configure terminal").
_MODE_ENTER_COMMAND: dict[str, str] = {
    "config": "configure terminal",
    "config-if": "configure terminal",
    "config-subif": "configure terminal",
    "config-router": "configure terminal",
    "config-line": "configure terminal",
    "config-vlan": "configure terminal",
    "enable": "enable",
    "exec": "disable",
}


def _pre_enter_status(pre_enter) -> str:
    return (getattr(pre_enter, "status", "") or "").strip()


def build_mode_context_advisory(pre_enter) -> Optional[Advisory]:
    """Tier 2 — reuse the carried pre-Enter mode/intent verdict.

    Bug 1: an ``int g0/1`` typed in EXEC is a WRONG-MODE error the pre-Enter
    engine already diagnosed; the post-Enter handler must reuse that verdict and
    advise the mode switch — never dump the raw EXEC keyword list.

    Bug 3: ``show ...`` in config mode is a ``wrong_mode_do`` verdict; emit ONLY
    the ``do`` tip ("'show' isn't available directly in config mode — use: do
    show ...") and suppress everything else.
    """
    if pre_enter is None:
        return None
    status = _pre_enter_status(pre_enter)

    if status == "wrong_mode_do":
        do_fix = (getattr(pre_enter, "do_suggestion", "") or "").strip()
        raw = (getattr(pre_enter, "raw_input", "") or "").strip()
        head = raw.split()[0] if raw else "that command"
        if do_fix:
            return Advisory(
                tier=TIER_MODE_CONTEXT,
                kind="wrong_mode",
                message=(
                    f"'{head}' isn't available directly in config mode "
                    f"\u2014 use: {do_fix}"
                ),
                fix=do_fix,
            )

    if status == "wrong_mode":
        target_mode = None
        matched = getattr(pre_enter, "matched_command", None)
        if matched is not None:
            target_mode = getattr(matched, "mode", None)
        # Prefer an explicit fix-path enter command from the explanation.
        exp = getattr(pre_enter, "explanation", None)
        fix_path = getattr(exp, "fix_mode_path", None) if exp else None
        if fix_path:
            enter_cmd = fix_path[0][1]
        else:
            enter_cmd = _MODE_ENTER_COMMAND.get(
                (target_mode or "").strip().lower(), "configure terminal"
            )
        target_label = target_mode or "config"
        return Advisory(
            tier=TIER_MODE_CONTEXT,
            kind="wrong_mode",
            message=f"Switch to {target_label} mode: {enter_cmd}",
            fix=enter_cmd,
        )

    return None


# ===========================================================================
# Priority router — the single entry point
# ===========================================================================


def route_post_enter_advisory(
    command: str,
    output: str,
    mode: str,
    pre_enter,
    help_lookup: Callable[[str], str],
    prompt: str = "",
    expand_fn: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[Advisory]:
    """Return the single highest-priority advisory for a submitted command.

    ``pre_enter`` is the carried pre-Enter :class:`~cliq.intelligence.engine.
    Analysis` (its mode/intent verdict). ``help_lookup`` injects the device's
    own ``?``-help so this stays offline-testable.

    Priority (only ONE advisory is ever returned):
        1. Destructive-action confirmation (red y/n)
        2. Mode/context fix (from carried pre-Enter state; includes 'do' tip)
        3. Abbreviation expansion
        4. Incomplete-command argument hint
        5. Fuzzy "Did you mean" — only if 1–4 produced nothing
    """
    # ── Tier 1: destructive-action confirmation (safety first) ──────────────
    destructive = build_destructive_confirmation(command, mode)
    if destructive is not None:
        return destructive

    # ── Tier 2: carried pre-Enter mode/context verdict ──────────────────────
    ctx = build_mode_context_advisory(pre_enter)
    if ctx is not None:
        return ctx

    # Tiers 3–5 only make sense once the DEVICE has actually rejected the line
    # with a genuine SYNTAX error (a bare operational '%' message means the
    # command was valid — stay silent).
    if not output_has_error(output) or not is_syntax_error(output):
        return None

    # ── Tier 3: abbreviation expansion (expand + confirm) ───────────────────
    # Higher priority than the incomplete-argument hint: a rejected abbreviated
    # line ('sh running-config int g0/1') is confirmed as the expanded command
    # instead of dumping every interface TYPE.
    abbr = build_abbreviation_advisory(command, mode, expand_fn)
    if abbr is not None:
        return abbr

    # ── Tier 4: incomplete-command argument hint ────────────────────────────
    # An incomplete command is syntactically valid so far — ask the device what
    # argument comes next and surface the missing-argument descriptor.
    if is_incomplete_command(output):
        try:
            page = help_lookup(command)
        except Exception:  # pragma: no cover - advisory path must never crash
            page = ""
        hint = build_incomplete_hint(page or "")
        if hint is not None:
            return Advisory(
                tier=TIER_INCOMPLETE,
                kind="missing_arg",
                message=hint.message,
                fix=hint.fix,
            )
        return None

    # ── Tier 5: fuzzy "did you mean" (guarded) ──────────────────────────────
    suggestion = analyze_device_error(command, output, help_lookup, prompt)
    if suggestion is None:
        return None
    # Never suggest the exact same command that was rejected
    if getattr(suggestion, "fix", None) and suggestion.fix.strip().lower() == command.strip().lower():
        return None
    # Global-command protection: never let the fuzzy matcher swap a well-known
    # global command's first token into an unrelated keyword (show -> flow).
    if _fuzzy_corrupts_global(command, suggestion):
        return None
    return Advisory(
        tier=TIER_FUZZY,
        kind=getattr(suggestion, "kind", "typo"),
        message=suggestion.message,
        fix=suggestion.fix,
    )


def _fuzzy_corrupts_global(command: str, suggestion: DidYouMean) -> bool:
    """True when a fuzzy suggestion would corrupt a protected global command.

    Compares the FIRST token of the typed command against the first token of the
    suggested fix; if the typed head is (an abbreviation of) a protected global
    command and the fix changed it into something unrelated (show -> flow), the
    suggestion is rejected.
    """
    fix = (getattr(suggestion, "fix", "") or "").strip()
    if not fix:
        return False
    typed_head = (command or "").strip().split()[:1]
    fix_head = fix.split()[:1]
    if not typed_head or not fix_head:
        return False
    return is_protected_substitution(typed_head[0], fix_head[0])
