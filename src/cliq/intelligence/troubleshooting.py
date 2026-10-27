#!/usr/bin/env python3
"""
troubleshooting.py — Scenario-based advisory toolkits (Phase 5).

When an engineer shows intent to troubleshoot a protocol (OSPF first), CLIQ can
surface a *preliminary toolkit*: a curated, ordered shortlist of diagnostic
commands grouped by phase. This module owns:

  * loading the curated, data-driven toolkit definitions (``command_db/
    troubleshooting/*.yaml``),
  * CONSERVATIVE intent detection (explicit phrases / keyword+trigger / a
    partial protocol command),
  * building a mode- and capability-aware toolkit payload for the UI.

Design principles (mirroring the rest of CLIQ):

  * ADVISORY ONLY — nothing here runs a command. Debug commands are flagged
    ``impactful`` so the UI can warn and never auto-run them.
  * DEVICE IS THE SOURCE OF TRUTH — a command the live device just
    SYNTAX-rejected (``runtime.rejected``) is dropped from the toolkit; the
    toolkit sits at the BOTTOM of the hierarchy (Live > Cache > KB > Recipe).
  * EXTENSIBLE — a new protocol toolkit (EIGRP, BGP) is added by dropping a new
    YAML file into the toolkit directory; NO code change is required.
  * SYNCHRONOUS + IN-MEMORY — toolkits are parsed once and cached, so a lookup
    on the hot path never does I/O or an embedding call.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("cliq.troubleshooting")

# Default directory holding the curated toolkit YAML files. Overridable via env
# for tests / alternate deployments.
_TOOLKITS_DIR = os.environ.get(
    "CLIQ_TROUBLESHOOTING_DIR", "./command_db/troubleshooting"
)

# Troubleshoot-intent words. Combined with a protocol keyword (e.g. "ospf")
# these confirm a diagnostic intent without over-triggering on unrelated typing.
_TROUBLESHOOT_TRIGGERS: frozenset[str] = frozenset({
    "troubleshoot", "troubleshooting", "debug", "diagnose", "diagnostic",
    "down", "not working", "isn't working", "isnt working", "broken",
    "issue", "issues", "problem", "problems", "why", "fail", "failing",
    "failed", "flap", "flapping", "stuck", "adjacency", "neighbor",
    "neighbour", "not forming", "won't form", "wont form", "check", "verify",
})

# Broader "working/intent" signals — combined with a protocol alias these
# indicate the engineer is working on / asking about the protocol, covering
# phrasings like "working on ospf", "need ospf commands", "help with ospf".
# Every trigger REQUIRES a protocol alias to be present, so these never fire on
# unrelated input (a bare signal word with no protocol alias is ignored).
_WORKING_SIGNALS: frozenset[str] = frozenset({
    "working on", "work on", "workin on", "i'm on", "im on", " on ",
    "need", "help with", "help me with", "commands", "command", "list",
    "show me", "how do i", "how to", "looking at", "dealing with",
    "configuring", "configure", "setup", "set up",
})

# Default signal set every recipe inherits (union of the two above) so a recipe
# YAML need only list PROTOCOL-SPECIFIC extra triggers, not re-declare these.
_DEFAULT_SIGNALS: frozenset[str] = _TROUBLESHOOT_TRIGGERS | _WORKING_SIGNALS

# Stable UI safety note for impactful (debug) commands.
DEBUG_SAFETY_NOTE = (
    "debug commands can flood the console on a live device — run briefly and "
    "disable with 'undebug all'. CLIQ never runs these for you."
)

# Clear / debug commands change device state or console load — flagged impactful.
CLEAR_SAFETY_NOTE = (
    "clear/debug commands are impactful on a live device (reset counters/"
    "adjacencies, flood the console). CLIQ never runs these for you."
)


@dataclass(frozen=True)
class ToolkitIntent:
    """Conservative, data-driven trigger definition for a toolkit.

    ``protocol_aliases`` lets a recipe declare synonyms (e.g. ``ospfv2``) so a
    match does not depend on the literal protocol token. ``triggers`` are
    PROTOCOL-SPECIFIC signal words merged on top of the built-in default signal
    set — a match requires an alias AND a signal (or an explicit phrase / a
    partial protocol command), keeping detection conservative.
    """
    keywords: tuple[str, ...] = ()
    phrases: tuple[str, ...] = ()
    command_prefixes: tuple[str, ...] = ()
    protocol_aliases: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()

    def aliases(self, protocol: str) -> tuple[str, ...]:
        """All protocol synonyms to look for (keywords + declared aliases)."""
        merged = set(self.keywords) | set(self.protocol_aliases) | {protocol}
        return tuple(sorted(a for a in merged if a))

    def signals(self) -> frozenset[str]:
        """Signal words that (with an alias present) confirm intent."""
        return _DEFAULT_SIGNALS | frozenset(self.triggers)


@dataclass(frozen=True)
class ToolkitCommand:
    """One diagnostic command in a toolkit phase."""
    command: str
    description: str = ""
    impactful: bool = False


@dataclass(frozen=True)
class ToolkitPhase:
    """An ordered group of diagnostic commands (one diagnostic phase)."""
    name: str
    commands: tuple[ToolkitCommand, ...] = ()


@dataclass(frozen=True)
class ToolkitDiscovery:
    """Data-driven configuration for Feature 3 command discovery / reference.

    ``walk_paths`` seed the live device ``?``-tree walk (e.g. ``show ip ospf``,
    ``debug ip ospf``). ``learned_prefixes`` select this protocol's learned
    templates. ``kb_query`` drives the KB/RAG retrieval. All optional — a recipe
    without a ``discovery`` block falls back to sensible defaults derived from
    the intent command prefixes.
    """
    walk_paths: tuple[str, ...] = ()
    learned_prefixes: tuple[str, ...] = ()
    kb_query: str = ""


@dataclass(frozen=True)
class Toolkit:
    """A curated, protocol-scoped troubleshooting toolkit."""
    protocol: str
    title: str
    required_capability: str
    intent: ToolkitIntent
    phases: tuple[ToolkitPhase, ...] = ()
    discovery: ToolkitDiscovery = field(default_factory=ToolkitDiscovery)

    def walk_paths(self) -> tuple[str, ...]:
        """Effective ``?``-walk seed paths (explicit, else derived from intent)."""
        if self.discovery.walk_paths:
            return self.discovery.walk_paths
        # Fallback: derive from the intent command prefixes + a clear path.
        derived = list(self.intent.command_prefixes)
        for pfx in list(derived):
            if pfx.startswith("show "):
                derived.append("clear " + pfx[len("show "):])
        return tuple(dict.fromkeys(derived))

    def learned_prefixes(self) -> tuple[str, ...]:
        if self.discovery.learned_prefixes:
            return self.discovery.learned_prefixes
        return self.intent.command_prefixes

    def kb_query(self) -> str:
        return self.discovery.kb_query or self.protocol


# In-memory cache keyed by the (absolute) directory the toolkits were loaded
# from, so tests can load an alternate directory without disturbing the default.
_cache: dict[str, dict[str, Toolkit]] = {}


def _coerce_toolkit(data: dict) -> Optional[Toolkit]:
    """Build a :class:`Toolkit` from parsed YAML, or None if it is malformed."""
    protocol = str(data.get("protocol", "")).strip().lower()
    if not protocol:
        return None
    intent_raw = data.get("intent", {}) or {}
    intent = ToolkitIntent(
        keywords=tuple(
            str(k).strip().lower() for k in (intent_raw.get("keywords") or []) if str(k).strip()
        ),
        phrases=tuple(
            str(p).strip().lower() for p in (intent_raw.get("phrases") or []) if str(p).strip()
        ),
        command_prefixes=tuple(
            str(c).strip().lower() for c in (intent_raw.get("command_prefixes") or []) if str(c).strip()
        ),
        protocol_aliases=tuple(
            str(a).strip().lower() for a in (intent_raw.get("protocol_aliases") or []) if str(a).strip()
        ),
        triggers=tuple(
            str(t).strip().lower() for t in (intent_raw.get("triggers") or []) if str(t).strip()
        ),
    )
    phases: list[ToolkitPhase] = []
    for phase_raw in (data.get("phases") or []):
        name = str(phase_raw.get("name", "")).strip()
        cmds: list[ToolkitCommand] = []
        for c in (phase_raw.get("commands") or []):
            command = str(c.get("command", "")).strip()
            if not command:
                continue
            cmds.append(ToolkitCommand(
                command=command,
                description=str(c.get("description", "")).strip(),
                impactful=bool(c.get("impactful", False)),
            ))
        if name and cmds:
            phases.append(ToolkitPhase(name=name, commands=tuple(cmds)))
    if not phases:
        return None
    disc_raw = data.get("discovery", {}) or {}
    discovery = ToolkitDiscovery(
        walk_paths=tuple(
            str(w).strip().lower() for w in (disc_raw.get("walk_paths") or []) if str(w).strip()
        ),
        learned_prefixes=tuple(
            str(p).strip().lower() for p in (disc_raw.get("learned_prefixes") or []) if str(p).strip()
        ),
        kb_query=str(disc_raw.get("kb_query", "")).strip(),
    )
    return Toolkit(
        protocol=protocol,
        title=str(data.get("title", f"{protocol.upper()} Troubleshooting Toolkit")).strip(),
        required_capability=str(data.get("required_capability", "")).strip().lower(),
        intent=intent,
        phases=tuple(phases),
        discovery=discovery,
    )


def get_toolkit(protocol: str, directory: Optional[str] = None) -> Optional[Toolkit]:
    """Return the loaded :class:`Toolkit` for ``protocol``, or None."""
    return load_toolkits(directory).get((protocol or "").strip().lower())


def load_toolkits(directory: Optional[str] = None, *, refresh: bool = False) -> dict[str, Toolkit]:
    """Load every curated toolkit YAML from ``directory`` (cached by directory).

    Best-effort: a missing directory or an unparsable file logs and is skipped
    so the advisory path never crashes. Extensible: any ``*.yaml`` dropped into
    the directory is discovered — no code change needed.
    """
    path = Path(directory or _TOOLKITS_DIR)
    key = str(path.resolve())
    if not refresh and key in _cache:
        return _cache[key]

    toolkits: dict[str, Toolkit] = {}
    try:
        import yaml
        if path.is_dir():
            for fp in sorted(path.glob("*.yaml")):
                try:
                    with fp.open("r", encoding="utf-8") as fh:
                        data = yaml.safe_load(fh) or {}
                    tk = _coerce_toolkit(data)
                    if tk is not None:
                        toolkits[tk.protocol] = tk
                    else:
                        log.warning("Skipping malformed toolkit file: %s", fp)
                except Exception as exc:  # pragma: no cover - defensive
                    log.error("Failed to load toolkit %s: %s", fp, exc)
            log.info("Loaded %d troubleshooting toolkit(s) from %s", len(toolkits), path)
        else:
            log.warning("Troubleshooting toolkit dir not found: %s", path)
    except Exception as exc:  # pragma: no cover - yaml import/other failure
        log.error("Failed to load troubleshooting toolkits (%s): %s", path, exc)

    _cache[key] = toolkits
    return toolkits


def _normalize(text: str) -> str:
    """Lowercase, strip surrounding punctuation, collapse whitespace.

    Punctuation is turned into spaces (apostrophes are dropped so ``i'm`` →
    ``im``) so trigger/alias matching is robust to phrasing and punctuation.
    """
    t = (text or "").lower()
    t = t.replace("'", "")
    t = re.sub(r"[^a-z0-9\- ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _alias_present(text: str, tokens: set[str], alias: str) -> bool:
    """True when a protocol alias (single- or multi-word) appears in text."""
    if " " in alias:
        return alias in text
    return alias in tokens


def detect_toolkit_intent(
    text: str, directory: Optional[str] = None, *, fuzzy: bool = False,
) -> Optional[str]:
    """Return the protocol of a matching toolkit for ``text``, or None.

    CONSERVATIVE and DATA-DRIVEN. A match requires one of:
      1. an explicit phrase from ``intent.phrases`` (most specific), OR
      2. the text being a partial protocol command (``intent.command_prefixes``),
         OR
      3. a protocol ALIAS present together with a SIGNAL word (troubleshoot or
         working/asking signal) — the alias gate means a bare signal never fires
         and a bare protocol word (no signal) never fires either.

    When ``fuzzy`` is True an extra tolerant pass (edit-distance on aliases) runs
    LAST — intended for the async advisory path only, never the keystroke hot
    path. It still requires a signal, so it stays conservative.
    """
    t = _normalize(text)
    if not t:
        return None
    tokens = set(t.split())
    toolkits = load_toolkits(directory)
    for tk in toolkits.values():
        intent = tk.intent
        # 1. Explicit phrase (strongest).
        if any(p and _normalize(p) in t for p in intent.phrases):
            return tk.protocol
        # 2. Partial protocol command context (e.g. typing `show ip ospf`).
        if any(pfx and t.startswith(_normalize(pfx)) for pfx in intent.command_prefixes):
            return tk.protocol
        # 3. Protocol alias + a signal word.
        aliases = intent.aliases(tk.protocol)
        if any(_alias_present(t, tokens, a) for a in aliases):
            signals = intent.signals()
            if any((s.strip() in t if " " in s else s in tokens) for s in signals):
                return tk.protocol
    # 4. Optional tolerant fallback (async path only).
    if fuzzy:
        return _fuzzy_toolkit_match(t, tokens, toolkits)
    return None


def _fuzzy_toolkit_match(
    t: str, tokens: set[str], toolkits: dict[str, "Toolkit"],
) -> Optional[str]:
    """Edit-distance tolerant alias match (e.g. ``ospff`` → ``ospf``).

    Requires a signal word too, so a mistyped protocol alone still won't fire.
    Non-blocking and cheap (difflib over a handful of short aliases).
    """
    import difflib
    for tk in toolkits.values():
        intent = tk.intent
        signals = intent.signals()
        has_signal = any((s.strip() in t if " " in s else s in tokens) for s in signals)
        if not has_signal:
            continue
        for alias in intent.aliases(tk.protocol):
            if " " in alias:
                continue
            for tok in tokens:
                if len(tok) >= 4 and difflib.SequenceMatcher(None, tok, alias).ratio() >= 0.82:
                    return tk.protocol
    return None


# ---------------------------------------------------------------------------
# Feature 3 — Command Discovery / Reference mode.
#
# Distinct from the curated preliminary toolkit: the engineer asks for the FULL
# or a SCOPED command set for a protocol ("show me all ospf commands", "ospf
# debug commands"). Detection returns (protocol, scope); the server then compiles
# a live-validated list from Device ?-walk + Learned templates + KB/RAG and hands
# the raw source lists to :func:`merge_discovery_sources` for authority-ranked,
# scope-filtered, safety-marked presentation.
# ---------------------------------------------------------------------------

# Words that signal an ENUMERATION request (the "give me the whole/among set"
# intent that distinguishes discovery from a single-command or troubleshoot ask).
_DISCOVERY_ENUMERATORS: frozenset[str] = frozenset({
    "all", "every", "everything", "which", "what", "available", "supported",
    "reference", "full", "complete", "set", "list", "options", "possible",
})

# Scope keywords → the command family the reference should be limited to.
_SCOPE_WORDS: dict[str, str] = {
    "debug": "debug",
    "clear": "clear",
    "config": "config",
    "configuration": "config",
    "configure": "config",
    "neighbor": "neighbor",
    "neighbour": "neighbor",
    "show": "show",
}


def _discovery_scope(t: str, tokens: set[str]) -> str:
    """Determine the reference scope from the query text.

    ``all`` (default) unless an explicit narrowing word is present. ``all`` /
    ``every`` win over a stray ``show`` (``show me all ospf commands`` → all).
    """
    if "all" in tokens or "every" in tokens or "everything" in tokens:
        return "all"
    for word in ("debug", "clear", "config", "configuration", "configure",
                 "neighbor", "neighbour", "show"):
        if word in tokens:
            return _SCOPE_WORDS[word]
    return "all"


def detect_discovery_intent(
    text: str, directory: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """Return ``(protocol, scope)`` for a command-discovery request, or None.

    Requires a protocol alias AND an enumeration signal so it does NOT fire for
    a plain troubleshoot ask (e.g. ``ospf commands`` alone → toolkit, not
    discovery). ``scope`` ∈ {all, show, debug, clear, config, neighbor}.
    """
    t = _normalize(text)
    if not t:
        return None
    tokens = set(t.split())
    has_cmd = "command" in tokens or "commands" in tokens
    has_enum = any(e in tokens for e in _DISCOVERY_ENUMERATORS) or "show me" in t
    for tk in load_toolkits(directory).values():
        aliases = tk.intent.aliases(tk.protocol)
        if not any(_alias_present(t, tokens, a) for a in aliases):
            continue
        scope = _discovery_scope(t, tokens)
        explicit_scope = scope != "all" and any(w in tokens for w in _SCOPE_WORDS)
        # Discovery = "give me the command set": needs the word 'command(s)' with
        # an enumerator or an explicit family scope, OR the word 'reference'.
        if (has_cmd and (has_enum or explicit_scope)) or "reference" in tokens:
            return (tk.protocol, scope)
    return None


def _command_group(base: str) -> str:
    """Classify a command into a reference group (Show / Debug / Clear / Config)."""
    b = (base or "").strip().lower()
    if b.startswith("do "):
        b = b[3:].strip()
    if b.startswith("show"):
        return "Show"
    if b.startswith("debug") or b.startswith("undebug"):
        return "Debug"
    if b.startswith("clear"):
        return "Clear"
    return "Config"


# Reference groups render in this order.
_GROUP_ORDER: tuple[str, ...] = ("Show", "Config", "Clear", "Debug")


def _scope_matches(base: str, scope: str) -> bool:
    """True when a command belongs in the requested reference scope."""
    b = (base or "").strip().lower()
    if scope == "all":
        return True
    if scope == "neighbor":
        return "neighbor" in b or "neighbour" in b
    if scope == "config":
        return not b.startswith(("show", "debug", "undebug", "clear"))
    return b.startswith(scope)


def merge_discovery_sources(
    protocol: str,
    *,
    device: "tuple[str, ...] | list[str]" = (),
    learned: "tuple[str, ...] | list[str]" = (),
    kb: "tuple[str, ...] | list[str]" = (),
    scope: str = "all",
    mode: str = "exec",
    capability: frozenset = frozenset(),
    required_capability: str = "routing",
    rejected: frozenset = frozenset(),
    mode_adapter: Optional[Callable[[str, str], str]] = None,
    device_available: bool = True,
    title: Optional[str] = None,
) -> Optional[dict]:
    """Merge Device / Learned / KB command lists into a grouped reference payload.

    Authority order Device > Learned > KB: when the SAME command appears in
    multiple sources, the highest-authority source wins (and a KB-only command is
    flagged ``verified=False`` — "unverified on this platform"). Applies scope
    filtering, capability gating, ``rejected`` suppression, mode adaptation
    (reusing ``mode_adapter``) and impactful (debug/clear) safety marking.

    Returns ``None`` when capability-gated out or nothing survives filtering.
    """
    if not capability_allows(required_capability, capability):
        return None

    adapt = mode_adapter or (lambda cmd, _mode: cmd)
    rej = {c.strip().lower() for c in (rejected or set())}

    # Highest-authority source wins per base command.
    ordered_sources = (("device", 3, device), ("learned", 2, learned), ("kb", 1, kb))
    best: dict[str, tuple[int, str, str]] = {}
    for source, authority, cmds in ordered_sources:
        for raw in (cmds or []):
            base = re.sub(r"\s+", " ", (raw or "").strip())
            if not base:
                continue
            key = base.lower()
            if key in rej:
                continue
            if not _scope_matches(base, scope):
                continue
            prev = best.get(key)
            if prev is None or authority > prev[0]:
                best[key] = (authority, source, base)

    if not best:
        return None

    groups: dict[str, list[dict]] = {}
    has_impactful = False
    sources_present: set[str] = set()
    for authority, source, base in best.values():
        grp = _command_group(base)
        impactful = grp in ("Debug", "Clear")
        has_impactful = has_impactful or impactful
        sources_present.add(source)
        groups.setdefault(grp, []).append({
            "command": adapt(base, mode),
            "base_command": base,
            "description": "",
            "impactful": impactful,
            "source": source,
            "verified": source == "device",
            "group": grp,
            "mode": mode,
        })

    # Sort within each group: verified (device) first, then a-z.
    ordered_groups: list[dict] = []
    for name in _GROUP_ORDER:
        items = groups.get(name)
        if not items:
            continue
        items.sort(key=lambda e: (0 if e["verified"] else 1, e["base_command"]))
        ordered_groups.append({"name": name, "commands": items})

    proto_upper = protocol.upper()
    unverified_note = ""
    if not device_available:
        unverified_note = (
            "Device offline — this reference is assembled from learned + KB only. "
            "Every command is UNVERIFIED on this platform; confirm with '?' when connected."
        )
    elif any(not e["verified"] for grp in ordered_groups for e in grp["commands"]):
        unverified_note = (
            "Commands marked KB are UNVERIFIED on this platform — confirm with the "
            "device's own '?' help before relying on them."
        )

    return {
        "protocol": protocol,
        "scope": scope,
        "title": title or f"{proto_upper} Command Reference — Live + Learned + KB",
        "groups": ordered_groups,
        "sources_present": sorted(sources_present),
        "safety_note": CLEAR_SAFETY_NOTE if has_impactful else "",
        "unverified_note": unverified_note,
        "device_available": device_available,
    }


def capability_allows(required_capability: str, capability: frozenset) -> bool:
    """True when a toolkit's required capability is compatible with the device.

    Mirrors the existing SOFT capability gating (see platform_rules /
    ``_reconcile_qa_result``): a specific requirement is suppressed ONLY when the
    device has a KNOWN capability set that lacks it. An empty/unknown capability
    set is permissive (the device decides), and a toolkit with no requirement
    always passes.
    """
    req = (required_capability or "").strip().lower()
    if not req:
        return True
    caps = capability or frozenset()
    if not caps:
        return True  # unknown → permissive, mirror device-is-truth
    return req in caps


def build_toolkit_payload(
    protocol: str,
    *,
    mode: str = "exec",
    capability: frozenset = frozenset(),
    rejected: frozenset = frozenset(),
    mode_adapter: Optional[Callable[[str, str], str]] = None,
    directory: Optional[str] = None,
) -> Optional[dict]:
    """Build the mode/capability-aware wire payload for a toolkit, or None.

    Returns ``None`` when the toolkit is unknown OR suppressed by capability
    gating (e.g. an OSPF toolkit on an L2-only switch). Otherwise returns a dict:

    ``{protocol, title, required_capability, safety_note, phases: [{name,
    commands: [{command, base_command, description, impactful, mode}]}]}``

    * ``mode_adapter(base_command, mode)`` — an injected callable that renders a
      command for the current CLI mode (e.g. prefixing ``do `` for an EXEC-only
      verb in config mode). Reuses the server's existing do-gating logic so mode
      handling is NOT duplicated here. Defaults to identity.
    * ``rejected`` — bare commands the live device SYNTAX-rejected. A rejected
      base command is DROPPED from the toolkit (device is the source of truth).
    * A phase left empty after rejection filtering is dropped entirely.
    """
    tk = load_toolkits(directory).get((protocol or "").strip().lower())
    if tk is None:
        return None
    if not capability_allows(tk.required_capability, capability):
        return None

    adapt = mode_adapter or (lambda cmd, _mode: cmd)
    rej = {c.strip().lower() for c in (rejected or set())}

    phases_out: list[dict] = []
    has_impactful = False
    for phase in tk.phases:
        cmds_out: list[dict] = []
        for c in phase.commands:
            base = c.command.strip()
            if base.lower() in rej:
                continue  # device rejected it — defer to the device
            display = adapt(base, mode)
            has_impactful = has_impactful or c.impactful
            cmds_out.append({
                "command": display,
                "base_command": base,
                "description": c.description,
                "impactful": c.impactful,
                "mode": mode,
            })
        if cmds_out:
            phases_out.append({"name": phase.name, "commands": cmds_out})

    if not phases_out:
        return None

    return {
        "protocol": tk.protocol,
        "title": tk.title,
        "required_capability": tk.required_capability,
        "safety_note": DEBUG_SAFETY_NOTE if has_impactful else "",
        "phases": phases_out,
    }
