from __future__ import annotations

import asyncio
import bisect
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cliq.connection.cml_auth import (
    CMLAuthManager,
    check_netsim_key_health,
    normalize_cml_host,
    reservation_api_key_present,
    reservation_policy,
    resolve_reservation_access,
)
from cliq.connection.cml_client import diagnose_cml_auth_failure, is_http_auth_error
from cliq.connection.live_device import (
    CMLConsoleDevice,
    ConnectionLostError,
    InteractiveAuthRequired,
    is_help_error,
    looks_like_auth_prompt,
    mode_from_prompt,
)
from cliq.connection.session_health import (
    DeathReason,
    HealthState,
    probe_session_health,
)
from cliq.connection.ssh_console_device import ConsoleBusyError, SSHConsoleDevice
from cliq.db.query import CommandDB
from cliq.intelligence.engine import IntelligenceEngine
from cliq.intelligence.command_template import (
    extract_template,
    clean_prefix,
    redact_args,
)
from cliq.ingest.candidate_promotion import promote_on_device_acceptance
from cliq.intelligence.learned_commands import LearnedCommandStore, is_accepted
from cliq.intelligence.did_you_mean import (
    analyze_device_error as _did_you_mean,
    build_capability_hint,
    is_syntax_error,
    parse_help_options,
    response_type,
)
from cliq.intelligence.advisory_router import (
    build_destructive_confirmation,
    expand_interface_token,
    route_post_enter_advisory,
    ConfirmationGate,
    CONFIRM_FORWARD,
    CONFIRM_DISCARD,
    CONFIRM_REPROMPT,
)
from cliq.intelligence.troubleshooting import (
    build_toolkit_payload,
    detect_toolkit_intent,
    detect_discovery_intent,
    get_toolkit,
    load_toolkits,
    merge_discovery_sources,
)
from cliq.memory.command_cache import classify_device_type
from cliq.memory.store import SessionLog
from cliq.utils import observability as obs
from cliq.validation.hybrid_validator import classify_subkeyword, head_token
from cliq.validation.platform_rules import command_capability, platform_capabilities, is_redundant_mode_transition, mode_transition_target

log = logging.getLogger("cliq.web")


PROMPT_RE = re.compile(r"^([\w\-.]+)(\(([^\)]+)\))?([#>])\s*$")

# Control-char artifacts that must NEVER reach xterm.js: NUL (^@, \x00) and ETX
# (^C, \x03) plus the other non-printing C0 controls the terminal server can
# spray during a laggy/delayed read. \t \n \r are legitimate layout and kept.
_DISPLAY_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_console_display(text: str) -> str:
    """Strip control-char artifacts (``^@``/NUL, ``^C``/ETX, other C0) from any
    console bytes bound for the browser. DISPLAY-side only — it never touches
    what is written to the device, so input integrity (typed==sent) is intact.
    A no-op for already-clean text (e.g. the CML transport's cleaned deltas)."""
    if not text:
        return text
    # Literal caret-@ (bytes 0x5e 0x40) the terminal server emits as VISIBLE
    # text (NOT a NUL) — strip the exact '^@' pairing before the C0-control
    # scrub. A lone '^' error marker ('% Invalid input ... ^') is never followed
    # by '@', so the real caret marker is preserved.
    text = text.replace("^@", "")
    return _DISPLAY_CTRL_RE.sub("", text)


class AuthRequest(BaseModel):
    host: str
    username: str
    password: str


class DevicesRequest(BaseModel):
    host: str
    token: str
    lab_id: str


class SSHConnectRequest(BaseModel):
    """Connect to a REAL device via a terminal-server SSH console.

    ``password`` is used in-memory only for the live SSH session and is NEVER
    logged (observability redaction) or persisted.
    """
    host: str
    console_user: str
    console_port: str
    password: str
    device_type: str = "cisco_ios"


@dataclass
class AuthSession:
    host: str
    username: str
    password: str
    # CML sessions carry a live auth manager; SSH sessions have none.
    auth: Optional[CMLAuthManager] = None
    # Which transport this session drives: "cml" (default) or "ssh".
    connection_type: str = "cml"


@dataclass
class DeviceRuntime:
    device: "CMLConsoleDevice | SSHConsoleDevice"
    mode: str = "exec"
    status: str = "connected"
    prompt: str = ""
    node_type: str = ""
    pager_active: bool = False
    # True while the device sits at an interactive IOS question (e.g. the
    # "Configuring from terminal, memory, or network [terminal]? " prompt a
    # bare 'configure' poses). The engineer's next line is then the ANSWER,
    # fed back through the interactive path instead of the command gate.
    awaiting_input: bool = False
    help_paging: bool = False
    help_partial: str = ""
    # Detected device capability set, a subset of {"routing", "switching"}.
    # Seeded from the node type and then refined authoritatively from what the
    # device actually accepts (device is the source of truth). Used ONLY for
    # soft suggestion ranking — never to block a command.
    capability: frozenset = field(default_factory=frozenset)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Bug P — commands this device most recently SYNTAX-rejected ('^' / '%
    # Invalid'). The device is the source of truth, so the advisory/Explain
    # stack consults this set to DEFER to the device: never claim a rejected
    # command is "valid for platform X" nor offer it as a re-run fix.
    rejected: set = field(default_factory=set)
    # Feature 2 — sticky protocol context. Session-scoped, server-authoritative
    # record of the protocol the engineer is actively working on, e.g.
    # ``{"protocol": "ospf", "source": "advisory"|"cli", "ts": <epoch>,
    #    "unrelated": <int>}``. Used ONLY to BIAS suggestion ranking toward the
    # active protocol (never to filter, never on the exact-prefix hot path) and
    # to render the "Context: OSPF" chip. Cleared on explicit action, session
    # reset, idle timeout, or N consecutive unrelated commands.
    active_context: Optional[dict] = None
    # Session Health Monitor — set True once the session is confirmed DEAD
    # (reservation reclaimed / socket gone / re-auth demanded / persistent empty
    # reads). While dead, every device-backed path (command execution, ?-help
    # walks, discovery ?-walk) is halted and advisory features degrade to
    # Learned + KB. Cleared implicitly by a fresh reconnect (a new runtime is
    # minted). ``dead_reason`` carries the classified DeathReason for the notice.
    dead: bool = False
    dead_reason: str = ""
    # Session Health Monitor — count of consecutive command-path failures that
    # look like empty reads where the device prompt/output was expected. Reset to
    # 0 on any successful command; once it crosses the empty-read limit the health
    # probe classifies the session dead with reason ``empty_reads``.
    empty_read_strikes: int = 0


app = FastAPI(title="CLIQ Web API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


SESSIONS: dict[str, AuthSession] = {}
DEVICES_BY_TOKEN: dict[str, dict[str, dict[str, Any]]] = {}
# Durable per-device operational memory (history, mode, typo stats), keyed by
# lab_id/node_id so it survives CLIQ reloads/reconnects.
SESSION_LOG = SessionLog()

# Bug J — per-console EXCLUSIVE attach registry (Option A). A Cisco device
# console is a single shared TTY; two CLIQ clients on the same console interleave
# I/O and corrupt each other's history. We register the live owner keyed by the
# console path; a second attach is refused. Lab+node path is globally unique so
# this serializes across users/tokens. Per-user history stays isolated because
# the runner is the sole owner. Released on disconnect.
_CONSOLE_LOCKS: dict[str, str] = {}  # console_path -> owning session token

# Bug P (Ask path) — module-level mirror of the live ``DeviceRuntime`` objects,
# keyed by the OWNERSHIP-SCOPED ``(session_token, device_id)`` tuple. The
# advisory REST endpoints (``/api/intel/ask`` and ``/api/intel/explain``) run
# OUTSIDE the websocket handler's scope, where the per-socket ``runtimes`` dict
# lives, yet they must reconcile KB/RAG answers against the live device's
# authoritative state (the ``rejected`` set + detected ``capability``). This
# registry holds references to the SAME runtime objects the websocket loop
# mutates in place, so reads always observe current truth. Tuple keying binds
# every runtime to its owning session so a request can ONLY ever read its own
# device — a ``device_id`` belonging to another session resolves to not-found,
# never leaking that session's ``node_type``/``rejected``/``capability``. It is
# registered on connect and released on socket close. Best-effort: a missing
# entry simply means "no connected device (owned by this session)", and the
# advisory path falls back to its KB-only behavior (device > cache > KB still
# holds — there is just no device to defer to).
_DEVICE_RUNTIMES: dict[tuple[str, str], "DeviceRuntime"] = {}

# In-session reconnect — monotonic timestamp of the last moment each session
# token's browser WebSocket was seen to drop. A token present here has NO live
# socket but its server-side state (SESSIONS + DEVICES_BY_TOKEN, including any
# held SSH console password) is being PRESERVED for the reconnect grace window.
# The entry is removed the instant a socket re-attaches under that token; the
# session-grace reaper drops the whole session (and wipes creds) once the window
# elapses with no reconnect. Empty for a session with a live socket.
_SESSION_LAST_SEEN: dict[str, float] = {}


# Shared read-only intelligence stack.
# Used for: keystroke suggestions + safety validation (NOT Q&A).
_db = CommandDB()
_engine = IntelligenceEngine(_db)

# ---------------------------------------------------------------------------
# Phase 6 — advisory intelligence stack (retrieval + validation + memory).
# Initialized lazily so the server still starts if KB files are absent.
# ---------------------------------------------------------------------------
_assistant: Any = None   # IntelligentCommandAssistant | None
_validator: Any = None   # SafetyValidator | None
_memory_store: Any = None  # OperationalMemoryStore | None

_KB_DB = os.environ.get("CLIQ_KB_DB", "./kb/cliq_kb.sqlite")
_VEC_DB = os.environ.get("CLIQ_VEC_DB", "./kb/cliq_vec.sqlite")
_LEARNED_DB = os.environ.get("CLIQ_LEARNED_DB", ".cliq/learned_commands.sqlite")

# Self-learning ghost-text index (built once at startup) + learned-command cache.
_ghost_index: Any = None      # _GhostIndex | None
_learned_store: Any = None    # LearnedCommandStore | None

# Part 1 — persistent, device-scoped command + ?-help cache. DB-backed under
# .cliq/memory, populated ONLY from device-confirmed truth, two-tier keyed
# (device-type for PI, platform for PD). Survives refresh/restart and is shared
# cross-user. Lazily created so a cache failure can never block the server.
_cmd_cache: Any = None        # PersistentCommandCache | None


def _get_cmd_cache() -> Any:
    """Return the shared persistent command cache, creating it on first use.

    Best-effort: any initialization failure logs and returns None so the live
    device path is never blocked by a cache problem (device is the source of
    truth; the cache is an accelerator only)."""
    global _cmd_cache
    if _cmd_cache is None:
        try:
            from cliq.memory.command_cache import PersistentCommandCache
            _cmd_cache = PersistentCommandCache()
        except Exception as exc:  # pragma: no cover - cache must never block
            log.warning("persistent command cache unavailable: %s", exc)
            _cmd_cache = False  # sentinel: tried and failed, don't retry per call
    return _cmd_cache or None


# Lazily-opened handle to the KB CommandStore (kb/cliq_kb.sqlite) — reused here
# ONLY to promote a product-CR advisory candidate to 'trusted' once the live
# device accepts the equivalent command (two-stage integration). Best-effort:
# any failure just means promotion is skipped this time, never blocks the
# device-execute path.
_kb_store: Any = None         # CommandStore | None


def _get_kb_store() -> Any:
    global _kb_store
    if _kb_store is None:
        try:
            from cliq.ingest.storage.sqlite_store import CommandStore
            _kb_store = CommandStore(_KB_DB) if Path(_KB_DB).exists() else False
        except Exception as exc:  # pragma: no cover - must never block
            log.warning("KB store unavailable for candidate promotion: %s", exc)
            _kb_store = False
    return _kb_store or None



def _template_help_lookup(node_type: str, mode: str):
    """Build a prefix -> device ``?``-help keyword options resolver for THIS
    device, used to value-strip an accepted command into a template.

    Resolves a prefix's known keyword continuations from (1) the in-memory help
    cache captured this session, then (2) the durable cross-session cache. A
    token in an argument position (not one of these keywords) is placeholdered as
    ``<arg>``. Returns None for an unknown prefix so :func:`extract_template`
    leaves that token literal (never guesses a keyword into an operand)."""
    head_norm = (mode or "").strip().lower()

    def _lookup(prefix: str):
        opts: list[str] = []
        # (1) in-memory help captured this session (any device of this node_type).
        for key, ctx in _help_cache.items():
            parts = key.split("\x00")
            if len(parts) == 3 and parts[1] == head_norm and ctx.head == prefix.lower():
                opts = list(ctx.tokens)
                break
        # (2) durable cross-session cache (device-type / platform scoped).
        if not opts and node_type:
            try:
                cache = _get_cmd_cache()
                if cache is not None:
                    opts = cache.get_help_options(prefix, node_type, mode or "exec")
            except Exception:  # pragma: no cover - never block learning
                opts = []
        return opts or None

    return _lookup



def _init_advisory_stack() -> None:
    """Build the Phase 6 advisory stack once on first use."""
    global _assistant, _validator, _memory_store
    if _assistant is not None:
        return
    try:
        # Set offline env flags so the embedder never hits the internet.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

        from cliq.ingest.storage.sqlite_store import CommandStore
        from cliq.ingest.storage.vector_store import SQLiteVectorStore
        from cliq.retrieval.assistant import IntelligentCommandAssistant
        from cliq.retrieval.query import CommandRetriever
        from cliq.validation.validator import SafetyValidator
        from cliq.memory.operational_memory import OperationalMemoryStore

        if Path(_KB_DB).exists() and Path(_VEC_DB).exists():
            sqlite_store = CommandStore(_KB_DB)
            vector_store = SQLiteVectorStore(_VEC_DB)
            retriever = CommandRetriever(sqlite_store, vector_store)
            _assistant = IntelligentCommandAssistant(retriever, enable_memory=False)
            
            # Verify KB record count and embedder type for diagnostics.
            try:
                import sqlite3
                conn = sqlite3.connect(_KB_DB)
                record_count = conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
                conn.close()
                embedder_type = "sentence-transformers (offline)"
                if hasattr(retriever, 'embedder') and hasattr(retriever.embedder, '__class__'):
                    embedder_type = retriever.embedder.__class__.__name__
                log.info(
                    "Phase 6 KB loaded: path=%s records=%d embedder=%s",
                    _KB_DB, record_count, embedder_type,
                )
            except Exception as ex:
                log.warning("Could not verify KB stats: %s", ex)
        else:
            log.warning(
                "Phase 6 KB not found (%s / %s) — advisory endpoints will return fallback.",
                _KB_DB, _VEC_DB,
            )
        _validator = SafetyValidator()
        _memory_store = OperationalMemoryStore()
        log.info("Phase 6 validator + memory store initialized.")
    except Exception as exc:
        log.error("Failed to init Phase 6 advisory stack: %s", exc)


# ---------------------------------------------------------------------------
# Self-learning ghost-text prefix index.
#
# Built ONCE at startup from three tiered sources so per-keystroke completion is
# a fast in-memory prefix lookup — never a live semantic/embedding search:
#   tier 0 (kb)      — canonical commands (legacy command set + curated KB rows)
#   tier 1 (doc)     — commands extracted from the ingested document (PDF)
#   tier 2 (learned) — locally learned, device-accepted commands
# New accepted commands are appended to the index in real time.
# ---------------------------------------------------------------------------

# Only index clean, well-formed CLI lines (lowercase words / digits / hyphens /
# spaces). This drops OCR/extraction noise such as parenthetical annotations.
_CLEAN_CMD_RE = re.compile(r"^[a-z0-9][a-z0-9 \-]*$")

# Bug 4b — first-token slice: leading whitespace (if any) + the next single
# whitespace-delimited token. Used to keep ghost text word-by-word instead of
# spilling the entire remaining command tail.
_NEXT_TOKEN_RE = re.compile(r"^(\s*)(\S+)")


def _next_token_suffix(suffix: str) -> str:
    """Truncate a completion suffix to just the next token (Bug 4b)."""
    if not suffix:
        return ""
    m = _NEXT_TOKEN_RE.match(suffix)
    return (m.group(1) + m.group(2)) if m else suffix


class _GhostIndex:
    """In-memory, tiered prefix index for ghost-text completion."""

    def __init__(self) -> None:
        # norm_command -> [tier, accept_count]; lower tier + higher count wins.
        self._entries: dict[str, list[int]] = {}
        self._keys: list[str] = []  # sorted command keys for bisect prefix scan

    @staticmethod
    def _norm(command: str) -> str:
        return re.sub(r"\s+", " ", (command or "").strip().lower())

    def add(self, command: str, tier: int, count: int = 0, *, live: bool = False) -> None:
        norm = self._norm(command)
        if not norm or not _CLEAN_CMD_RE.match(norm):
            return
        existing = self._entries.get(norm)
        if existing is None:
            self._entries[norm] = [tier, count]
            if live:
                bisect.insort(self._keys, norm)
        else:
            existing[0] = min(existing[0], tier)
            existing[1] = max(existing[1], count)

    def finalize(self) -> None:
        self._keys = sorted(self._entries)

    def best_completion(self, prefix: str) -> str:
        """Return the inline ghost suffix for the single best prefix match."""
        norm = self._norm(prefix)
        if not norm or not self._keys:
            return ""
        # If the user typed a trailing space they want the NEXT word, so only
        # match keys that continue past a word boundary.
        if prefix != prefix.rstrip():
            norm = norm + " "
        i = bisect.bisect_left(self._keys, norm)
        n = len(self._keys)
        best: tuple[int, int, int, str] | None = None
        while i < n and self._keys[i].startswith(norm):
            key = self._keys[i]
            if key != norm:  # must have a real continuation to suggest
                tier, count = self._entries[key]
                # Rank: lowest tier, then most-accepted, then shortest, then a-z.
                cand = (tier, -count, len(key), key)
                if best is None or cand < best:
                    best = cand
            i += 1
        if best is None:
            return ""
        # Bug 4b — progressive, word-by-word ghost text. Return ONLY up to the
        # next whitespace-delimited token, never the whole remaining tail: typing
        # 'show' offers ' ip', not ' ip interface brief'. Any leading whitespace
        # (caret at a word boundary) is preserved so the next word is offered
        # cleanly.
        return _next_token_suffix(best[3][len(norm):])

    def best_completion_biased(self, prefix: str, bias: Optional[str] = None) -> str:
        """Ghost suffix, BIASING (not filtering) toward protocol ``bias``.

        Feature 2 — sticky protocol context. Identical to :meth:`best_completion`
        except a leading rank element prefers keys that mention the active
        protocol (e.g. ``ospf``): among otherwise-equal candidates an OSPF
        command sorts first. This only re-orders index candidates — it never
        drops non-matching commands, so ``show version`` still completes while a
        session is "on OSPF". The device ?-help hot path (Tier 1) runs before
        this and is never touched.
        """
        if not bias:
            return self.best_completion(prefix)
        bias = bias.strip().lower()
        norm = self._norm(prefix)
        if not norm or not self._keys:
            return ""
        if prefix != prefix.rstrip():
            norm = norm + " "
        i = bisect.bisect_left(self._keys, norm)
        n = len(self._keys)
        best: tuple[int, int, int, int, str] | None = None
        while i < n and self._keys[i].startswith(norm):
            key = self._keys[i]
            if key != norm:
                tier, count = self._entries[key]
                bias_rank = 0 if bias in key else 1  # 0 sorts first
                cand = (bias_rank, tier, -count, len(key), key)
                if best is None or cand < best:
                    best = cand
            i += 1
        if best is None:
            return ""
        return _next_token_suffix(best[4][len(norm):])

    def commands_with_prefix(self, prefix: str, max_tier: int = 99, limit: int = 200) -> list[str]:
        """Return full index commands under ``prefix`` (Feature 3 KB source).

        Used by the command-discovery walk to harvest KB/RAG commands (pass
        ``max_tier=1`` to exclude the learned tier, which is gathered separately
        from the durable learned store). Not on the keystroke hot path.
        """
        norm = self._norm(prefix)
        if not norm:
            return []
        out: list[str] = []
        for key, entry in self._entries.items():
            if entry[0] > max_tier:
                continue
            if key == norm or key.startswith(norm + " "):
                out.append(key)
                if len(out) >= limit:
                    break
        return out

    def __len__(self) -> int:
        return len(self._entries)


def _build_ghost_index() -> None:
    """Build the ghost prefix index + open the learned cache (idempotent)."""
    global _ghost_index, _learned_store
    if _ghost_index is not None:
        return
    idx = _GhostIndex()
    # tier 0/1 — unified KB (doc_source marks PDF-extracted document commands).
    try:
        import sqlite3
        conn = sqlite3.connect(_KB_DB)
        for command, doc_source in conn.execute(
            "SELECT command, doc_source FROM commands"
        ):
            tier = 1 if str(doc_source or "").lower().endswith(".pdf") else 0
            idx.add(command, tier)
        conn.close()
    except Exception as exc:
        log.warning("Ghost index: KB load failed: %s", exc)
    # tier 0 — legacy canonical command set (abbreviation-friendly base).
    try:
        for cmd in _db.commands:
            idx.add(cmd.full_command, 0)
    except Exception as exc:
        log.warning("Ghost index: legacy command load failed: %s", exc)
    # tier 2 — local learned cache.
    try:
        if _learned_store is None:
            _learned_store = LearnedCommandStore(_LEARNED_DB)
        for command, count in _learned_store.all_commands():
            idx.add(command, 2, count)
    except Exception as exc:
        log.warning("Ghost index: learned cache load failed: %s", exc)
    idx.finalize()
    _ghost_index = idx
    log.info("Ghost prefix index built: %d entries", len(idx))


# ---------------------------------------------------------------------------
# Priority 3, Tier 1 — device '?'-help token cache (version-aware, authoritative)
#
# The LIVE device's own context help is authoritative for what it supports —
# the KB/PDF is a *different* IOS train (IOS XE 17.15 vs the live IOSv 15.9), so
# its syntax does not always match. When the engineer presses '?', we cache the
# continuation tokens for that command head and complete their next keystrokes'
# LAST token from those tokens BEFORE consulting the KB/PDF/learned index.
# Head matching is abbreviation-tolerant so 'sh ip bgp r' completes from a
# 'show ip bgp ?' capture.
# ---------------------------------------------------------------------------

_HELP_TOKEN_RE = re.compile(r"^[A-Za-z][\w-]*$")


@dataclass
class _HelpContext:
    head: str            # normalized (lower) command head the tokens continue
    tokens: list[str]    # valid continuation tokens captured from the device


# Bug F — keyed by (device_uuid, mode, full-command-path) so 'show vlan ?'
# options can never bleed into 'show platform hardware ?'. Bug O: mode is part of
# the key so config-mode help ('sh' -> shell) can never complete exec-mode 'sh'
# (-> show) or vice-versa. Key = "{device_id}\x00{mode}\x00{head}".
_help_cache: dict[str, _HelpContext] = {}


def _help_key(device_id: str, head: str, mode: str = "") -> str:
    return f"{device_id}\x00{(mode or '').strip().lower()}\x00{(head or '').strip().lower()}"


# ---------------------------------------------------------------------------
# Bug 8 — per-device VLAN capability cache (keyed by device UUID).
#
# The DEVICE is the source of truth: a verdict is recorded the FIRST time the
# device tells us (via 'show vlan ?' help-capture, an after-Enter '^' rejection,
# or a successful vlan command) whether it supports VLANs. We then reuse that
# cached verdict on every keystroke so the on-type "VLAN not supported" hint is
# instant and we never re-run 'show vlan ?' per keypress.
#
#   value True  -> device supports VLANs (don't hint)
#   value False -> device rejects VLANs  (surface the capability hint on-type)
#   absent      -> not yet known          (no hint until the device tells us)
# ---------------------------------------------------------------------------
_vlan_capability: dict[str, bool] = {}

# Bug 9 — how often the keepalive loop pokes each idle console. Must stay well
# under the CML console idle timeout (a few minutes) so a channel is poked long
# before it sleeps; 45s gives a comfortable margin without spamming the device.
_KEEPALIVE_INTERVAL_S = 45.0

# Session Health Monitor — bounded window for a single liveness poke / re-attach
# during a keepalive tick. A session that does not answer within this window is
# declared dead (keepalive_timeout) rather than hanging the tick indefinitely.
_KEEPALIVE_PROBE_TIMEOUT_S = 8.0
# Consecutive command-path empty-read strikes tolerated before the health probe
# classifies the session dead with reason ``empty_reads`` (see DeviceRuntime).
_EMPTY_READ_DEATH_LIMIT = 3

# In-session reconnect — server-side session persistence decoupled from the
# transient device/browser socket. When a browser WebSocket drops (network blip,
# tab sleep, proxy timeout) the session's server-side state (token, device meta,
# and the in-memory held console password for SSH) is RETAINED for a bounded
# grace window so the SAME session can be re-established under the SAME token
# with NO re-login and NO fresh terminal. A background reaper drops any session
# whose socket has been gone longer than the grace TTL, so credentials are never
# retained indefinitely. Offline/in-memory only — no external calls.
_SESSION_GRACE_TTL_S = 300.0
_SESSION_REAPER_INTERVAL_S = 60.0

# Per-byte hex tracing (added during the ^@ diagnostics phase) is OFF by
# default; set CLIQ_TRACE_BYTES=1 to re-enable the WS_SEND device_stream /
# device_output hex dumps at DEBUG. Logging only — no transport change.
_TRACE_BYTES = os.getenv("CLIQ_TRACE_BYTES", "").strip().lower() in ("1", "true", "yes", "on")

# 'show vlan...' / 'sh vl...' / bare 'vlan ...' — the vlan-family command head.
_VLAN_FAMILY_RE = re.compile(r"^(?:sh(?:o|ow)?\s+)?(vl(?:a(?:n)?)?)\b", re.IGNORECASE)


def _vlan_family_keyword(partial: str) -> str | None:
    """Return the canonical capability keyword ('vlan') when ``partial`` is a
    vlan-family command head, else None. Abbreviation-tolerant ('vl'/'vla')."""
    m = _VLAN_FAMILY_RE.match((partial or "").strip())
    if not m:
        return None
    from cliq.intelligence.did_you_mean import _expand_capability_keyword
    return _expand_capability_keyword(m.group(1))


def _vlan_help_verdict(page: str) -> bool | None:
    """Bug 11 — interpret a 'show vlan ?' help page into a VLAN-support verdict.

    The DEVICE is the source of truth, and different platforms format help
    differently (IOS columns vs NX-OS). The previous logic required IOS-style
    continuation tokens and so wrongly marked NX-OS — whose 'show vlan ?' returns
    a perfectly valid (differently-formatted) page — as UNSUPPORTED, producing a
    false "'vlan' isn't supported" hint on nxos9000.

    Be CONSERVATIVE: only ever record 'unsupported' when the device clearly
    REJECTS the keyword (a '%'-prefixed help error such as "% Invalid input").
    Any non-error page with real content means the device understands vlan, so
    record 'supported'. When the page is empty/ambiguous, return None (unknown)
    so we record nothing and never guess.

        True  -> supported (no hint)
        False -> unsupported (hint on iosv/router)
        None  -> unknown (don't record, don't hint)
    """
    # A genuine rejection may lead with a bare caret line (the '^' marker) BEFORE
    # the '%' error, so is_help_error() (which inspects only the first non-blank
    # line) can miss it. Treat a '%'-prefixed line ANYWHERE as a clear rejection.
    if is_help_error(page) or any(
        ln.lstrip().startswith("%") for ln in (page or "").splitlines()
    ):
        return False
    if _help_tokens(page):
        return True
    # No '%'-error but also no parsed IOS tokens — e.g. NX-OS formatting. A
    # non-trivial page with no rejection marker still proves the keyword is
    # understood; treat real content as supported, blank as unknown.
    return True if (page or "").strip() else None


def _record_vlan_capability(device_id: str, supported: bool, stage: str) -> None:
    """Cache (and log) a device's authoritative VLAN-support verdict.

    Keyed STRICTLY by device UUID (Bug 11) — there is no global/last-device
    fallback anywhere; each device's verdict is independent. Only logs when the
    verdict CHANGES so repeated confirmations stay quiet.
    """
    if _vlan_capability.get(device_id) is supported:
        return
    _vlan_capability[device_id] = supported
    obs.log_event(
        "validator", "vlan_capability_cache", stage,
        "supported" if supported else "unsupported",
        detail=device_id,
    )



def _help_tokens(help_text: str) -> list[str]:
    """Extract valid continuation tokens from a device '?'-help body.

    IOS lays help out as ``keyword<2+ spaces>description`` (sometimes several
    keyword columns). A column with no internal whitespace is a keyword; a column
    with whitespace is a prose description and is skipped. Artifacts such as
    ``<cr>`` and ``A.B.C.D`` are skipped.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for raw_line in (help_text or "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        for col in re.split(r"\s{2,}", line):
            tok = col.strip()
            if not tok or re.search(r"\s", tok):
                continue
            if not _HELP_TOKEN_RE.match(tok):
                continue
            low = tok.lower()
            if low not in seen:
                seen.add(low)
                tokens.append(tok)
    return tokens


def _cache_device_help(
    device_id: str, partial: str, help_text: str, *, extend: bool = False,
    mode: str = "", node_type: str = "",
) -> None:
    """Cache the device-help continuation tokens for later ghost completion.

    Priority 1: this is the ?-help -> ghost HANDOFF. Tokens parsed from EACH
    streamed page are cached immediately (``extend=True`` merges subsequent
    pages onto the same head) so the engineer's next keystrokes ghost-complete
    from the authoritative live-device help BEFORE the full list is captured.

    Part 1: device-verified tokens are ALSO persisted to the DB-backed
    cross-session cache (two-tier, PD-correct) so a captured ``?``-help survives
    refresh/restart and benefits other users on a same-type device.
    """
    tokens = _help_tokens(help_text)
    words = partial.split()
    if partial != partial.rstrip():        # trailing space -> subcommand list
        head = " ".join(words)
    else:                                   # mid-token -> tokens complete last word
        head = " ".join(words[:-1])
    head = head.lower()
    # Part 1 — persist device-confirmed options to the durable cache (best-effort).
    if tokens and node_type:
        try:
            cache = _get_cmd_cache()
            if cache is not None:
                cache.record_help_options(head, tokens, node_type, mode or "exec")
                obs.log_event(
                    "cache", "cache_store", "help", "ok",
                    device_type=None, platform=node_type, mode=mode or "exec",
                    source="device", detail=f"{head}|{len(tokens)}",
                )
        except Exception:  # pragma: no cover - cache must never block typing
            log.debug("help cache persist skipped", exc_info=True)
    key = _help_key(device_id, head, mode)
    existing = _help_cache.get(key)
    if extend and existing and existing.head == head:
        seen = {t.lower() for t in existing.tokens}
        merged = existing.tokens + [t for t in tokens if t.lower() not in seen]
        _help_cache[key] = _HelpContext(head=head, tokens=merged)
        obs.log_validation(
            "help_cache", "extend", "merged", device_uuid=device_id,
            detail=f"{head}|{len(merged)}",
        )
        log.info(
            "[GHOST] (handoff) extended device=%s head=%r +%d -> %d token(s)",
            device_id, head, len(merged) - len(existing.tokens), len(merged),
        )
        return
    if not tokens:
        return
    _help_cache[key] = _HelpContext(head=head, tokens=tokens)
    obs.log_validation(
        "help_cache", "store", "cached", device_uuid=device_id,
        detail=f"{head}|{len(tokens)}",
    )
    log.info(
        "[GHOST] (handoff) cached device=%s head=%r tokens=%d -> %s",
        device_id, head, len(tokens), tokens[:12],
    )


def _help_complete(device_id: str, partial: str, mode: str = "", node_type: str = "") -> str:
    """Tier 1 completion: complete the partial's LAST token from cached device
    '?'-help tokens, with an abbreviation-tolerant head match."""
    if not partial:
        return ""
    words = partial.split()
    if not words:
        return ""
    if partial != partial.rstrip():        # trailing space -> want the next word
        head_words = words
        last = ""
    else:
        head_words = words[:-1]
        last = words[-1].lower()
    # Bug F — look up the cache by THIS command's exact head; no head-verb-only
    # fallback, so a different command's tokens can never bleed in.
    head = " ".join(head_words)
    ctx = _help_cache.get(_help_key(device_id, head, mode))
    if not ctx and node_type:
        # Part 1 — SERVE-FIRST from the durable cross-session cache. The
        # in-memory cache is empty after a refresh/restart, but a previously
        # captured '?'-help for this device-TYPE (and platform, for PD options)
        # is persisted. Hydrate the in-memory cache from it so completion works
        # immediately and survives reconnects.
        try:
            cache = _get_cmd_cache()
            if cache is not None:
                opts = cache.get_help_options(head, node_type, mode or "exec")
                if opts:
                    ctx = _HelpContext(head=head.lower(), tokens=opts)
                    _help_cache[_help_key(device_id, head, mode)] = ctx
                    obs.log_event(
                        "cache", "cache_serve", "help", "hit",
                        platform=node_type, mode=mode or "exec",
                        source="device", detail=f"{head}|{len(opts)}",
                    )
        except Exception:  # pragma: no cover - cache must never block typing
            log.debug("help cache serve skipped", exc_info=True)
    if not ctx:
        return ""
    cached_head_words = ctx.head.split()
    if len(head_words) != len(cached_head_words):
        return ""
    # Abbreviation-tolerant: each typed head word is a prefix of the cached
    # canonical word OR vice-versa ('sh' <-> 'show').
    for tw, cw in zip(head_words, cached_head_words):
        tl, cl = tw.lower(), cw.lower()
        if not (cl.startswith(tl) or tl.startswith(cl)):
            return ""
    cands = [t for t in ctx.tokens
             if t.lower().startswith(last) and t.lower() != last]
    if not cands:
        return ""
    # Single best: shortest (most specific), tie-broken alphabetically.
    cands.sort(key=lambda t: (len(t), t.lower()))
    return cands[0][len(last):]


# Part 4 — node-types whose durable learned templates have already been hydrated
# into the in-memory ghost index this process, so the synchronous DB read-back
# fires at most once per device-type (the per-keystroke hot path stays in-memory).
_ghost_hydrated: set[str] = set()


def _hydrate_accepted_from_db(node_type: str, mode: str = "") -> bool:
    """Read accepted-command TEMPLATES back from the durable cache into the live
    ghost index (Part 4 read-back). Idempotent per node-type.

    After a hard refresh / restart the in-memory index is empty, but the shared
    cache still holds every device-confirmed template. Seeding the clean keyword
    prefixes here means accepted-command suggestions resolve instantly from the
    DB — no re-learning, no re-querying the device. Returns True when a (possibly
    repeated) hydration for this node-type has completed, so the caller can retry
    the in-memory lookup.
    """
    if not node_type:
        return False
    key = classify_device_type(node_type) + "\x00" + (mode or "").strip().lower()
    if key in _ghost_hydrated:
        return True
    try:
        cache = _get_cmd_cache()
        if cache is None or _ghost_index is None:
            return False
        cmds = cache.known_commands(node_type, None)
        seeded = 0
        for cmd in cmds:
            prefix = clean_prefix(cmd)
            if not prefix:
                continue
            _ghost_index.add(prefix, tier=2, count=1, live=True)
            seeded += 1
        _ghost_hydrated.add(key)
        if seeded:
            obs.log_event(
                "cache", "db_readback", "ghost", "hit",
                store="accepted_commands", platform=node_type,
                mode=mode, source="device", detail=str(seeded),
            )
        return True
    except Exception:  # pragma: no cover - read-back is best-effort
        log.debug("accepted-command db read-back skipped", exc_info=True)
        return False


def _ghost_complete(partial: str, device_id: str | None = None, mode: str = "", node_type: str = "", bias_protocol: str | None = None) -> str:
    """Tiered, version-aware ghost completion — the single suggestion resolver.

    Fallback chain (Part 1 — strict order, first hit wins, each miss falls
    through; ``device > cache > KB`` is preserved because the live device's own
    ``?``-help is consulted before the static corpus):

      Tier 1  device '?'-help tokens — authoritative for the LIVE device,
              overriding any KB/PDF train mismatch (live-device source of truth),
      Tier 2  KB / tree         — ingested command set (tree.lookup),
      Tier 3  RAG document       — PDF/semantic-extracted commands,
      Tier 4  learned templates  — device-accepted, value-stripped commands.

    Tiers 2-4 are served by the in-memory prefix index (ranked tier 0<1<2). Per
    Part 4 the serve order within the learned tier is **in-memory -> DB ->
    device**: on an in-memory miss the durable cache is read back (once per
    device-type) and the lookup retried, so a hard refresh never loses a learned
    suggestion. Returns the inline ghost SUFFIX for the single best match, or ''.
    """
    # Tier 1 — device '?'-help tokens (last-token completion, abbreviation-aware).
    if device_id:
        help_ghost = _help_complete(device_id, partial, mode, node_type)
        if help_ghost:
            log.debug(
                "[GHOST] device=%s partial=%r -> tier1(device-help) suffix=%r",
                device_id, partial, help_ghost,
            )
            return help_ghost
    # Tiers 2-4 — KB -> document(RAG) -> learned, via the prefix index.
    if _ghost_index is None:
        return ""
    # Bug 4b — inside a config mode, 'do <cmd>' runs an EXEC command. The KB and
    # learned index store the BARE exec command ('show ip interface brief'), not
    # 'do show ...', so strip the 'do ' prefix before matching. The returned
    # suffix simply continues the already-typed line, so nothing is re-prepended
    # for display.
    index_partial = partial
    if (mode or "").strip().lower().startswith("config"):
        after = partial.lstrip()
        if after[:3].lower() == "do " and after[3:].strip():
            index_partial = after[3:]
    suffix = _ghost_index.best_completion_biased(index_partial, bias_protocol)
    if suffix:
        log.debug(
            "[GHOST] device=%s partial=%r -> index(KB/RAG/learned) suffix=%r",
            device_id, partial, suffix,
        )
        return suffix
    # Part 4 — DB read-back. The in-memory index missed; after a hard refresh it
    # may simply not hold this device-type's learned templates yet. Hydrate them
    # from the durable cache once (in-memory -> DB -> device serve order) and
    # retry, so accepted-command suggestions survive refresh with no re-learning.
    if node_type and _hydrate_accepted_from_db(node_type, mode):
        suffix = _ghost_index.best_completion_biased(index_partial, bias_protocol)
        if suffix:
            log.debug(
                "[GHOST] device=%s partial=%r -> db-readback suffix=%r",
                device_id, partial, suffix,
            )
    return suffix


def _learn_accepted(command: str, mode: str, platform: str, output: str | None) -> None:
    """Record an accepted command in the learned cache + live ghost index.

    Accepted-only: skips anything whose device response carried an IOS error
    marker (handled by ``is_accepted``).
    """
    if _learned_store is None or not is_accepted(output):
        return
    try:
        rec = _learned_store.record_accepted(command, mode, platform)
    except Exception as exc:
        log.warning("Could not record learned command: %s", exc)
        return
    if rec and _ghost_index is not None:
        norm, count = rec
        _ghost_index.add(norm, tier=2, count=count, live=True)


def _refine_capability(runtime: "DeviceRuntime", command: str, output: str | None) -> None:
    """Authoritatively widen a device's capability from what it actually accepts.

    The device is the source of truth: if it accepts a switching command (no IOS
    error marker), it *is* switching-capable, regardless of any platform guess —
    and likewise for routing. This only ever ADDS capabilities (never removes),
    and is used purely for soft suggestion ranking.
    """
    if not is_accepted(output):
        return
    cap = command_capability(command)
    if cap in ("routing", "switching") and cap not in runtime.capability:
        runtime.capability = runtime.capability | {cap}


@app.on_event("startup")
async def _on_startup() -> None:
    # Build the ghost index off the event loop so startup stays responsive.
    await asyncio.to_thread(_build_ghost_index)
    # In-session reconnect — start the grace reaper that drops sessions whose
    # browser socket has been gone longer than the grace TTL (also wiping any
    # retained SSH console credentials). Held on the app so it is a singleton.
    if getattr(app.state, "_session_reaper_task", None) is None:
        app.state._session_reaper_task = asyncio.create_task(_session_grace_reaper())


async def _session_grace_reaper() -> None:
    """Drop sessions whose browser socket dropped and never came back.

    In-session reconnect keeps a disconnected session's server-side state alive
    for :data:`_SESSION_GRACE_TTL_S` so a reconnect resumes it with no re-login.
    This loop reaps any token still socket-less past that window: it removes the
    session, its device registry (wiping the in-memory SSH console password), any
    stale runtime mirrors, and every console lock it owned — so the credentials
    are never retained indefinitely and the console frees for a fresh attach.
    """
    while True:
        await asyncio.sleep(_SESSION_REAPER_INTERVAL_S)
        _reap_stale_sessions(time.monotonic())


def _reap_stale_sessions(now: float) -> None:
    """Reap every socket-less session idle past :data:`_SESSION_GRACE_TTL_S`.

    Split out from :func:`_session_grace_reaper` so the reap decision is testable
    without driving the sleep loop.
    """
    for tok, seen in list(_SESSION_LAST_SEEN.items()):
        if now - seen < _SESSION_GRACE_TTL_S:
            continue
        _SESSION_LAST_SEEN.pop(tok, None)
        sess = SESSIONS.pop(tok, None)
        DEVICES_BY_TOKEN.pop(tok, None)
        for key in [k for k in _DEVICE_RUNTIMES if k[0] == tok]:
            _DEVICE_RUNTIMES.pop(key, None)
        for path, owner in list(_CONSOLE_LOCKS.items()):
            if owner == tok:
                _CONSOLE_LOCKS.pop(path, None)
        if sess is not None:
            obs.log_event(
                "connection", "session_reaped", "grace_expired", "released",
                connection_type=getattr(sess, "connection_type", "cml"),
                grace_ttl_s=_SESSION_GRACE_TTL_S, detail=tok[:8],
            )



def _parse_mode(prompt: str) -> str:
    match = PROMPT_RE.match(prompt.strip())
    if not match:
        return "exec"
    submode = match.group(3)
    marker = match.group(4)
    if marker == ">":
        return "exec"
    if not submode:
        return "enable"
    known = {
        "config": "config",
        "config-if": "config-if",
        "config-router": "config-router",
        "config-line": "config-line",
        "config-subif": "config-subif",
        "config-vlan": "config-vlan",
        "config-ext-nacl": "config-ext-nacl",
    }
    return known.get(submode, "config")


def _normalize_ws_msg_type(value: Any) -> str:
    """Normalize websocket message types defensively.

    Handles casing, surrounding whitespace, and invisible/non-word chars so
    clients cannot accidentally miss a known route due to formatting drift.
    """
    raw = str(value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9_\-]", "", raw)
    aliases = {
        "askquestion": "ask",
        "ask_question": "ask",
        "qna": "ask",
        "runfix": "run_fix",
        "recallmemory": "recall_memory",
        "markresolved": "mark_resolved",
        "devicehelp": "device_help",
        "device-help": "device_help",
        "contexthelp": "device_help",
        "pagerkey": "pager_key",
        "pager-key": "pager_key",
        "morekey": "pager_key",
    }
    return aliases.get(cleaned, cleaned)


# Platforms that share the bulk of the classic IOS CLI syntax. A query issued
# from any one of these can usefully retrieve commands tagged with another,
# because the command surface is (for the purposes of discovery/Q&A) shared.
_IOS_PLATFORM_FAMILY = frozenset({"ios", "iosv", "iosvl2", "iosxe"})


def _platform_is_ios_family(platform: Optional[str]) -> bool:
    return bool(platform) and platform.strip().lower() in _IOS_PLATFORM_FAMILY


async def _qa_respond(question: str, device_platform: Optional[str], device_mode: Optional[str]):
    """Run advisory Q&A with platform/mode treated as SOFT hints only.

    Hard SQL platform/mode filters can zero out all results when the device's
    platform tag (e.g. ``iosv``) differs from the KB tag (e.g. ``iosxe``), even
    though the two share the same CLI. For Q&A/discovery we therefore:

    1. Never hard-filter by ``cmd_mode`` (always a soft hint).
    2. Drop the platform filter when the device is in a shared CLI family, so
       ``iosv``/``iosvl2`` queries still retrieve ``iosxe``-tagged IOS commands.
    3. As a safety net, if a platform filter still zeroes the result, retry
       once without any platform filter and annotate the response.

    Returns ``(response, annotated)`` where ``annotated`` is True when results
    came from a different-platform reference and should carry a verify note.

    NOTE: This soft-hint behavior is for Q&A ONLY. Safety validation
    (``/api/intel/validate``) keeps strict hard platform checks so wrong-device
    commands are still blocked.
    """
    from cliq.retrieval.schema import RetrievalQuery

    # IOS-family devices: treat platform as a soft hint (no hard filter) so the
    # shared command surface is reachable. Non-family platforms keep the hint
    # initially and rely on the retry safety net below.
    soft_platform = None if _platform_is_ios_family(device_platform) else (device_platform or None)
    annotated = bool(device_platform) and soft_platform is None

    q = RetrievalQuery(
        input_text=question,
        mode="auto",
        platform=soft_platform,
        cmd_mode=None,  # mode is always a soft hint for Q&A — never hard-filter
        top_k=5,
        min_confidence=0.3,
    )
    resp = await asyncio.to_thread(_assistant.respond, q)

    zeroed = resp.response_tier == "fallback" or (not resp.results and not resp.grouped_results)
    if zeroed and soft_platform is not None:
        # Safety net: a platform filter eliminated everything — retry unfiltered.
        log.info(
            "Q&A platform filter (platform=%s) zeroed results for %r; retrying without filter",
            soft_platform, question,
        )
        q_retry = RetrievalQuery(
            input_text=question, mode="auto",
            platform=None, cmd_mode=None,
            top_k=5, min_confidence=0.3,
        )
        resp = await asyncio.to_thread(_assistant.respond, q_retry)
        annotated = True

    return resp, annotated


# ---------------------------------------------------------------------------
# Bug P (Ask path) — DEVICE > CACHE > KB reconciliation for the Q&A advisory.
#
# The terminal/ghost path and the Explain card already defer to the live device
# (see ``_explain_command``). The natural-language "Ask CLIQ" path, however, used
# to return pure KB/RAG answers with no awareness of what the connected device
# just rejected or which capability class it is. These helpers layer the SAME
# reconciliation pattern onto the Ask results: suppress device-rejected commands,
# gate by capability, and demote platform-mismatched rows to enrichment-only.
#
# All of this is BEST-EFFORT: when no device is connected (``runtime is None``)
# every result passes through unchanged, preserving the KB-only fallback (plus
# the existing "verify on your platform" annotation).
# ---------------------------------------------------------------------------

def _serialize_qa_result(r: Any, reconcile: Optional[str] = None,
                         source_override: Optional[str] = None) -> dict[str, Any]:
    """Serialize a retrieval result to the wire shape, with optional reconcile tag."""
    out = {
        "command": r.command,
        "description": r.description,
        "platform": r.platform,
        "mode": r.mode,
        "confidence": r.confidence_score,
        "source": source_override if source_override is not None else r.source_attribution,
        "match_type": r.match_type,
        "is_candidate": getattr(r, "is_candidate", False),
        "syntax_variants": getattr(r, "syntax_variants", None) or [],
    }
    if reconcile:
        out["reconcile"] = reconcile
    return out


def _reconcile_qa_result(
    r: Any, runtime: Optional["DeviceRuntime"], device_platform: Optional[str],
) -> tuple[str, Optional[dict[str, Any]]]:
    """Reconcile ONE KB/RAG result against the live device (device > KB).

    Returns ``(outcome, serialized)`` where ``outcome`` is one of
    ``"suppressed"`` / ``"enrichment_only"`` / ``"passed"`` and ``serialized`` is
    None when the result is suppressed. Mirrors ``_explain_command``:

    1. **Rejected** — the device SYNTAX-rejected this exact command → suppress it
       (never present a device-rejected command as a valid answer or fix).
    2. **Capability gate** — a routing/switching command whose class the device
       does not have (e.g. a ``vlan`` command on a routing-only ``iosv``) →
       suppress, so switching commands don't surface on a router and vice versa.
    3. **Platform mismatch** — the KB row's source platform classifies to a
       different device-type than the live device → demote to enrichment-only
       (keep the explanation, flag it, drop the "run this" framing).
    """
    if runtime is None:
        return "passed", _serialize_qa_result(r)

    cmd = (r.command or "").strip()
    norm = cmd.lower()

    # 1. Device explicitly rejected this exact command — device is authoritative.
    rejected = {c.strip().lower() for c in getattr(runtime, "rejected", set())}
    if norm and norm in rejected:
        return "suppressed", None

    # 2. Capability gate (soft class, device-confirmed/seeded). Only suppress a
    #    command with a SPECIFIC class the device lacks; capability-neutral
    #    ("both") commands always pass.
    caps = getattr(runtime, "capability", None) or frozenset()
    cap = command_capability(cmd)
    if cap in ("routing", "switching") and caps and cap not in caps:
        return "suppressed", None

    # 3. Platform device-type mismatch — demote to enrichment-only.
    kb_platform = (getattr(r, "platform", "") or "")
    if kb_platform and device_platform:
        from cliq.memory.command_cache import classify_device_type
        if classify_device_type(kb_platform) != classify_device_type(device_platform):
            note = f"(KB is for {kb_platform}; enrichment only — verify on this device)"
            base = r.source_attribution or ""
            source = f"{base} {note}".strip()
            return "enrichment_only", _serialize_qa_result(
                r, reconcile="enrichment_only", source_override=source,
            )

    return "passed", _serialize_qa_result(r)


def _apply_qa_reconciliation(
    resp: Any, runtime: Optional["DeviceRuntime"],
    device_platform: Optional[str], question: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reconcile a whole Q&A response against the device; log a summary event.

    Returns serialized ``(results, groups)`` with device-rejected / wrong-capability
    rows removed and platform-mismatched rows flagged. Emits a single
    ``kb_device_reconcile`` observability event mirroring ``_explain_command``
    with ``outcome`` ``device_wins`` / ``enrichment_only`` / ``passed``.
    """
    stats = {"suppressed": 0, "enrichment_only": 0, "passed": 0}

    def _proc(items: Any) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for r in items:
            outcome, serialized = _reconcile_qa_result(r, runtime, device_platform)
            stats[outcome] += 1
            if serialized is not None:
                kept.append(serialized)
        return kept

    results = _proc(resp.results)
    groups: list[dict[str, Any]] = []
    for g in resp.grouped_results:
        g_results = _proc(g.results)
        if g_results:  # drop a group emptied by reconciliation
            groups.append({"label": g.label, "results": g_results})

    if runtime is not None:
        outcome = (
            "device_wins" if stats["suppressed"]
            else "enrichment_only" if stats["enrichment_only"]
            else "passed"
        )
        obs.log_event(
            "advisory", "kb_device_reconcile", "qa_ask", outcome,
            kb_vs_device="device",
            command_path=(question or "")[:80],
            detail=(
                f"dev={device_platform or '?'} suppressed={stats['suppressed']} "
                f"enrichment={stats['enrichment_only']} passed={stats['passed']}"
            ),
        )
    return results, groups


def _filter_sequence_for_device(
    steps: list[dict[str, Any]], runtime: Optional["DeviceRuntime"],
) -> list[dict[str, Any]]:
    """Drop device-rejected / wrong-capability steps from a KB-assembled sequence.

    Applied ONLY to the UNVERIFIED KB-assembled fallback sequence — curated,
    human-verified recipes are never gated. Best-effort: no device → unchanged.
    """
    if runtime is None:
        return steps
    rejected = {c.strip().lower() for c in getattr(runtime, "rejected", set())}
    caps = getattr(runtime, "capability", None) or frozenset()
    kept: list[dict[str, Any]] = []
    for step in steps:
        cmd = (step.get("command") or "").strip()
        if cmd.lower() in rejected:
            continue
        cap = command_capability(cmd)
        if cap in ("routing", "switching") and caps and cap not in caps:
            continue
        kept.append(step)
    return kept


# Setup / how-to phrasing that should yield a reviewable config + verify sequence.
_SETUP_PATTERNS = (
    "set up", "setup", "set-up", "configure", "config ", "enable ",
    "how do i set", "how to set", "how do i configure", "how to configure",
    "help me set", "help me configure", "bring up", "turn on", "create a",
)


def _is_setup_query(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in _SETUP_PATTERNS)


def _result_kind(mode: Optional[str], command: Optional[str]) -> str:
    """Classify a result as a 'config' step or a 'verify' (show/exec) step."""
    m = (mode or "").lower()
    c = (command or "").lower().strip()
    if m.startswith("config"):
        return "config"
    if c.startswith(("show ", "ping", "traceroute", "debug ")) or c == "show":
        return "verify"
    if m in {"exec", "enable", "user"}:
        return "verify"
    return "config"


def _serialize_result(r: Any) -> dict[str, Any]:
    return {
        "command": r.command,
        "description": r.description,
        "platform": r.platform,
        "mode": r.mode,
        "confidence": r.confidence_score,
        "source": r.source_attribution,
        "match_type": r.match_type,
        "syntax_variants": getattr(r, "syntax_variants", None) or [],
    }


def _build_sequence(resp: Any) -> list[dict[str, Any]]:
    """Build an ordered, reviewable config+verify sequence from a response.

    Config steps come first (the setup), verify/show steps last. Each entry is
    insert-able by the engineer — advisory only, never auto-run.
    """
    flat = list(resp.results) + [r for g in resp.grouped_results for r in g.results]
    seen: set[str] = set()
    config_steps: list[dict[str, Any]] = []
    verify_steps: list[dict[str, Any]] = []
    for r in flat:
        key = (r.command or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        entry = _serialize_result(r)
        entry["kind"] = _result_kind(r.mode, r.command)
        (config_steps if entry["kind"] == "config" else verify_steps).append(entry)
    return config_steps + verify_steps


# ---------------------------------------------------------------------------
# Phase 9 (Bug 3) — curated procedural recipes.
#
# Known setup procedures (e.g. "configure a static route") return HUMAN-VERIFIED
# templates from command_db/recipes.yaml instead of KB keyword assembly, which
# produced noise like `ipv6 route static bfd`. KB assembly is used ONLY as a
# fallback when no curated recipe matches, and is then labelled UNVERIFIED.
# ---------------------------------------------------------------------------
_RECIPES_PATH = os.environ.get("CLIQ_RECIPES", "./command_db/recipes.yaml")
_recipes_cache: Optional[list[dict[str, Any]]] = None


def _load_recipes() -> list[dict[str, Any]]:
    """Load curated recipes from YAML once (cached)."""
    global _recipes_cache
    if _recipes_cache is not None:
        return _recipes_cache
    recipes: list[dict[str, Any]] = []
    try:
        import yaml
        path = Path(_RECIPES_PATH)
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            recipes = list(data.get("recipes", []) or [])
            log.info("Loaded %d curated recipe(s) from %s", len(recipes), path)
        else:
            log.warning("Recipes file not found: %s — using KB fallback only.", path)
    except Exception as exc:
        log.error("Failed to load recipes (%s): %s", _RECIPES_PATH, exc)
    _recipes_cache = recipes
    return recipes


def _recipe_step_entry(step: dict[str, Any], platform: Optional[str]) -> dict[str, Any]:
    """Normalize a curated recipe step into the wire shape used by the UI."""
    return {
        "command": str(step.get("command", "")).strip(),
        "description": str(step.get("description", "")).strip(),
        "platform": str(step.get("platform", platform or "ios")),
        "mode": str(step.get("mode", "")),
        "confidence": 1.0,
        "source": "curated recipe (verified)",
        "match_type": "curated",
        "kind": str(step.get("kind", "config")),
    }


def _resolve_recipe(question: str, platform: Optional[str]) -> Optional[list[dict[str, Any]]]:
    """Return curated steps for a known procedure, or None if none matches.

    A recipe matches when EVERY keyword in its `match.keywords` appears in the
    question, OR any phrase in `match.phrases` is a substring of the question.
    The most specific matching recipe (most keywords) wins.
    """
    q = re.sub(r"\s+", " ", (question or "").strip().lower())
    if not q:
        return None
    q_tokens = set(re.findall(r"[a-z0-9]+", q))
    best: Optional[dict[str, Any]] = None
    best_score = -1
    for recipe in _load_recipes():
        match = recipe.get("match", {}) or {}
        keywords = [str(k).lower() for k in (match.get("keywords") or [])]
        phrases = [str(p).lower() for p in (match.get("phrases") or [])]
        kw_ok = bool(keywords) and all(k in q_tokens for k in keywords)
        phrase_ok = any(p in q for p in phrases)
        if not (kw_ok or phrase_ok):
            continue
        # Prefer the recipe matching the most keywords (most specific).
        score = len(keywords) if kw_ok else 0
        score += len([p for p in phrases if p in q])
        if score > best_score:
            best, best_score = recipe, score
    if best is None:
        return None
    steps = [_recipe_step_entry(s, platform) for s in (best.get("steps") or [])]
    steps += [_recipe_step_entry(s, platform) for s in (best.get("verify") or [])]
    return [s for s in steps if s["command"]]


def _explain_command(
    command: str, platform: Optional[str], mode: Optional[str],
    *, device_rejected: bool = False,
) -> dict[str, Any]:
    """Produce an advisory, plain-language explanation of a command.

    Combines the KB description (via the assistant's exact lookup) with the
    safety validator's verdict so the engineer gets both 'what it does' and
    'is it safe / right here'. Advisory only — never executes.
    """
    cmd = (command or "").strip()
    # Bug P — DEVICE OVER KB. If the live device just SYNTAX-rejected this exact
    # command, the device is authoritative: the advisory must NOT claim it's
    # "valid for platform X" nor offer it as a re-run fix, even if a (possibly
    # mis-platformed) Cat9600 PDF KB entry exists. Defer to the device.
    if device_rejected:
        obs.log_event(
            "advisory", "kb_device_reconcile", "device_rejected", "device_wins",
            kb_vs_device="device", command_path=cmd, detail=platform or "",
        )
        plat = platform or "this device"
        return {
            "command": cmd,
            "description": "",
            "source": "device",
            "verdict": "wrong_device",
            "severity": "warning",
            "reason": f"This command isn't available on this device ({plat}). "
            "The device rejected it — not re-running.",
            "suggestion": None,
        }
    _init_advisory_stack()
    description = ""
    source = ""
    suggestion = None
    if _assistant is not None and cmd:
        try:
            from cliq.retrieval.schema import RetrievalQuery
            q = RetrievalQuery(
                input_text=cmd, mode="auto", platform=None, cmd_mode=None,
                top_k=3, min_confidence=0.3,
            )
            resp = _assistant.respond(q)
            top = resp.results[0] if resp.results else (
                resp.grouped_results[0].results[0]
                if resp.grouped_results and resp.grouped_results[0].results else None
            )
            if top:
                description = top.description or ""
                source = top.source_attribution or ""
                if (top.command or "").strip().lower() != cmd.lower():
                    suggestion = top.command
                # Bug P — PLATFORM-FILTER the KB. A Cat9600-PDF entry must NOT be
                # offered as a valid fix for an iosv/router unless the device has
                # confirmed it. If the KB entry's source platform classifies to a
                # different device-type than THIS device, demote it to
                # enrichment: keep the description, drop the re-run suggestion.
                kb_platform = getattr(top, "platform", "") or ""
                if suggestion and platform and kb_platform:
                    from cliq.memory.command_cache import classify_device_type
                    if classify_device_type(kb_platform) != classify_device_type(platform):
                        obs.log_event(
                            "advisory", "kb_device_reconcile", "platform_mismatch",
                            "device_wins", kb_vs_device="device",
                            command_path=cmd,
                            detail=f"kb={kb_platform} dev={platform}",
                        )
                        suggestion = None
                        source = f"{source} (KB is for {kb_platform}; enrichment only)".strip()
        except Exception as exc:
            log.warning("explain: assistant lookup failed: %s", exc)

    verdict = "ok"
    severity = "info"
    reason = ""
    if _validator is not None and cmd:
        try:
            from cliq.validation.schema import ValidationContext
            ctx = ValidationContext(
                command=cmd,
                device_platform=platform or "iosxe",
                current_mode=mode or "exec",
            )
            v = _validator.validate(ctx)
            verdict = v.verdict_type.value
            severity = v.severity.value
            reason = v.reason or ""
            if v.suggested_action and not suggestion:
                suggestion = v.normalized_command or None
        except Exception as exc:
            log.warning("explain: validator failed: %s", exc)

    return {
        "command": cmd,
        "description": description,
        "source": source,
        "verdict": verdict,
        "severity": severity,
        "reason": reason,
        "suggestion": suggestion,
    }


# Bug 6 — EXEC-only verbs that are never valid as the FIRST token of a
# config-mode line (they must run from EXEC, or be prefixed with 'do' inside
# config). This is the small, stable set of IOS EXEC actions that always require
# 'do' in config — mode-driven, NOT a per-command capability table.
_EXEC_ONLY_VERBS: frozenset[str] = frozenset({
    "show", "ping", "traceroute", "write", "copy", "reload", "clear",
    "debug", "undebug", "telnet", "ssh", "more", "dir", "terminal",
})

# Stable message constant so the suggest handler can recognise the do-hint for
# observability without string-sniffing the rendered text twice.
_CONFIG_DO_HINT_MSG = "In config mode \u2014 use 'do <cmd>' to run EXEC commands"

# Bug 3 — 'do' is ONLY valid inside config sub-modes. In exec/enable mode a
# 'do <cmd>' line is invalid: the command runs directly. This corrective hint
# tells the engineer to drop 'do' (and we also suppress do-completions there).
_EXEC_DO_HINT_MSG = (
    "'do' is only valid in config modes. In exec mode, run the command directly."
)


def _is_exec_mode_do(mode: str, partial: str) -> bool:
    """True when a ``do ...`` line is typed OUTSIDE a config sub-mode.

    ``do`` only prefixes EXEC commands from within config modes; in exec/enable
    (prompt ``hostname#`` / ``hostname>``) it is invalid, so we must neither
    ghost-complete it nor offer ``do`` continuations (Bug 3).
    """
    if (mode or "").strip().lower().startswith("config"):
        return False
    p = (partial or "").lstrip().lower()
    return p == "do" or p.startswith("do ")


def _match_exec_only_verb(token: str) -> str | None:
    """Resolve a typed (possibly abbreviated) verb to an EXEC-only verb.

    Exact match wins; otherwise an UNAMBIGUOUS abbreviation (a prefix that
    matches exactly one EXEC-only verb) resolves, so 'sho'/'pin' still trigger
    the hint. Ambiguous prefixes (e.g. 'c' -> copy/clear) resolve to None.
    """
    t = (token or "").lower()
    if not t:
        return None
    if t in _EXEC_ONLY_VERBS:
        return t
    matches = [v for v in _EXEC_ONLY_VERBS if v.startswith(t)]
    return matches[0] if len(matches) == 1 else None


def _toolkit_mode_command(command: str, mode: str) -> str:
    """Render a toolkit command for the current CLI mode (reuses do-gating).

    In any ``config`` submode an EXEC-only verb (``show`` / ``debug`` / …) must
    be prefixed with ``do`` to run — the SAME rule the on-type ``do`` hint uses
    (:func:`_match_exec_only_verb`). In exec/enable the command is left as-is.
    This is the ONLY mode logic the toolkit needs, and it delegates entirely to
    the existing EXEC-only verb table so nothing is duplicated.
    """
    cmd = (command or "").strip()
    if not cmd:
        return cmd
    if not (mode or "").strip().lower().startswith("config"):
        return cmd
    head = cmd.split()[0]
    if _match_exec_only_verb(head) is not None:
        return f"do {cmd}"
    return cmd


def _build_toolkit_advisory(
    question: str, runtime: Optional["DeviceRuntime"], mode: Optional[str],
) -> Optional[dict[str, Any]]:
    """Detect troubleshooting intent and build a gated toolkit payload, or None.

    Slots the Recipe Toolkit at the BOTTOM of the proven hierarchy (Live Device >
    Persistent Cache > KB Enrichment > Recipe Toolkit): it defers to the live
    device by (a) suppressing the toolkit when the device capability lacks the
    required class (e.g. OSPF on an L2-only switch) and (b) dropping any command
    the device just SYNTAX-rejected (``runtime.rejected``). Mode-aware rendering
    reuses the existing ``do``-gating. Synchronous + in-memory — no embedding
    call on the hot path. Returns None when there is no troubleshooting intent or
    the toolkit is suppressed.
    """
    protocol = detect_toolkit_intent(question)
    if not protocol:
        return None
    capability = getattr(runtime, "capability", None) or frozenset()
    rejected = frozenset(getattr(runtime, "rejected", set()) or set())
    effective_mode = (mode or (runtime.mode if runtime else None) or "exec")
    payload = build_toolkit_payload(
        protocol,
        mode=effective_mode,
        capability=capability,
        rejected=rejected,
        mode_adapter=_toolkit_mode_command,
    )
    if payload is None:
        obs.log_event(
            "advisory", "toolkit", protocol, "suppressed",
            detail=f"cap={sorted(capability) if capability else '?'} mode={effective_mode}",
        )
        return None
    obs.log_event(
        "advisory", "toolkit", protocol, "surfaced",
        detail=f"phases={len(payload['phases'])} mode={effective_mode}",
    )
    return payload


# Feature 2 — sticky protocol context tuning knobs.
_CONTEXT_IDLE_SECONDS = 900          # clear if untouched for 15 minutes
_CONTEXT_UNRELATED_LIMIT = 5         # clear after N consecutive unrelated commands


def _context_protocol_matches(protocol: str, command: str) -> bool:
    """True when ``command`` is plausibly part of ``protocol``'s command family.

    Uses the toolkit's own aliases + command_prefixes (data-driven, never a
    hardcoded protocol list) so a command counts as "related" if it starts with
    a known prefix or mentions a protocol alias. Best-effort and cheap — used
    only to decide whether an unrelated-command clears the sticky context.
    """
    cmd = (command or "").strip().lower()
    if not cmd:
        return True  # blank line never counts against the context
    tk = get_toolkit(protocol)
    if tk is None:
        return protocol.lower() in cmd
    for prefix in tk.intent.command_prefixes:
        if cmd.startswith(prefix):
            return True
    for alias in tk.intent.aliases(protocol):
        if alias in cmd:
            return True
    return False


def _protocol_for_command(command: str) -> Optional[str]:
    """Return the toolkit protocol a run command belongs to, if any.

    Feature 2 (CLI source) — data-driven: a command whose head matches a
    toolkit's ``command_prefixes`` (e.g. ``show ip ospf neighbor`` → ``ospf``)
    sets the sticky context from actual device activity. Iterates the loaded
    toolkits so adding EIGRP/BGP needs no code change here.
    """
    cmd = (command or "").strip().lower()
    if not cmd:
        return None
    try:
        toolkits = load_toolkits()
    except Exception:  # pragma: no cover - defensive
        return None
    for protocol, tk in toolkits.items():
        for prefix in tk.intent.command_prefixes:
            if prefix and cmd.startswith(prefix):
                return protocol
    return None


def _set_active_context(
    runtime: Optional["DeviceRuntime"], protocol: str, source: str,
) -> Optional[dict]:
    """Set/refresh the session's sticky protocol context (server-authoritative)."""
    if runtime is None or not protocol:
        return None
    runtime.active_context = {
        "protocol": protocol.strip().lower(),
        "source": source,
        "ts": time.time(),
        "unrelated": 0,
    }
    obs.log_event("advisory", "context", protocol, f"set:{source}")
    return runtime.active_context


def _clear_active_context(runtime: Optional["DeviceRuntime"], reason: str = "explicit") -> None:
    """Clear the sticky protocol context (explicit action / reset / timeout)."""
    if runtime is None:
        return
    if runtime.active_context is not None:
        obs.log_event(
            "advisory", "context", runtime.active_context.get("protocol", "?"),
            f"clear:{reason}",
        )
    runtime.active_context = None


def _active_context_protocol(runtime: Optional["DeviceRuntime"]) -> Optional[str]:
    """Return the live sticky protocol, honoring the idle timeout (auto-clears)."""
    ctx = getattr(runtime, "active_context", None) if runtime else None
    if not ctx:
        return None
    if time.time() - float(ctx.get("ts", 0)) > _CONTEXT_IDLE_SECONDS:
        _clear_active_context(runtime, reason="idle")
        return None
    return ctx.get("protocol") or None


def _touch_active_context_on_command(
    runtime: Optional["DeviceRuntime"], command: str,
) -> None:
    """Age the sticky context against an executed command.

    A protocol-related command refreshes the context (resets the unrelated
    counter + timestamp); an unrelated command increments the counter and clears
    the context once it crosses :data:`_CONTEXT_UNRELATED_LIMIT`.
    """
    ctx = getattr(runtime, "active_context", None) if runtime else None
    if not ctx:
        return
    protocol = ctx.get("protocol") or ""
    if _context_protocol_matches(protocol, command):
        ctx["unrelated"] = 0
        ctx["ts"] = time.time()
        return
    ctx["unrelated"] = int(ctx.get("unrelated", 0)) + 1
    if ctx["unrelated"] >= _CONTEXT_UNRELATED_LIMIT:
        _clear_active_context(runtime, reason="unrelated")


# Feature 3 — command discovery / reference.
# Per-session, per-PID cache of the live device ?-walk so a repeated "show me
# OSPF commands" never re-walks the device (the walk is async + off the
# keystroke hot path either way). Keyed by device_id + protocol + PID.
_DISCOVERY_WALK_CACHE: dict[str, list[str]] = {}


def _discovery_cache_key(device_id: Optional[str], protocol: str) -> str:
    return f"{device_id or 'offline'}::{protocol}::{os.getpid()}"


async def _walk_device_commands(runtime: "DeviceRuntime", walk_paths) -> list[str]:
    """Async ?-walk over ``walk_paths`` on the live device (source of truth).

    For each base path (``show ip ospf`` …) captures the device's own ``?``-help
    page and expands one level of next-tokens into full commands. Serialized
    under the device lock so it never interleaves with a running command; runs
    entirely in threads so the event loop is never blocked. Best-effort: any
    path that errors is skipped.
    """
    device = getattr(runtime, "device", None)
    if device is None or not walk_paths:
        return []
    # Session Health Monitor — never ?-walk a dead session; fall back to Learned+KB.
    if getattr(runtime, "dead", False):
        return []
    out: list[str] = []
    seen: set[str] = set()
    async with runtime.lock:
        for base in walk_paths:
            try:
                page = await asyncio.to_thread(_device_help_text, device, base)
            except Exception:  # pragma: no cover - walk is best-effort
                continue
            options, _has_cr = parse_help_options(page)
            for opt in options:
                low = opt.lower()
                if low.startswith("<") or low in ("|", "<cr>", "cr"):
                    continue
                cmd = f"{base} {opt}".strip()
                key = cmd.lower()
                if key not in seen:
                    seen.add(key)
                    out.append(cmd)
    return out


async def _build_discovery_reference(
    question: str,
    runtime: Optional["DeviceRuntime"],
    mode: Optional[str],
    device_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Detect discovery intent and merge Device/Learned/KB into a reference, or None.

    Feature 3 — compiles a live-validated command list from three attributed
    sources (Device ?-walk > Learned templates > KB), deduped by authority,
    scope-filtered, capability-gated, rejected-suppressed and mode-adapted.
    Async (device ?-walk + KB scan) — never on the keystroke hot path. Falls
    back gracefully to Learned + KB (marked unverified) when no device is live.
    """
    detected = detect_discovery_intent(question)
    if not detected:
        return None
    protocol, scope = detected
    tk = get_toolkit(protocol)
    if tk is None:
        return None
    # Session Health Monitor — a dead session is treated as no-device so the
    # reference degrades gracefully to Learned + KB, every command flagged
    # "unverified on this platform" (consistent with the Toolkit v2 offline path).
    device_available = (
        runtime is not None
        and getattr(runtime, "device", None) is not None
        and not getattr(runtime, "dead", False)
    )

    # Source 1 — live device ?-walk (highest authority), cached per session/PID.
    device_cmds: list[str] = []
    if device_available:
        ckey = _discovery_cache_key(device_id, protocol)
        cached = _DISCOVERY_WALK_CACHE.get(ckey)
        if cached is None:
            cached = await _walk_device_commands(runtime, tk.walk_paths())
            _DISCOVERY_WALK_CACHE[ckey] = cached
        device_cmds = cached

    # Source 2 — learned, device-accepted templates.
    learned_cmds: list[str] = []
    if _learned_store is not None:
        try:
            for pref in tk.learned_prefixes():
                for cmd, _cnt in _learned_store.prefix_matches(pref, limit=50):
                    learned_cmds.append(cmd)
        except Exception:  # pragma: no cover - learned lookup best-effort
            log.debug("discovery learned lookup skipped", exc_info=True)

    # Source 3 — KB / RAG corpus (from the in-memory index, learned tier excluded).
    kb_cmds: list[str] = []
    if _ghost_index is not None:
        for pref in tk.walk_paths():
            kb_cmds.extend(_ghost_index.commands_with_prefix(pref, max_tier=1))

    capability = getattr(runtime, "capability", None) or frozenset()
    rejected = frozenset(getattr(runtime, "rejected", set()) or set())
    effective_mode = (mode or (runtime.mode if runtime else None) or "exec")
    payload = merge_discovery_sources(
        protocol,
        device=device_cmds,
        learned=learned_cmds,
        kb=kb_cmds,
        scope=scope,
        mode=effective_mode,
        capability=capability,
        required_capability=tk.required_capability,
        rejected=rejected,
        mode_adapter=_toolkit_mode_command,
        device_available=device_available,
    )
    if payload is None:
        obs.log_event(
            "advisory", "discovery", protocol, "suppressed",
            detail=f"scope={scope} cap={sorted(capability) if capability else '?'}",
        )
        return None
    obs.log_event(
        "advisory", "discovery", protocol, "surfaced",
        detail=f"scope={scope} device={len(device_cmds)} learned={len(learned_cmds)} kb={len(kb_cmds)}",
    )
    return payload


def _config_mode_do_hint(analysis) -> dict[str, Any] | None:
    """Bug 6 — proactive 'do' advisory for EXEC-only verbs typed in config mode.

    Fires the instant the engineer types an EXEC-only verb (show/ping/...) while
    in any ``config`` submode, BEFORE Enter — so they see the ``do`` guidance
    without first eating a ``% Invalid input`` rejection. Mode-driven and
    advisory only: ``fix`` carries the ``do``-prefixed line for a one-key insert,
    nothing is auto-run. Returns None outside config mode or for non-EXEC verbs.
    """
    mode = (getattr(analysis, "mode", "") or "")
    if not mode.startswith("config"):
        return None
    raw = (getattr(analysis, "raw_input", "") or "").strip()
    if not raw:
        return None
    if _match_exec_only_verb(raw.split()[0]) is None:
        return None
    return {
        "kind": "wrong_mode",
        "message": _CONFIG_DO_HINT_MSG,
        "fix": f"do {raw}",
    }


def _first_alternative(message: str) -> str | None:
    """Extract the first capability-appropriate alternative from a hint message.

    Capability advisories read ``'vlan' isn't supported on this device. Try:
    show ip interface brief · show interfaces``. Pull the first alternative
    (split on the middle-dot separator) so the Advisory card can offer it as a
    copyable fix command. Returns None when no ``Try:`` clause is present.
    """
    if not message or "Try:" not in message:
        return None
    tail = message.split("Try:", 1)[1].strip()
    first = tail.split("\u00b7", 1)[0].strip()
    return first or None


def _build_live_hint(analysis) -> dict[str, Any] | None:
    """Build a compact, single-message live hint for as-you-type display.

    Returns None when the input is fine (status ok/partial) so the overlay
    stays clear and only appears the moment a mistake is detected.
    """
    # Bug 3 — 'do' in exec/enable mode. The engine flags this as a 'typo', but
    # the typo path stays SILENT before Enter (Bug K gate below), so we handle
    # it explicitly here: emit a corrective hint and offer the bare command as
    # the one-key fix. Fires ON-TYPE the instant 'do ' is seen outside config.
    mode = getattr(analysis, "mode", "") or ""
    raw = (getattr(analysis, "raw_input", "") or "").lstrip()
    # Tier 1 (safety first) — destructive-action confirmation. BEFORE the
    # engineer commits a deletion / negation / destructive command ('no ...',
    # 'shutdown', 'clear', 'erase', 'reload', 'write erase', 'delete', ...),
    # surface a RED y/n confirmation naming the exact object being changed. This
    # is the single highest-priority advisory, so it pre-empts every hint below.
    # Advisory only: CLIQ never auto-runs — the engineer answers the y/n.
    destructive = build_destructive_confirmation(raw, mode)
    if destructive is not None:
        obs.log_validation(
            "advisory_tier", "pre_enter", "destructive",
            detail=(raw.split() or ["?"])[0],
        )
        return {
            "kind": destructive.kind,
            "message": destructive.message,
            "ansiMessage": destructive.ansi_message,
            "danger": True,
            "fix": destructive.fix,
        }
    if _is_exec_mode_do(mode, raw):
        inner = raw[3:].strip() if raw.lower().startswith("do ") else ""
        return {
            "kind": "wrong_mode",
            "message": _EXEC_DO_HINT_MSG,
            "fix": inner or None,
        }
    # Bug 6 — proactive config-mode 'do' hint. Fire ON-TYPE (before Enter) the
    # instant the engineer starts an EXEC-only verb while in any config submode,
    # instead of only after the device rejects it. Mode-driven, so it takes
    # priority over the status-based hints below.
    do_hint = _config_mode_do_hint(analysis)
    if do_hint is not None:
        return do_hint
    status = analysis.status
    e = analysis.explanation
    if status == "typo":
        # Bug K — do NOT fire a correction mid-type. The 195-cmd table fuzzy
        # match snaps a still-incomplete 'sh platform' to a dissimilar entry
        # ('show vlan'), which is both premature and wrong. Corrections fire
        # ONLY after submission / a device '^' rejection (device-grounded);
        # ghost-text completion still flows live. Stay silent here.
        return None
    if status == "wrong_device":
        return {
            "kind": "wrong_device",
            "message": e.what if e else "Not available on this device type.",
            "fix": None,
        }
    if status == "wrong_mode":
        # Mode-transition awareness: 'configure terminal' (and its abbreviations
        # 'conf t' / 'config t') don't RUN in a mode — they ENTER config. The
        # engine matches it to its enable-mode home, so a naive comparison yells
        # "requires 'enable'" even when the user is already IN config. Recognise
        # that here via the target-mode map:
        #   - already at/below the target (config / config-if / config-subif):
        #     redundant no-op -> gentle hint, never "requires 'enable'".
        #   - otherwise (exec / enable): a valid FORWARD transition -> no warning.
        cmd_text = analysis.expanded_command or getattr(analysis, "raw_input", "") or ""
        if mode_transition_target(cmd_text) is not None:
            if is_redundant_mode_transition(cmd_text, analysis.mode):
                return {
                    "kind": "wrong_mode",
                    "message": "Already in config mode \u2014 'configure terminal' is not needed here.",
                    "fix": None,
                }
            return None
        req = analysis.matched_command.mode if analysis.matched_command else "?"
        fix = e.fix_mode_path[0][1] if e and e.fix_mode_path else None
        return {
            "kind": "wrong_mode",
            "message": f"Wrong mode \u2014 requires '{req}'",
            "fix": fix,
        }
    if status == "wrong_mode_do":
        return {
            "kind": "wrong_mode",
            "message": f"Use 'do': {analysis.do_suggestion}",
            "fix": analysis.do_suggestion,
        }
    if status == "missing_arg":
        return {
            "kind": "missing_arg",
            "message": e.how if e else "Missing argument",
            "fix": None,
        }
    if status == "invalid_arg":
        # Bug 1 — never warn before Enter for a sub-keyword the device accepts.
        # The allowlist fast-path (offline, no '?'-query so the keystroke path
        # stays fast) suppresses the false-positive advisory; anything else
        # stays a soft hint that the device confirms or contradicts on Enter.
        head = analysis.matched_command.full_command if analysis.matched_command else ""
        bad_token = analysis.arg_error.provided if analysis.arg_error else ""
        if classify_subkeyword(head_token(head), bad_token).accepted:
            return None
        return {
            "kind": "typo",
            "message": (e.why if e else None) or (e.what if e else "Invalid argument"),
            "fix": None,
        }
    if status == "unknown":
        # Extra-trailing-token detection (device-agnostic, no hardcoding):
        # if dropping the LAST whitespace token turns the input into a valid,
        # complete command, the engineer almost certainly fat-fingered a
        # trailing word (e.g. "show running-config run" -> "show running-config").
        reduced = _reduced_command_fix(analysis)
        if reduced:
            return {
                "kind": "typo",
                "message": f"Did you mean: {reduced}?",
                "fix": reduced,
            }
    return None


def _device_help_text(device, prefix: str) -> str:
    """Capture a device '?'-help page for ``prefix`` (advisory, never executes).

    Drives the device's own context-help so the 'did you mean?' suggestion is
    grounded in the real, live token set. Self-cleaning: any paged help is
    drained/quit so the device input line is left empty for the next command.
    """
    page, more = device.context_help_start((prefix or "") + " ")
    pages = [page]
    guard = 0
    # Bug Q — capture the COMPLETE option set (a busy 'ip' prefix on a router
    # spans many pages). 24 pages comfortably covers it without risking a stuck
    # pager; the guard below still quits cleanly if the device keeps paging.
    while more and guard < 24:
        page, more = device.context_help_more(" ")
        pages.append(page)
        guard += 1
    if more:
        # Still paging after the guard — quit the pager so the device is clean.
        try:
            device.context_help_more("q")
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
    return "\n".join(pages)


def _reduced_command_fix(analysis) -> str | None:
    """Return a valid command formed by dropping the last token, else None.

    Used to catch an extraneous trailing word on an otherwise-complete command.
    Re-analyses the shortened input with the SAME engine/mode so the check is
    fully driven by the command model (never command-specific heuristics).
    """
    raw = (analysis.raw_input or "").strip()
    tokens = raw.split()
    if len(tokens) < 2:
        return None
    reduced_text = " ".join(tokens[:-1])
    try:
        reduced = _engine.analyze(
            reduced_text, analysis.mode, is_executing=True,
        )
    except Exception:  # pragma: no cover - advisory path must never crash typing
        return None
    if reduced.status == "ok" and reduced.is_complete:
        return reduced.expanded_command or reduced_text
    return None


def _valid_command_fix(candidate: str, mode: str) -> str | None:
    """Re-run a 'did you mean?' candidate through the engine so EVERY mistaken
    token is corrected — not only the one under the device '^' caret.

    The device-driven suggestion fixes just the caret token (e.g. 'shw'->'show')
    and preserves the rest verbatim, so 'do shw valn' collapses to the STILL-
    invalid 'do show valn'. Feeding that back through the model-driven engine
    corrects/expands the remaining tokens ('valn'->'vlan') to yield a fully
    valid command ('do show vlan'). This is generic — it applies the same second
    pass to any command, wherever the leftover mistake is. Returns the corrected
    command, or None when the engine can't improve it (so the caller keeps the
    original, device-grounded suggestion).
    """
    text = (candidate or "").strip()
    if not text:
        return None
    try:
        a = _engine.analyze(text, mode, is_executing=True)
    except Exception:  # pragma: no cover - advisory path must never crash
        return None
    if a.status in ("ok", "typo"):
        fixed = (a.expanded_command or "").strip()
        if fixed and fixed.lower() != text.lower():
            return fixed
    return None


def _expand_abbrev_line(command: str) -> str | None:
    """Expand every keyword + interface abbreviation on a line, or None.

    Tier-3 (abbreviation-expansion) helper for the post-Enter advisory router.
    Expands valid keyword abbreviations (``sh`` -> ``show``, ``int`` ->
    ``interface``) against the command model and canonicalises interface short
    names (``g0/1`` -> ``GigabitEthernet0/1``). Returns the fully-expanded line
    only when it DIFFERS from the input AND every alphabetic token resolved —
    if a would-be keyword token is unresolved it is a genuine TYPO, not an
    abbreviation, so this returns None and lets the fuzzy tier (5) handle it.
    """
    toks = (command or "").split()
    if not toks:
        return None
    try:
        result = _engine.matcher.expander.expand(toks)
    except Exception:  # pragma: no cover - advisory path must never crash
        return None
    unresolved = set(result.unresolved)
    out: list[str] = []
    changed = False
    orig_tokens = list(result.original_tokens)
    for idx, (orig, exp) in enumerate(zip(orig_tokens, result.expanded_tokens)):
        iface = expand_interface_token(orig)
        if iface is not None:
            out.append(iface)
            changed = changed or (iface != orig)
            continue
        # 'int'/'inter'/... immediately followed by an interface token is
        # unambiguously the 'interface' keyword (not 'internal'): the operand
        # proves the intent. Resolve it so 'int g0/1' -> 'interface g0/1'.
        nxt = orig_tokens[idx + 1] if idx + 1 < len(orig_tokens) else ""
        if (
            orig.isalpha()
            and len(orig) >= 2
            and "interface".startswith(orig.lower())
            and expand_interface_token(nxt) is not None
        ):
            out.append("interface")
            changed = changed or (orig.lower() != "interface")
            continue
        # A purely-alphabetic token that resolved to no keyword is a typo, not a
        # valid abbreviation — bail so the guarded fuzzy matcher corrects it.
        if orig in unresolved and orig.isalpha() and len(orig) > 1:
            return None
        out.append(exp)
        changed = changed or (exp.lower() != orig.lower())
    if not changed:
        return None
    return " ".join(out)


@app.on_event("startup")
async def _startup() -> None:
    """Pre-warm the Phase 6 advisory stack so the first request isn't slow."""
    # Log role separation at startup.
    log.info(
        "CLIQ Web Services initialized. Legacy intelligence engine (%d commands) "
        "reserved for keystroke suggestions + safety validation. "
        "Phase 6 Q&A routed to advisory stack (see next log for KB record count).",
        len(_db.commands),
    )
    # Reservation posture + NetSim key-health probe (network call) off-loop.
    await asyncio.to_thread(_log_reservation_posture)
    await asyncio.to_thread(_init_advisory_stack)


def _serialize_resolution(pr: Any) -> dict[str, Any]:
    """Serialize a PastResolution (which nests a MemoryRecord) to a flat dict."""
    mr = pr.memory_record
    return {
        "memory_id": mr.memory_id or "",
        "issue_text": mr.issue_text,
        "commands": mr.commands,
        "platform": mr.platform,
        "device_name": mr.device_name,
        "outcome": mr.outcome,
        "outcome_notes": mr.outcome_notes or "",
        "similarity": round(pr.final_rank_score, 4),
    }


# ---------------------------------------------------------------------------
# Pydantic request models for Phase 6 advisory REST endpoints
# ---------------------------------------------------------------------------

class IntelAskRequest(BaseModel):
    question: str
    platform: Optional[str] = None
    mode: Optional[str] = None
    device_id: Optional[str] = None


class IntelValidateRequest(BaseModel):
    command: str
    platform: str = "iosxe"
    current_mode: str = "exec"


class IntelExplainRequest(BaseModel):
    command: str
    platform: Optional[str] = None
    mode: Optional[str] = None
    device_id: Optional[str] = None


class IntelRecallRequest(BaseModel):
    query: str
    platform: Optional[str] = None
    top_k: int = 3


class IntelResolveRequest(BaseModel):
    issue_text: str
    commands: list[str]
    platform: str = "iosxe"
    device_name: Optional[str] = None
    outcome_notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Advisory-endpoint authorization (CRITICAL: multi-tenant isolation)
# ---------------------------------------------------------------------------

def require_session(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> str:
    """Validate the caller's session token and return it, else FAIL CLOSED (401).

    Single shared dependency for every advisory ``/api/intel/*`` endpoint. The
    bearer token (``Authorization: Bearer <token>``, same model as ``/api/auth``
    /``/api/devices``) is matched against :data:`SESSIONS`. A missing/unknown
    token is rejected with 401 BEFORE any advisory data is produced, so an
    unauthenticated caller can never reach the KB, the assistant, or — via the
    ownership-scoped lookup below — another session's live-device runtime.
    """
    tail = (request.url.path.rsplit("/", 1)[-1] or "session").strip()
    op = f"intel_{tail}" if tail else "intel"
    token = ""
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
        else:
            token = authorization.strip()
    if not token or token not in SESSIONS:
        obs.log_event(
            "authz", op, "gate", "denied",
            status_code=401, reason="no_token", owns_runtime=False,
        )
        raise HTTPException(status_code=401, detail="Authentication required")
    return token


def _owned_runtime(token: str, device_id: Optional[str]) -> Optional["DeviceRuntime"]:
    """Return the live runtime for ``(token, device_id)`` or ``None``.

    Ownership-scoped: a ``device_id`` registered to a DIFFERENT session (or not
    registered at all) resolves to ``None`` — another session's runtime state is
    never returned. ``None``/empty ``device_id`` means "KB-only advisory".
    """
    if not device_id:
        return None
    return _DEVICE_RUNTIMES.get((token, device_id))


def _device_owned_elsewhere(token: str, device_id: Optional[str]) -> bool:
    """True if ``device_id`` is a live runtime owned by a DIFFERENT session.

    Observability-only helper: lets the authz log distinguish a cross-session
    probe (``reason="cross_session"``) from a plain "no device attached". It
    never exposes the other session's runtime — only the fact of ownership.
    """
    if not device_id:
        return False
    return any(k[1] == device_id and k[0] != token for k in _DEVICE_RUNTIMES)


# ---------------------------------------------------------------------------
# Phase 6 advisory REST endpoints (read-only; never touch a device)
# ---------------------------------------------------------------------------

@app.post("/api/intel/ask")
async def intel_ask(
    payload: IntelAskRequest,
    token: str = Depends(require_session),
) -> dict[str, Any]:
    """Natural-language Q&A: maps a question to tiered command answers.
    
    SAFEGUARD: This endpoint routes to Phase 6 assistant (1713-record KB),
    NOT the legacy engine (195 commands). Legacy engine is for keystroke
    suggestions + safety validation only.
    """
    _init_advisory_stack()
    question = payload.question.strip().lstrip("?").strip()
    log.debug(
        "REST ask (Phase 6 Q&A): question=%r platform=%s mode=%s kb_path=%s",
        question, payload.platform, payload.mode, _KB_DB,
    )
    if _assistant is None:
        return {
            "tier": "fallback",
            "message": "Knowledge base not available — start with 'show' or consult Cisco docs.",
            "results": [],
            "groups": [],
        }
    if not question:
        raise HTTPException(status_code=422, detail="question cannot be empty")

    resp, annotated = await _qa_respond(question, payload.platform, payload.mode)
    log.info(
        "REST ask handler: tier=%s results=%d groups=%d annotated=%s query=%r",
        resp.response_tier,
        len(resp.results),
        len(resp.grouped_results),
        annotated,
        question,
    )
    # Bug P (Ask path) — reconcile KB/RAG answers against the LIVE device when one
    # is connected (device > cache > KB). Ownership-scoped: only THIS session's
    # own device runtime is ever read; another session's device_id resolves to
    # None and the endpoint falls back to the unchanged KB-only behavior below.
    runtime = _owned_runtime(token, payload.device_id)
    obs.log_event(
        "authz", "intel_ask", "gate", "allowed",
        reason="cross_session" if (runtime is None and _device_owned_elsewhere(
            token, payload.device_id)) else "ok",
        owns_runtime=runtime is not None,
    )
    device_platform = (runtime.node_type if runtime else None) or payload.platform
    results_list, groups_list = await asyncio.to_thread(
        _apply_qa_reconciliation, resp, runtime, device_platform, question,
    )
    # Phase 5 — scenario-based advisory toolkit (conservative intent, capability/
    # mode gated, live-device rejections dropped). Static in-memory lookup.
    toolkit = _build_toolkit_advisory(question, runtime, payload.mode)
    # Feature 3 — command discovery / reference (async ?-walk + learned + KB).
    reference = await _build_discovery_reference(
        question, runtime, payload.mode, payload.device_id,
    )
    # Feature 2 — set + mirror sticky protocol context on advisory intent.
    ctx_protocol = (toolkit or {}).get("protocol") or (reference or {}).get("protocol")
    active_context = None
    if ctx_protocol and runtime is not None:
        active_context = _set_active_context(runtime, ctx_protocol, "advisory")
    message = resp.message
    if annotated and (results_list or groups_list):
        message = f"{resp.message} (from iosxe reference — verify on your platform)"
    # Bug 3: curated recipe first, KB assembly (UNVERIFIED) only as fallback.
    rest_sequence: list[dict[str, Any]] = []
    if _is_setup_query(question):
        curated = _resolve_recipe(question, payload.platform)
        if curated:
            rest_sequence = curated
            message = f"{message}  (curated, verified procedure)"
        else:
            # Reconcile the UNVERIFIED KB-assembled steps against the device too,
            # so a device-rejected / wrong-capability command is never offered as
            # a "suggested fix" step.
            rest_sequence = _filter_sequence_for_device(_build_sequence(resp), runtime)
            if rest_sequence:
                message = (
                    f"{message}  (assembled from KB — UNVERIFIED, "
                    f"review each step before running)"
                )
    return {
        "tier": resp.response_tier,
        "message": message,
        "results": results_list,
        "groups": groups_list,
        "sequence": rest_sequence,
        "toolkit": toolkit,
        "reference": reference,
        "active_context": active_context,
        "past_resolutions": [
            _serialize_resolution(pr)
            for pr in (resp.past_resolutions or [])
        ],
    }


@app.post("/api/intel/validate")
async def intel_validate(
    payload: IntelValidateRequest,
    token: str = Depends(require_session),
) -> dict[str, Any]:
    """Safety validation: checks a command for blocklist/platform/mode issues."""
    _init_advisory_stack()
    if _validator is None:
        return {"valid": True, "verdict": "ok", "severity": "info", "reason": "Validator unavailable.", "suggested_action": None}

    from cliq.validation.schema import ValidationContext
    ctx = ValidationContext(
        command=payload.command.strip(),
        device_platform=payload.platform,
        current_mode=payload.current_mode,
    )
    verdict = await asyncio.to_thread(_validator.validate, ctx)
    return {
        "valid": verdict.valid,
        "verdict": verdict.verdict_type.value,
        "severity": verdict.severity.value,
        "reason": verdict.reason,
        "suggested_action": verdict.suggested_action,
        "normalized_command": verdict.normalized_command,
    }


@app.post("/api/intel/explain")
async def intel_explain(
    payload: IntelExplainRequest,
    token: str = Depends(require_session),
) -> dict[str, Any]:
    """Plain-language explanation of a command (KB description + safety verdict).

    Advisory only — never executes. Powers the VS Code-style 'Explain' action.

    Bug P (Ask path parity) — when a live device is connected, defer to it: if
    the device just SYNTAX-rejected this exact command, ``_explain_command``
    returns the ``wrong_device`` verdict instead of a (possibly mis-platformed)
    KB claim. The device's node type also becomes the authoritative platform for
    KB platform-mismatch demotion.

    Ownership-scoped: only THIS session's own device runtime is consulted; a
    ``device_id`` belonging to another session resolves to None (KB-only), so
    another session's ``node_type``/``rejected`` state is never disclosed.
    """
    runtime = _owned_runtime(token, payload.device_id)
    obs.log_event(
        "authz", "intel_explain", "gate", "allowed",
        reason="cross_session" if (runtime is None and _device_owned_elsewhere(
            token, payload.device_id)) else "ok",
        owns_runtime=runtime is not None,
    )
    device_platform = payload.platform
    device_rejected = False
    if runtime is not None:
        device_platform = runtime.node_type or payload.platform
        rejected = {c.strip().lower() for c in getattr(runtime, "rejected", set())}
        device_rejected = (payload.command or "").strip().lower() in rejected
    return await asyncio.to_thread(
        _explain_command, payload.command, device_platform, payload.mode,
        device_rejected=device_rejected,
    )


@app.post("/api/intel/recall")
async def intel_recall(
    payload: IntelRecallRequest,
    token: str = Depends(require_session),
) -> dict[str, Any]:
    """Semantic recall: returns past resolutions similar to the query."""
    _init_advisory_stack()
    if _memory_store is None:
        return {"resolutions": []}
    resolutions = await asyncio.to_thread(
        _memory_store.recall_similar,
        payload.query.strip(),
        payload.top_k,
        0.4,
        payload.platform,
    )
    return {
        "resolutions": [_serialize_resolution(pr) for pr in resolutions],
    }


@app.post("/api/intel/resolve")
async def intel_resolve(
    payload: IntelResolveRequest,
    token: str = Depends(require_session),
) -> dict[str, Any]:
    """Store a resolved issue in operational memory so it can be recalled later."""
    _init_advisory_stack()
    if _memory_store is None:
        return {"success": False, "memory_id": None, "message": "Memory store unavailable."}
    try:
        memory_id = await asyncio.to_thread(
            _memory_store.store_memory,
            payload.issue_text.strip(),
            payload.commands,
            payload.platform,
            payload.device_name,
            "resolved",
            payload.outcome_notes,
            "cross_device",
        )
        return {"success": True, "memory_id": memory_id, "message": "Resolution stored."}
    except Exception as exc:
        log.error("intel_resolve error: %s", exc)
        return {"success": False, "memory_id": None, "message": str(exc)}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _is_host_resolution_error(exc: BaseException) -> bool:
    """True if ``exc`` is a DNS / host-resolution or malformed-URL failure.

    Detects the ``requests``/``urllib3`` name-resolution family plus the plain
    string forms, so a pasted-URL / bad-host mistake is reported as its real
    cause ("could not resolve host") instead of the misleading auth / NetSim-key
    guidance.
    """
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        if name in (
            "NameResolutionError", "ConnectionError", "MaxRetryError",
            "NewConnectionError", "gaierror",
        ):
            text = str(cur).lower()
            if any(
                t in text
                for t in (
                    "resolve", "name resolution", "nodename",
                    "getaddrinfo", "temporary failure in name",
                    "no address associated", "failed to resolve",
                )
            ):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_backend_unreachable_error(exc: BaseException) -> bool:
    """True if ``exc`` is a network-unreachable / timeout (but NOT a DNS) failure.

    A resolvable host that refuses the connection, times out, or is otherwise
    unreachable (VPN down, host offline) is reported as "could not reach the CML
    host" — distinct from a DNS/hostname mistake and from a credential rejection.
    """
    if _is_host_resolution_error(exc):
        return False  # DNS is reported as its own, more specific cause
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        if name in (
            "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
            "ConnectionRefusedError", "ConnectionResetError", "MaxRetryError",
            "NewConnectionError", "ProxyError", "SSLError", "TimeoutError",
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _log_reservation_posture() -> None:
    """Startup check: log standalone controller session authority posture."""
    policy = reservation_policy()
    log.info("CLIQ standalone mode active: controller session authority.")
    obs.log_event(
        "connection", "reservation_posture", "startup", "controller_standalone",
        policy=policy, api_key_present=False, verified=True, key_health="disabled",
    )


def _reservation_decision(auth: Any, *, proxy: Optional[str] = None, detail: str = ""):
    """Resolve the shared reservation policy for a session's auth manager.

    Consulted by BOTH ``/api/auth`` and the console re-attach gate so they can
    never diverge: detect the NetSim reservation state (+ the distinct
    ``unverifiable`` cause), apply the configured policy
    (:func:`resolve_reservation_access`), emit the observability event, and
    return the :class:`ReservationDecision`. Never raises.
    """
    policy = reservation_policy()
    if policy == "disabled":
        decision = resolve_reservation_access("active")
        # No NetSim authority check was performed by explicit operator choice.
        obs.log_event(
            "connection", "reservation_check", "disabled", "allowed",
            reservation_state="active", authority="disabled", policy=policy,
            unverifiable_cause="", api_key_present=reservation_api_key_present(),
            verified=False, gated=False, detail=detail,
        )
        return decision

    state, cause = "unverifiable", "netsim_unreachable"
    try:
        detect = getattr(auth, "check_reservation_detail", None)
        if callable(detect):
            det = detect(proxy=proxy)
            state, cause = det.state, getattr(det, "cause", "")
        else:  # older fake/stub exposing only the state
            state = auth.check_reservation_state(proxy=proxy)
            cause = "" if state != "unverifiable" else (
                "no_key" if not reservation_api_key_present() else "netsim_unreachable"
            )
    except Exception:  # noqa: BLE001 - detection never raises, but be defensive
        state, cause = "unverifiable", "netsim_unreachable"
    decision = resolve_reservation_access(state, cause)
    # 'controller_fallback' authority when we proceed WITHOUT NetSim verification
    # (unverifiable + warn); otherwise the NetSim authority produced the verdict.
    authority = "controller_fallback" if (
        state == "unverifiable" and decision.allowed
    ) else "netsim"
    # Honest reporting: a session is only 'verified' when a real NetSim reservation
    # lookup produced the verdict. The unverifiable/warn fallback is NEVER verified.
    verified = state != "unverifiable"
    obs.log_event(
        "connection", "reservation_check", authority, decision.outcome,
        reservation_state=state, authority=authority, policy=decision.policy,
        unverifiable_cause=cause if state == "unverifiable" else "",
        api_key_present=reservation_api_key_present(), verified=verified,
        gated=True, detail=detail,
    )
    return decision


@app.post("/api/auth")
def api_auth(payload: AuthRequest) -> dict[str, Any]:
    auth = CMLAuthManager()
    raw_host = payload.host
    host = normalize_cml_host(raw_host)
    proxy = getattr(payload, "proxy", None)

    def _reject(reason: str, message: str, error_kind: str,
                *, status: int = 401, discarded: bool = False) -> None:
        """Fail closed: log the outcome and raise a non-2xx with a real cause."""
        obs.log_event(
            "auth", "login", "gate", "rejected",
            status_code=status, reason=reason,
            host_normalized=host, token_source="live",
            cached_token_discarded=discarded,
        )
        raise HTTPException(
            status_code=status,
            detail={"error": message, "errorKind": error_kind,
                    "host": host, "success": False},
        )

    # Empty / malformed host → clear, real-cause message (never the misleading
    # "no NetSim API key" text, which was surfacing for DNS/URL problems).
    if not host:
        _reject(
            "invalid_host",
            "Invalid CML host. Enter a bare hostname such as "
            "'cml.example.com' (do not include http:// or a path).",
            "invalid_host", status=400,
        )

    username = payload.username.strip()
    password = payload.password.strip()

    try:
        # login() fails closed: it verifies the CURRENT credentials live and
        # never falls back to a cached token when live auth fails. A stale token
        # can no longer bypass a wrong password / expired reservation.
        auth.login(host, username, password)
        labs = auth.get_labs()
    except Exception as exc:  # noqa: BLE001 - map every failure to a non-2xx
        # DNS / URL malformation (e.g. an unresolvable host) → surface the ACTUAL
        # cause, not the auth/NetSim guidance.
        if _is_host_resolution_error(exc):
            _reject(
                "dns_failure",
                f"Could not resolve host '{host}'. Check the hostname "
                "(a bare host, no http:// or trailing path) and your VPN/DNS.",
                "dns_failure", discarded=True,
            )
        # A CML 401/403 → classify the DISTINCT cause (wrong password vs. expired
        # reservation vs. an invalid NetSim key that blocks verification) so the
        # user sees an accurate message, not a single catch-all.
        if is_http_auth_error(exc):
            diag = diagnose_cml_auth_failure(host, exc=exc, proxy=proxy)
            status = 403 if diag.kind == "forbidden" else 401
            _reject(diag.kind, diag.message, diag.kind, status=status, discarded=True)
        # A resolvable host we simply could not reach (VPN down, host offline,
        # connection refused/timeout) → its own distinct message.
        if _is_backend_unreachable_error(exc):
            _reject(
                "backend_unreachable",
                f"Could not reach the CML host '{host}'. Check that the host is "
                "online and your VPN/network connection is up.",
                "backend_unreachable", discarded=True,
            )
        # Anything else also fails closed rather than leaking a stack trace.
        _reject("auth_error", str(exc) or "Authentication failed.",
                "auth_error", discarded=True)

    # ── Reservation authority gate (NetSim) ──────────────────────────────────
    # ── Reservation authority gate (NetSim) + policy ─────────────────────────
    # A successful CML login only proves the credentials + controller are good —
    # NOT that the NetSim booking window is still open (the controller stays
    # "up" during the post-expiry grace period). Apply the SHARED policy the
    # console re-attach gate uses: a verified-EXPIRED reservation is always
    # refused; an UNVERIFIABLE reservation is governed by CLIQ_RESERVATION_POLICY
    # (default 'warn' → allow with a visible banner; 'enforce' → refuse).
    decision = _reservation_decision(auth, proxy=proxy, detail=host)
    if not decision.allowed:
        if decision.state == "expired":
            _reject("reservation_expired", decision.message,
                    "reservation_expired", discarded=True)
        _reject("unverifiable", decision.message, "unverifiable", discarded=True)

    token = str(uuid.uuid4())
    SESSIONS[token] = AuthSession(
        host=host,
        username=username,
        password=password,
        auth=auth,
    )

    # Phase A usage analytics (local-only, Phase H seed). No host/user/
    # credential is logged — only the event + a count.
    obs.log_usage("session_start", labs=len(labs))
    obs.log_event(
        "auth", "login", "gate", "success",
        reason="ok", host_normalized=host,
        token_source="live", cached_token_discarded=False,
    )

    return {
        "success": True,
        "token": token,
        "host": host,
        # Non-blocking warning when proceeding without NetSim verification
        # (unverifiable + warn policy). Empty on a fully-verified login.
        "warning": decision.message if decision.warn else "",
        # Honest state flag driving the persistent UI indicator: True ONLY for an
        # unverified session (unverifiable + warn). A NetSim-verified login is
        # never 'unverified'.
        "unverified": decision.state == "unverifiable" and decision.allowed,
        "labs": [
            {
                "id": lab.lab_id,
                "title": lab.title,
                "state": lab.state,
            }
            for lab in labs
        ],
    }


@app.post("/api/devices")
def api_devices(payload: DevicesRequest) -> dict[str, Any]:
    session = SESSIONS.get(payload.token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid token")
    host = normalize_cml_host(payload.host)
    if normalize_cml_host(session.host) != host:
        raise HTTPException(status_code=400, detail="Host/token mismatch")

    devices = session.auth.get_devices(payload.lab_id)
    topology = session.auth.get_topology(payload.lab_id)

    # Phase A usage analytics (local-only, Phase H seed).
    obs.log_usage("lab_opened", detail=str(payload.lab_id), devices=len(devices))

    DEVICES_BY_TOKEN[payload.token] = {
        d.node_id: {
            "id": d.node_id,
            "lab_id": payload.lab_id,
            "label": d.label,
            "type": d.node_type,
            "capability": sorted(platform_capabilities(d.node_type)),
            "status": "connected",
            "mode": "exec",
            "cli_connected": False,
            "node_state": d.state,
            "console_path": d.console_path,
        }
        for d in devices
    }

    return {
        "devices": [
            {
                "id": d.node_id,
                "label": d.label,
                "type": d.node_type,
                "capability": sorted(platform_capabilities(d.node_type)),
                "mode": "exec",
                "status": "connected",
                "cli_connected": False,
                "node_state": d.state,
            }
            for d in devices
        ],
        "topology": {
            "nodes": [
                {
                    "id": n["id"],
                    "position": {
                        "x": n.get("x", 0),
                        "y": n.get("y", 0),
                    },
                    "data": {
                        "label": n.get("label", n["id"]),
                        "type": n.get("type", "unknown"),
                        "node_state": n.get("state", "unknown"),
                        "cli_connected": False,
                    },
                }
                for n in topology.get("nodes", [])
            ],
            "links": [
                {
                    "id": l["id"],
                    "label": l.get("label", l["id"]),
                    "source": l.get("node_a", ""),
                    "target": l.get("node_b", ""),
                    "sourceHandle": l.get("node_a_intf", ""),
                    "targetHandle": l.get("node_b_intf", ""),
                }
                for l in topology.get("links", [])
            ],
        },
    }


@app.post("/api/ssh/connect")
def api_ssh_connect(payload: SSHConnectRequest) -> dict[str, Any]:
    """Register an SSH terminal-server console session and its single device.

    This is the SSH analogue of ``/api/auth`` + ``/api/devices`` combined: the
    real device is not reached here (the live SSH attach happens on the WS
    ``connect_device``), we only mint a token and register a synthetic device so
    the SAME WebSocket terminal loop can drive it. The password is held in
    memory only for the live session and is NEVER logged (redaction) or written
    to disk.
    """
    host = payload.host.strip()
    console_user = payload.console_user.strip()
    console_port = str(payload.console_port).strip()
    if not host or not console_user:
        raise HTTPException(status_code=422, detail="host and console_user are required")

    token = str(uuid.uuid4())
    SESSIONS[token] = AuthSession(
        host=host,
        username=console_user,
        password=payload.password,
        auth=None,
        connection_type="ssh",
    )

    device_id = f"ssh-{uuid.uuid4().hex[:8]}"
    label = f"{console_user}:{console_port}@{host}" if console_port else f"{console_user}@{host}"
    # console_path is used ONLY as the globally-unique exclusive-attach lock key.
    console_path = f"ssh://{host}/{console_port or '0'}/{console_user}"
    DEVICES_BY_TOKEN[token] = {
        device_id: {
            "id": device_id,
            "lab_id": "ssh",
            "label": label,
            "type": "",  # unknown until 'show version' → PID on connect
            "connection_type": "ssh",
            "ssh_host": host,
            "ssh_console_user": console_user,
            "ssh_console_port": console_port,
            "ssh_device_type": (payload.device_type or "cisco_ios").strip() or "cisco_ios",
            "console_path": console_path,
            "status": "connected",
            "mode": "exec",
            "cli_connected": False,
            "node_state": "BOOTED",
        }
    }

    # Analytics: SSH session created. Host is fine to log; credentials are not
    # present in this event at all.
    obs.log_usage("ssh_session_start", detail=host)

    return {
        "success": True,
        "token": token,
        "host": host,
        "connection_type": "ssh",
        "device": {
            "id": device_id,
            "label": label,
            "type": "",
            "capability": [],
            "mode": "exec",
            "status": "connected",
            "cli_connected": False,
            "node_state": "BOOTED",
        },
    }


@app.websocket("/ws")
async def ws_terminal(websocket: WebSocket, token: str, host: str) -> None:
    session = SESSIONS.get(token)
    host = normalize_cml_host(host)
    if not session or normalize_cml_host(session.host) != host:
        await websocket.close(code=1008, reason="Invalid session")
        return

    await websocket.accept()

    # In-session reconnect — this token now has a LIVE socket again, so cancel
    # any pending grace-reaper countdown from a prior socket drop. The retained
    # server-side session state (devices, held creds, memory) is reused as-is:
    # the client re-issues connect_device on (re)open, resuming under the SAME
    # token with no re-login and no fresh terminal.
    _resumed = _SESSION_LAST_SEEN.pop(token, None) is not None
    if _resumed:
        obs.log_event(
            "connection", "reconnect", "websocket", "resumed",
            connection_type=getattr(session, "connection_type", "cml"),
            detail=token[:8],
        )

    # Unique owner id for this socket; identifies who holds each console lock.
    session_id = uuid.uuid4().hex
    runtimes: dict[str, DeviceRuntime] = {}
    # Bug 7: devices parked at an interactive login/password prompt, awaiting
    # operator credentials. Kept OUT of `runtimes` until login completes so the
    # command path never touches a half-authenticated channel. Channels stay
    # open here; credentials are exchanged via the `auth_credentials` handler
    # and are NEVER stored in this map or anywhere on disk.
    pending_auth: dict[str, dict[str, Any]] = {}

    # Destructive-action confirmation gate (Tier 1 safety). A blocking, stateful
    # gate per device: a destructive command ('no ...', shutdown, clear, erase,
    # reload, write erase, delete, ...) is HELD here on Enter and forwarded to
    # the device ONLY after the operator explicitly answers 'y'. Advisory-only —
    # CLIQ never auto-executes; the device performs the action on release.
    confirm_gate = ConfirmationGate()

    # Bug 8 / Bug 9 — long-lived background tasks (VLAN capability prewarm and
    # the console keepalive loop). Held in a set so the event loop keeps a
    # strong reference (fire-and-forget tasks are otherwise GC-eligible) and so
    # the `finally` block can cancel them cleanly on disconnect.
    _bg_tasks: set[asyncio.Task] = set()
    _keepalive_task: asyncio.Task | None = None

    def _spawn_bg(coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return task

    async def _prewarm_vlan_capability(device_id: str) -> None:
        """Deprecated — connect-time '?'-priming removed.

        Auto-issuing 'show vlan ?' on connect was a device-inappropriate, wasted
        round-trip (e.g. asking a router about VLANs) and violated the rule that
        a '?'-capture must fire ONLY in response to real user input. The VLAN
        verdict is now learned lazily from actual keystrokes. Retained as an
        explicit no-op so any stray caller cannot re-introduce a connect-time
        device round-trip.
        """
        return

    async def _seed_ghost_from_cache(node_type: str, mode: str) -> None:
        """Part 1 — seed the ghost index from the durable command cache.

        Pulls device-confirmed commands valid for THIS device (device-type PI
        rows + this platform's PD rows) and adds them to the live ghost index so
        a previously captured truth survives a hard refresh/restart and is shared
        cross-user. Best-effort; never blocks or disrupts the session.
        """
        if not node_type:
            return
        try:
            cache = await asyncio.to_thread(_get_cmd_cache)
            if cache is None or _ghost_index is None:
                return
            cmds = await asyncio.to_thread(cache.known_commands, node_type, None)
            seeded = 0
            for cmd in cmds:
                # Stored entries are value-stripped TEMPLATES; seed only the clean
                # keyword prefix (up to the first placeholder) so inline ghost text
                # never renders a '<interface>'/'<ip>' placeholder. An operand-free
                # template seeds whole.
                prefix = clean_prefix(cmd)
                if not prefix:
                    continue
                _ghost_index.add(prefix, tier=2, count=1, live=True)
                seeded += 1
            if seeded:
                obs.log_event(
                    "cache", "db_readback", "seed", "hit",
                    store="accepted_commands",
                    platform=node_type, mode=mode, source="device",
                    detail=str(seeded),
                )
        except Exception:  # pragma: no cover - seeding is best-effort
            log.debug("ghost seed from cache skipped", exc_info=True)

    def _transport_label(device) -> str:
        """'ssh' for the real-HW netmiko console, 'cml' for the CML proxy."""
        return "ssh" if isinstance(device, SSHConsoleDevice) else "cml"

    async def _mark_session_dead(device_id, runtime, reason, prompt_last_seen) -> None:
        """Confirmed session death: emit the notice, halt I/O, release the runtime.

        Idempotent — a device already flagged dead is not re-processed. Mirrors the
        socket-close disconnect cleanup path (best-effort ``device.disconnect`` +
        registry/console-lock release) so no stale runtime lingers to mislead
        reconciliation/gating, then surfaces a SINGLE unambiguous ``session_expired``
        notice over the EXISTING WebSocket channel — it lands in the same
        terminal/advisory UI with a reconnect affordance (the existing connect flow).
        The device object is dropped from ``runtimes`` and ``_DEVICE_RUNTIMES`` so a
        subsequent ``connect_device`` re-mints a fresh, healthy runtime.
        """
        if runtime.dead:
            return
        rv = reason.value if isinstance(reason, DeathReason) else (str(reason) or "")
        runtime.dead = True
        runtime.dead_reason = rv
        runtime.status = "expired"
        runtime.pager_active = False
        runtime.awaiting_input = False
        transport = _transport_label(runtime.device)
        # Tear the (gone) channel down best-effort so no in-flight read/pager lingers
        # against a socket that is already released.
        try:
            await asyncio.to_thread(runtime.device.disconnect)
        except Exception:  # pragma: no cover - disconnect is best-effort
            pass
        # Clean, actionable UI notice over the EXISTING channel (never a hang, empty
        # stream, or silent drop). Surfaces in the SAME terminal as a reconnect prompt.
        await websocket.send_json(
            {
                "type": "session_expired",
                "device_id": device_id,
                "reason": rv,
                "prompt_last_seen": prompt_last_seen or runtime.prompt or "",
                "message": "Device session expired or unreachable — please reconnect.",
            }
        )
        # Registry cleanup — mirror the disconnect path: drop the advisory mirror,
        # the per-socket runtime, the console lock (so a reconnect can re-attach),
        # and this device's discovery ?-walk cache (so a reconnect re-walks fresh).
        _DEVICE_RUNTIMES.pop((token, device_id), None)
        runtimes.pop(device_id, None)
        meta = DEVICES_BY_TOKEN.get(token, {}).get(device_id, {})
        _CONSOLE_LOCKS.pop(meta.get("console_path", ""), None)
        for _ckey in [k for k in _DISCOVERY_WALK_CACHE if k.startswith(f"{device_id}::")]:
            _DISCOVERY_WALK_CACHE.pop(_ckey, None)
        obs.log_event(
            "connection", "session_health", "dead", "released",
            reason=rv, transport=transport, detail=device_id,
        )

    async def _guard_device_io_failure(device_id, runtime, exc) -> bool:
        """Decide whether a device-command failure means the SESSION is dead.

        Called from the command path's exception handlers. Bumps the empty-read
        strike counter (a command that expected output but the channel returned
        nothing is the empty-read death signal) and runs a bounded health probe. If
        the session is confirmed dead, emits the ``session_expired`` notice + releases
        the runtime and returns True so the caller SUPPRESSES its generic error panel
        (no garbled/empty streaming, no misleading "execution failed"). A transient
        error returns False and the caller surfaces the normal error panel unchanged
        (healthy-path behavior preserved).
        """
        if getattr(runtime, "dead", False):
            return True
        runtime.empty_read_strikes += 1
        try:
            result = await probe_session_health(
                runtime.device,
                timeout_s=_KEEPALIVE_PROBE_TIMEOUT_S,
                empty_read_strikes=runtime.empty_read_strikes,
                empty_read_limit=_EMPTY_READ_DEATH_LIMIT,
            )
        except Exception:  # pragma: no cover - probe must never mask the error
            return False
        if result.is_dead:
            await _mark_session_dead(
                device_id, runtime, result.reason, result.prompt_last_seen,
            )
            return True
        return False

    async def _run_session_health_check(device_id, runtime) -> None:
        """Probe one device's session over the DeviceTransport surface and act.

        Alive → (optionally) a silent idle-drop recovery, no user-visible banner.
        Dead  → :func:`_mark_session_dead` (notice + halt + registry release).
        Identical for CML and SSH — only the transport-specific detection
        primitives (``_check_connection`` / ``reattach``) differ, and those live in
        the backends.
        """
        start = time.monotonic()
        transport = _transport_label(runtime.device)
        result = None
        try:
            async with runtime.lock:
                result = await probe_session_health(
                    runtime.device,
                    timeout_s=_KEEPALIVE_PROBE_TIMEOUT_S,
                    empty_read_strikes=runtime.empty_read_strikes,
                    empty_read_limit=_EMPTY_READ_DEATH_LIMIT,
                )
                if result.state is HealthState.ALIVE and result.recovered:
                    # Idle drop recovered by a silent re-attach — resume at the
                    # device's own prompt/mode; reset the empty-read strike count.
                    prompt = runtime.device.current_prompt or runtime.prompt
                    runtime.mode = mode_from_prompt(prompt) if prompt else runtime.mode
                    runtime.prompt = prompt
                    runtime.status = "connected"
                    runtime.empty_read_strikes = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - probe must never crash the loop
            obs.log_event(
                "connection", "session_health", "probe", "error",
                transport=transport, detail=f"{device_id}:{exc}",
            )
            return
        if result is None:
            return
        if result.is_dead:
            await _mark_session_dead(
                device_id, runtime, result.reason, result.prompt_last_seen,
            )
            return
        if result.recovered:
            await websocket.send_json(
                {
                    "type": "device_status",
                    "device_id": device_id,
                    "mode": runtime.mode,
                    "status": "connected",
                    "latency": 0,
                    "prompt": runtime.prompt,
                }
            )
            obs.log_event(
                "connection", "session_health", "alive", "recovered",
                elapsed_ms=(time.monotonic() - start) * 1000.0,
                transport=transport, detail=device_id,
            )
        else:
            obs.log_event(
                "connection", "session_health", "alive", "ok",
                transport=transport, detail=device_id,
            )

    async def _keepalive_loop() -> None:
        """Bug 9 + Session Health Monitor — keep idle consoles warm, RE-ATTACH
        silently on an idle drop, and DETECT/ACT on a session that has truly died.

        A CML console proxy drops the session after a few idle minutes; a lab
        reservation (CML/NetSim) can also be reclaimed while its socket stays
        *temporarily* attachable. Each tick, for every idle device, we run a bounded
        health probe over the transport:
          * healthy → nothing (quietly poked so it never sleeps),
          * idle drop that re-attaches → resume at the same prompt/mode with only a
            quiet ``device_status`` (NEVER ``device_connecting``), and
          * confirmed dead (socket close / keepalive timeout / auth loss / persistent
            empty reads) → a single ``session_expired`` notice, device I/O halted, and
            the runtime released from the registries (see ``_mark_session_dead``).
        Busy devices (holding their lock for a command/help capture) are skipped this
        tick to avoid contending with live I/O; a device already flagged dead is left
        released until the operator reconnects.
        """
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
            for device_id, runtime in list(runtimes.items()):
                if runtime.lock.locked():
                    continue  # device is mid-command; its own path keeps it warm
                if runtime.dead:
                    continue  # already released — wait for an operator reconnect
                await _run_session_health_check(device_id, runtime)

    def _ensure_keepalive() -> None:
        """Start the keepalive loop once, after the first device connects."""
        nonlocal _keepalive_task
        if _keepalive_task is None or _keepalive_task.done():
            _keepalive_task = asyncio.create_task(_keepalive_loop())

    async def connect_device(device_id: str) -> None:
        dev_map = DEVICES_BY_TOKEN.get(token, {})
        meta = dev_map.get(device_id)
        if not meta:
            await websocket.send_json(
                {
                    "type": "error_panel",
                    "device_id": device_id,
                    "errorType": "missing_arg",
                    "what": "Device not loaded",
                    "how": "Load devices for the selected lab before connecting.",
                }
            )
            return

        if device_id in runtimes:
            return

        # Bug J — exclusive console attach. The console is one shared TTY; if a
        # second CLIQ session attaches, the two interleave keystrokes/history.
        # Refuse the second attach instead of corrupting both. Lock keyed by the
        # globally-unique console path so it serializes across users/tokens.
        console_path = meta.get("console_path", "")
        owner = _CONSOLE_LOCKS.get(console_path)
        if owner and owner != token:
            label = meta.get("label", device_id)
            obs.log_event(
                "session", "session_lock", "refused", "in_use",
                detail=label,
            )
            await websocket.send_json(
                {
                    "type": "error_panel",
                    "device_id": device_id,
                    "errorType": "wrong_mode",
                    "what": f"🔒 Device {label} is in use by another session",
                    "how": "Only one console session can attach at a time. Wait "
                    "for the other session to disconnect, then try again.",
                }
            )
            return
        _CONSOLE_LOCKS[console_path] = token
        obs.log_event(
            "session", "session_lock", "acquired", "ok",
            detail=meta.get("label", device_id),
            active=sum(1 for v in _CONSOLE_LOCKS.values() if v == token),
        )

        # Resolve the backend + prior-session memory up front so the reservation
        # gate below can run BEFORE any console auth is attempted.
        connection_type = meta.get("connection_type", "cml")
        log_key = SessionLog.make_key(meta.get("lab_id", ""), device_id)
        prior = SESSION_LOG.load_state(log_key)

        # ── SECURITY: reservation-gate the console RE-ATTACH path ─────────────
        # A terminal-server console socket can stay *physically alive* during the
        # grace period before node teardown, so the re-attach's own console SSH
        # auth ("Authentication (password) successful!") is NOT sufficient on its
        # own — it is a console-layer variant of the cached-token bypass. Before
        # re-attaching we apply the SAME shared reservation policy the CML API
        # gate uses (single source of truth). A verified-EXPIRED reservation is
        # ALWAYS refused; an UNVERIFIABLE one is governed by CLIQ_RESERVATION_POLICY
        # (default 'warn' → proceed on device liveness; 'enforce' → refuse). A
        # refusal happens BEFORE any console auth and never registers the runtime.
        # The SSH terminal-server path (no CML reservation) is untouched.
        if prior and connection_type != "ssh" and session.auth is not None:
            decision = await asyncio.to_thread(
                _reservation_decision, session.auth, detail=device_id,
            )
            lab_id = meta.get("lab_id", "")
            lab_accessible = True
            if decision.allowed and lab_id and hasattr(session.auth, "is_lab_accessible"):
                lab_accessible = await asyncio.to_thread(session.auth.is_lab_accessible, lab_id)

            if not decision.allowed or not lab_accessible:
                _CONSOLE_LOCKS.pop(console_path, None)
                obs.log_event(
                    "connection", "console_reattach", decision.state, "refused",
                    reservation_state=decision.state, policy=decision.policy,
                    gated=True, reason="reservation_not_active", detail=device_id,
                )
                if decision.state == "expired":
                    what = "Reservation expired or inactive — device access denied"
                    how = (
                        "The NetSim reservation for this lab has ended, so CLIQ "
                        "will not re-attach to the console. Re-book or verify on "
                        "the CML controller, then log in again."
                    )
                else:  # unverifiable under the 'enforce' policy
                    what = (
                        "Cannot verify controller session is active — access denied"
                    )
                    how = (
                        "CLIQ could not confirm the controller session is active. "
                        "Check network connectivity to the CML controller and re-authenticate."
                    )
                await websocket.send_json(
                    {
                        "type": "error_panel",
                        "device_id": device_id,
                        "errorType": "wrong_mode",
                        "what": what,
                        "how": how,
                    }
                )
                return

        # Phase 3 latency masking: the SSH console attach below blocks for ~10s.
        # Tell the client immediately so it can show a "Connecting…" state
        # instead of a frozen/blank terminal during the attach. UX only.
        await websocket.send_json(
            {
                "type": "device_connecting",
                "device_id": device_id,
                "label": meta.get("label", device_id),
            }
        )

        # Transport selection: pick the backend by connection type. Both the
        # CML console proxy and the SSH terminal-server console implement the
        # same DeviceTransport surface, so the entire WS loop below (?-help,
        # ghost, reconciliation, learning, pager) runs UNCHANGED over either.
        if connection_type == "ssh":
            device: "CMLConsoleDevice | SSHConsoleDevice" = SSHConsoleDevice(
                host=meta["ssh_host"],
                username=meta["ssh_console_user"],
                password=session.password,
                port_user=meta.get("ssh_console_port", ""),
                device_type=meta.get("ssh_device_type", "cisco_ios"),
            )
        else:
            device = CMLConsoleDevice(
                gateway=session.host,
                username=session.username,
                password=session.password,
                path=meta["console_path"],
            )

        # If CLIQ has prior memory for this device (lab_id/node_id), re-attach to
        # the existing console session and resume in its last-known mode instead
        # of forcing a fresh, normalized connect. The reattach is only reached for
        # a CML session once the reservation gate above confirmed it is active.
        try:
            if prior:
                await asyncio.to_thread(device.reattach)
                if connection_type != "ssh" and session.auth is not None:
                    # The reservation gate above confirmed 'active' before this
                    # console auth ran — record the gated re-attach outcome.
                    obs.log_event(
                        "connection", "console_reattach", "active", "attached",
                        reservation_state="active", gated=True,
                        reason="reservation_active", detail=device_id,
                    )
            else:
                await asyncio.to_thread(device.connect)
        except InteractiveAuthRequired as exc:
            # Bug 7: the console is parked at a login/password prompt. Keep the
            # channel open, remember the context, and ask the UI to collect
            # credentials — DON'T surface a misleading "Connection failed".
            pending_auth[device_id] = {
                "device": device,
                "meta": meta,
                "log_key": log_key,
                "prior": prior,
            }
            obs.log_event(
                "auth", "interactive_auth", exc.stage, "required",
                detail=meta.get("label", device_id),
            )
            await websocket.send_json(
                {
                    "type": "device_auth_required",
                    "device_id": device_id,
                    "label": meta.get("label", device_id),
                    "stage": exc.stage,
                    "message": "Authentication required — enter credentials",
                }
            )
            return
        except Exception as exc:
            _CONSOLE_LOCKS.pop(meta.get("console_path", ""), None)
            # A busy/occupied console line (single-session terminal server) gets
            # a distinct title so the operator knows it is NOT a wrong password.
            busy = isinstance(exc, ConsoleBusyError)
            await websocket.send_json(
                {
                    "type": "error_panel",
                    "device_id": device_id,
                    "errorType": "wrong_mode",
                    "what": "Console in use" if busy else "Connection failed",
                    "how": str(exc),
                }
            )
            return

        await _finalize_connection(device, device_id, meta, log_key, prior)

    async def _finalize_connection(device, device_id, meta, log_key, prior) -> None:
        """Register a freshly-attached (and authenticated) device and tell the UI.

        Shared by the normal connect path and the post-interactive-auth
        completion (Bug 7), so a device that required a login lands in exactly
        the same connected state as one that didn't.
        """
        prompt = device.current_prompt or ""
        if not prior and prompt.strip().endswith("#"):
            # Fresh connect: start in exec mode so the first prompt is R1>.
            # (On reconnect we deliberately keep the device's left-in mode.)
            try:
                await asyncio.to_thread(device.execute, "disable")
                prompt = device.current_prompt or prompt
            except Exception:
                # Keep current prompt if disable is unsupported on a platform.
                pass
        if not prompt.strip():
            prompt = f"{meta.get('label', device_id)}>"
        mode = mode_from_prompt(prompt) if prompt else _parse_mode(prompt)

        # NOTE: the device pager is intentionally LEFT ENABLED. Long outputs page
        # natively with `--More--`, exactly like a real CML console. CLIQ detects
        # the pager and routes raw keys (Space/Enter/q) to the device — see the
        # `pager_key` WS handler and CMLConsoleDevice.execute_paged/pager_send_key.

        # Prefer the device's OWN discovered identity/capability when it has one.
        # The SSH backend reads the real PID from 'show version' on connect
        # (ISR4331/K9 → routing) — that is authoritative and gates VLAN OFF. The
        # CML backend leaves these unset, so we fall back to the node-type map.
        node_type = getattr(device, "node_type", "") or meta.get("type", "")
        dev_capability = getattr(device, "capability", None)
        capability = dev_capability if dev_capability else platform_capabilities(
            meta.get("type", "")
        )
        # Keep the device meta in sync so cache/ghost keying uses the real type.
        if node_type:
            meta["type"] = node_type

        runtimes[device_id] = DeviceRuntime(
            device=device, mode=mode, status="connected", prompt=prompt,
            node_type=node_type,
            capability=capability,
        )
        # Bug P (Ask path) — mirror this runtime into the module-level registry
        # so the REST advisory endpoints can reconcile KB answers against the
        # live device. Same object reference, so it tracks rejected/capability
        # mutations made by the command loop. Ownership-scoped by (token,
        # device_id) so only THIS session can ever read its own runtime.
        _DEVICE_RUNTIMES[(token, device_id)] = runtimes[device_id]

        # Unified connect summary (creds redacted). connection_type distinguishes
        # the CML proxy from the real-HW SSH console; pid/capability_class are
        # populated for SSH from the device's own 'show version' identification.
        _caps = sorted(capability) if capability else []
        obs.log_event(
            "connection", "connect", meta.get("connection_type", "cml"), "success",
            connection_type=meta.get("connection_type", "cml"),
            pid=getattr(device, "pid", "") or "",
            capability_class=(_caps[0] if len(_caps) == 1 else "both"),
            detail=device_id,
        )
        # Persist the resumed state so the next reload can restore it.
        SESSION_LOG.save_state(
            log_key, meta.get("lab_id", ""), device_id,
            hostname=prompt, mode=mode,
        )
        # Part 1 — REHYDRATE from the durable command cache. Commands this
        # device-type previously confirmed (PI) plus this platform's PD commands
        # are seeded back into the ghost index so a captured truth survives a
        # hard refresh/restart and instantly benefits this (and any same-type)
        # device. Best-effort and off the hot path.
        _spawn_bg(_seed_ghost_from_cache(meta.get("type", ""), mode))

        await websocket.send_json(
            {
                "type": "device_status",
                "device_id": device_id,
                "mode": mode,
                "status": "connected",
                "latency": 0,
                "prompt": prompt,
            }
        )
        # Restore prior command history so the user can arrow back through it.
        if prior:
            history = SESSION_LOG.get_history(log_key)
            if history:
                await websocket.send_json(
                    {
                        "type": "history_restore",
                        "device_id": device_id,
                        "history": history,
                    }
                )
        await websocket.send_json(
            {
                "type": "device_output",
                "device_id": device_id,
                "output": "",
                "prompt": prompt,
            }
        )
        # NOTE: No connect-time '?'-priming. A device is NEVER auto-issued a
        # 'show vlan ?' (or any seed query) on connect — that was a device-
        # inappropriate, wasted round-trip (e.g. VLAN help on a router). The
        # VLAN verdict is learned lazily from ACTUAL user input: the per-keystroke
        # help-capture and after-Enter '^' rejection paths populate it on demand.
        # Bug 9 — ensure the idle keepalive loop is running now that at least one
        # device is connected. Starts at most once per WS session.
        _ensure_keepalive()

    async def complete_device_auth(device_id: str, username: str, password: str) -> None:
        """Bug 7: drive a parked console login with operator-supplied creds.

        Credentials arrive over the WS from the masked UI prompt, are used
        in-memory only, and are NEVER persisted or logged. On success the device
        finishes connecting exactly like a non-auth device; on rejection the UI
        is asked to re-enter; on any other failure a real error is surfaced.
        """
        ctx = pending_auth.get(device_id)
        if not ctx:
            return
        device = ctx["device"]
        meta = ctx["meta"]
        try:
            await asyncio.to_thread(device.complete_login, username, password)
        except InteractiveAuthRequired as exc:
            # Wrong credentials — the device re-prompted. Ask the UI again.
            obs.log_event(
                "auth", "interactive_auth", exc.stage, "rejected",
                detail=meta.get("label", device_id),
            )
            await websocket.send_json(
                {
                    "type": "device_auth_required",
                    "device_id": device_id,
                    "label": meta.get("label", device_id),
                    "stage": exc.stage,
                    "message": "Authentication failed — re-enter credentials",
                    "retry": True,
                }
            )
            return
        except Exception as exc:
            pending_auth.pop(device_id, None)
            obs.log_event(
                "auth", "interactive_auth", "error", "error",
                detail=type(exc).__name__,
            )
            await websocket.send_json(
                {
                    "type": "error_panel",
                    "device_id": device_id,
                    "errorType": "wrong_mode",
                    "what": "Connection failed",
                    "how": str(exc),
                }
            )
            return

        # Authenticated — drop the credentials and finish the connection.
        pending_auth.pop(device_id, None)
        obs.log_event(
            "auth", "interactive_auth", "login", "success",
            detail=meta.get("label", device_id),
        )
        await _finalize_connection(
            device, device_id, meta, ctx["log_key"], ctx["prior"],
        )

    def _find_compatible_device(matched_command):
        """Return (device_id, label) of a connected device that supports this
        command, or (None, '')."""
        if not matched_command:
            return None, ""
        platforms = getattr(matched_command, "platforms", [])
        if not platforms:
            return None, ""
        dev_map = DEVICES_BY_TOKEN.get(token, {})
        for dev_id, rt in runtimes.items():
            nt = (rt.node_type or "").lower()
            if any(p.lower() == nt or nt.startswith(p.lower()) for p in platforms):
                return dev_id, dev_map.get(dev_id, {}).get("label", dev_id)
        return None, ""

    def _make_stream_bridge(device_id: str):
        """Build an ``on_chunk`` callback for execute_paged that forwards cleaned
        output deltas to the browser as ``device_stream`` messages.

        The callback runs in the execute_paged worker thread, so it schedules the
        WebSocket send back onto the event loop in a thread-safe way. Best-effort:
        a failed send (e.g. client gone) never breaks command execution. Returns
        ``(on_chunk, state)`` where ``state['streamed']`` is True once any delta
        was emitted — the caller then suppresses the duplicate final body.
        """
        loop = asyncio.get_running_loop()
        state = {"streamed": False}

        def on_chunk(delta: str) -> None:
            if not delta:
                return
            # DISPLAY BOUNDARY GUARD (the single choke point for streamed SSH
            # bytes): strip every control artifact — NUL (^@), ETX (^C) and the
            # other C0 controls a laggy terminal server sprays — so none can ever
            # render on xterm.js. A delayed/empty read that is nothing BUT those
            # artifacts collapses to "" here and is DROPPED (no empty emit, no
            # placeholder). Real output/prompt bytes pass through untouched.
            # No-op for CML (its cleaned deltas never carry control chars).
            delta = _sanitize_console_display(delta)
            if not delta.strip():
                return
            state["streamed"] = True
            # Per-byte diagnostic trace of the device_stream chunk sent to the
            # browser — gated behind CLIQ_TRACE_BYTES (DEBUG), off by default.
            if _TRACE_BYTES:
                log.debug(
                    "WS_SEND device_stream: repr=%r hex=%s",
                    delta, [hex(ord(c)) for c in delta],
                )
            try:
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json(
                        {
                            "type": "device_stream",
                            "device_id": device_id,
                            "chunk": delta,
                        }
                    ),
                    loop,
                )
            except Exception:
                pass

        return on_chunk, state

    async def run_command(device_id: str, command: str, *, raw: bool = False,
                          _confirmed: bool = False) -> None:
        runtime = runtimes.get(device_id)
        if not runtime:
            await websocket.send_json(
                {
                    "type": "error_panel",
                    "device_id": device_id,
                    "errorType": "missing_arg",
                    "what": "Device not connected",
                    "how": "Connect the device first.",
                }
            )
            return

        # Session Health Monitor — refuse to drive a dead session (a race where a
        # command lands just as the keepalive probe confirmed death). Re-surface the
        # clean reconnect notice instead of streaming into a socket that is gone.
        if getattr(runtime, "dead", False):
            await websocket.send_json(
                {
                    "type": "session_expired",
                    "device_id": device_id,
                    "reason": runtime.dead_reason or DeathReason.SOCKET_CLOSE.value,
                    "prompt_last_seen": runtime.prompt or "",
                    "message": "Device session expired or unreachable — please reconnect.",
                }
            )
            return

        # Drive an interactive IOS question (e.g. the one a bare 'configure'
        # poses). Submits via the device's interactive path (bare CR) so a
        # trailing newline can't auto-answer the default, then either surfaces
        # the question and arms `awaiting_input` (device is STILL waiting — do
        # NOT enter config mode) or settles the normal cycle once the device
        # resolves it (mode then reflects reality, e.g. 'config').
        async def _run_interactive(line: str) -> None:
            meta = DEVICES_BY_TOKEN.get(token, {}).get(device_id, {})
            log_key = SessionLog.make_key(meta.get("lab_id", ""), device_id)
            async with runtime.lock:
                t0 = time.perf_counter()
                on_chunk, stream_state = _make_stream_bridge(device_id)
                try:
                    page, state = await asyncio.to_thread(
                        runtime.device.execute_interactive, line, on_chunk
                    )
                except Exception as exc:
                    runtime.awaiting_input = False
                    if await _guard_device_io_failure(device_id, runtime, exc):
                        return
                    await websocket.send_json(
                        {
                            "type": "error_panel", "device_id": device_id,
                            "errorType": "wrong_mode",
                            "what": "Command execution failed", "how": str(exc),
                        }
                    )
                    return
                latency_ms = round((time.perf_counter() - t0) * 1000)
                runtime.empty_read_strikes = 0
                streamed = stream_state["streamed"]
                prompt = runtime.device.current_prompt or ""
                SESSION_LOG.append_command(log_key, line)
                if state == "input":
                    # The device is still waiting on the operator — show its
                    # question inline and keep the line armed. Mode is left
                    # untouched: config mode is NOT entered until the answer
                    # lands (this is the real-device behaviour being restored).
                    runtime.awaiting_input = True
                    await websocket.send_json(
                        {
                            "type": "device_output", "device_id": device_id,
                            "output": "" if streamed else (page or ""),
                            "prompt": "", "awaitingInput": True,
                        }
                    )
                    await websocket.send_json(
                        {
                            "type": "device_status", "device_id": device_id,
                            "mode": runtime.mode, "status": runtime.status,
                            "latency": latency_ms, "prompt": runtime.prompt,
                            "awaitingInput": True,
                        }
                    )
                    return
                # Device answered / settled — resume the normal cycle and
                # reflect the real (possibly now 'config') mode/prompt.
                runtime.awaiting_input = False
                runtime.pager_active = False
                runtime.mode = _parse_mode(prompt)
                runtime.prompt = prompt
                await websocket.send_json(
                    {
                        "type": "device_output", "device_id": device_id,
                        "output": "" if streamed else (page or ""),
                        "prompt": prompt,
                    }
                )
                await websocket.send_json(
                    {
                        "type": "device_status", "device_id": device_id,
                        "mode": runtime.mode, "status": runtime.status,
                        "latency": latency_ms, "prompt": runtime.prompt,
                    }
                )

        # If the device is parked at an interactive question, the engineer's
        # next typed line is the ANSWER (including a bare Enter for the default).
        # Route it back through the interactive path, bypassing the command gate.
        if runtime.awaiting_input and not raw:
            await _run_interactive(command)
            return

        # ── Tier 1 safety: destructive-action confirmation gate ────────────
        # Intercept a destructive command ('no ...', shutdown, clear, erase,
        # reload, write erase, delete, ...) BEFORE it reaches the device. The
        # command is HELD (never forwarded) and a RED y/n confirmation naming
        # the exact object is surfaced; the operator's answer arrives as a
        # separate ``confirm_response`` message. On 'y' this same run_command is
        # re-invoked with ``_confirmed=True`` so the device performs the action.
        # ``raw`` inserts (explicit config-block lines) and the confirmed replay
        # bypass the gate. Advisory-only: CLIQ never auto-executes.
        if not raw and not _confirmed:
            confirmation = confirm_gate.intercept(
                device_id, command, runtime.mode, raw=raw,
            )
            if confirmation is not None:
                obs.log_event(
                    "safety", "destructive_gate", "pre_execute", "held",
                    device_uuid=device_id, mode=runtime.mode,
                    detail=f"HELD destructive command pending y/n: {command!r}",
                )
                await websocket.send_json(
                    {
                        "type": "confirm_required",
                        "device_id": device_id,
                        "command": command,
                        "message": confirmation.message,
                        "ansiMessage": confirmation.ansi_message,
                    }
                )
                return

        # Bug 4: inserted config-block / Insert-button lines arrive with raw=True.
        # They are plain text the engineer chose to run — send them straight to
        # the device WITHOUT the command-recognition gate (no typo/unknown/
        # wrong-mode rejection, no 'Unknown command'). Normal typed commands
        # (raw=False) still go through full validation below.
        if raw:
            cmd = (command or "").strip()
            if not cmd:
                return
            async with runtime.lock:
                t0 = time.perf_counter()
                on_chunk, stream_state = _make_stream_bridge(device_id)
                try:
                    output, more = await asyncio.to_thread(
                        runtime.device.execute_paged, cmd, on_chunk
                    )
                except Exception as exc:
                    if await _guard_device_io_failure(device_id, runtime, exc):
                        return
                    await websocket.send_json(
                        {
                            "type": "error_panel",
                            "device_id": device_id,
                            "errorType": "wrong_mode",
                            "what": "Command execution failed",
                            "how": str(exc),
                        }
                    )
                    return
                latency_ms = round((time.perf_counter() - t0) * 1000)
                runtime.empty_read_strikes = 0
                # Display-boundary guard: strip any control artifact (^@/^C/C0)
                # from the fallback body too, so nothing but real output can be
                # rendered on the non-streamed path. Device-side bytes are
                # untouched (typed==sent preserved).
                output = _sanitize_console_display(output or "")
                # If output already streamed live to the client, send an empty
                # body so it is not rendered twice (the streamed text stands in).
                streamed = stream_state["streamed"]
                _learn_accepted(cmd, runtime.mode, runtime.node_type, output)
                _refine_capability(runtime, cmd, output)
                # Phase A usage analytics: raw=True is an Insert-button / config
                # block (recipe insert). Log the verb only, never the full line.
                obs.log_usage(
                    "recipe_insert",
                    detail=(cmd.split() or ["?"])[0],
                    elapsed_ms=latency_ms,
                )
                meta = DEVICES_BY_TOKEN.get(token, {}).get(device_id, {})
                log_key = SessionLog.make_key(meta.get("lab_id", ""), device_id)
                SESSION_LOG.append_command(log_key, cmd)
                prompt = runtime.device.current_prompt or ""
                if more:
                    runtime.pager_active = True
                    await websocket.send_json(
                        {
                            "type": "device_output", "device_id": device_id,
                            "output": "" if streamed else (output or ""),
                            "prompt": "", "paging": True,
                        }
                    )
                    await websocket.send_json(
                        {
                            "type": "pager_active", "device_id": device_id,
                            "prompt": runtime.prompt, "latency": latency_ms,
                        }
                    )
                    return
                runtime.pager_active = False
                runtime.mode = _parse_mode(prompt)
                runtime.prompt = prompt
                await websocket.send_json(
                    {
                        "type": "device_output", "device_id": device_id,
                        "output": "" if streamed else (output or ""),
                        "prompt": prompt,
                    }
                )
                await websocket.send_json(
                    {
                        "type": "device_status", "device_id": device_id,
                        "mode": runtime.mode, "status": runtime.status,
                        "latency": latency_ms, "prompt": runtime.prompt,
                    }
                )
            return

        # Safety validation before execution: uses legacy engine (fast, 195 commands).
        # NOT used for Q&A (which uses Phase 6 assistant with 1713 KB).
        analysis = _engine.analyze(command, runtime.mode, is_executing=True,
                                   device_type=runtime.node_type)

        # Stable id of the input that produced this analysis. Every warning we
        # emit below carries it so the frontend can MATCH-AND-REPLACE the panel
        # for this exact line instead of appending duplicates.
        input_key = analysis.input_key

        async def send_error(error_type: str, what: str, how: str,
                             fix_command: str | None = None) -> None:
            await websocket.send_json(
                {
                    "type": "error_panel",
                    "device_id": device_id,
                    "errorType": error_type,
                    "what": what,
                    "how": how,
                    "fixCommand": fix_command,
                    "inputKey": input_key,
                }
            )

        meta = DEVICES_BY_TOKEN.get(token, {}).get(device_id, {})
        log_key = SessionLog.make_key(meta.get("lab_id", ""), device_id)

        if analysis.ghost_text:
            await websocket.send_json(
                {
                    "type": "ghost_text",
                    "device_id": device_id,
                    "text": analysis.ghost_text,
                }
            )

        # TODO-1 — DEVICE-AUTHORITATIVE VALIDATION. The legacy engine is fast but
        # only knows ~195 commands; it must NEVER reject a command the live device
        # would accept. So none of the analysis verdicts below BLOCK execution any
        # more: they are surfaced as ADVISORY hints only, then we fall through and
        # let the device be the source of truth (its own '^' / '% Invalid input'
        # is the only authority). Abbreviations (sh, int, bri, conf t) flow through
        # untouched because exec_cmd keeps the raw input for any non-"ok" status.
        # Genuine danger (reload / write erase / clear) is still gated separately
        # by the safety blocklist; that pillar is unchanged.
        obs.log_validation(
            "command", "pre_execute", "passthrough",
            detail=f"{analysis.status} | {(command.split() or ['?'])[0]}",
            device_uuid=device_id,
        )

        if analysis.status == "wrong_device":
            # Advisory: a Cat8000v both routes and switches; capability is
            # rank-only and never blocks. Stay silent and let the device decide.
            pass

        if analysis.status == "wrong_mode_do":
            # Advisory only: hint the 'do' prefix without blocking. The device will
            # reject if truly wrong; many config-mode forms are still valid.
            await send_error(
                "wrong_mode",
                "Tip: use 'do' prefix in config mode",
                f"Try: {analysis.do_suggestion}",
                fix_command=analysis.do_suggestion,
            )

        if analysis.status == "missing_arg":
            e = analysis.explanation
            # Advisory only: surface the hint but still send to the device, which
            # answers '% Incomplete command' authoritatively if it disagrees.
            await send_error(
                "missing_arg",
                e.what if e else "Missing argument",
                e.how if e else "",
            )

        if analysis.status == "invalid_arg":
            # Bug 1 — device is the source of truth. The static arg validator
            # is a fast offline heuristic that sometimes rejects a VALID
            # sub-keyword (e.g. 'show interface switchport'). We must NEVER
            # hard-block here. Run the ACCEPT-only hybrid validator purely for
            # observability + the allowlist fast-path, then fall through to
            # execution: the device's real output (empty = success, or its own
            # '% Invalid input') is the final authority. A genuine rejection is
            # surfaced afterwards by the device-driven 'did you mean?' hint.
            head = (
                analysis.matched_command.full_command
                if analysis.matched_command else ""
            )
            bad_token = analysis.arg_error.provided if analysis.arg_error else ""
            t_val = time.perf_counter()
            decision = classify_subkeyword(head_token(head), bad_token)
            outcome = "accept" if decision.accepted else "passthrough"
            obs.log_validation(
                "subkeyword", "pre_execute", outcome,
                elapsed_ms=(time.perf_counter() - t_val) * 1000.0,
                detail=f"{head} | {bad_token} | {decision.source}",
            )
            # Intentionally NO send_error / return — proceed to execute.

        # Handle unknown or unrecognized commands: ADVISORY ONLY. Never block —
        # the device may well accept it (abbreviations, platform-specific verbs,
        # commands outside the 195-cmd legacy table). The post-execution
        # device-driven 'did you mean?' supplies an accurate hint only if the
        # device itself rejects the line.
        if analysis.status == "unknown":
            pass

        # Bare 'configure' (no 'terminal'/'t' argument) is INTERACTIVE on real
        # IOS: the device asks "Configuring from terminal, memory, or network
        # [terminal]? " and WAITS for an answer. Route it through the interactive
        # path so we pose that question and stay put, instead of auto-answering
        # the default and jumping straight into config mode. 'conf t' (with an
        # argument) is non-interactive and flows through normal execution below.
        _cfg_tok = command.strip().lower()
        if (
            runtime.mode in ("exec", "enable")
            and " " not in command.strip()
            and len(_cfg_tok) >= 4
            and "configure".startswith(_cfg_tok)
        ):
            await _run_interactive(command)
            return

        # Build the command to execute. Always send the RAW input for any non-"ok"
        # status so device-valid abbreviations / commands unknown to the legacy
        # engine reach the device verbatim; expand only confirmed exact matches.
        exec_cmd = analysis.expanded_command + analysis.pipe_suffix if analysis.status == "ok" else command

        # Bug N — input integrity. The device is the source of truth, so what we
        # send it must be EXACTLY what the engineer typed, except for a confirmed
        # exact-match abbreviation expansion (status "ok"). Log typed-vs-sent so
        # any ghost-text/console-interleave corruption is caught: for any non-ok
        # status, sent MUST equal typed. Mismatches are recorded (advisory only).
        obs.log_validation(
            "input", "input_integrity",
            "expanded" if exec_cmd != command else "verbatim",
            device_uuid=device_id, mode=runtime.mode,
            detail=f"typed={command!r} sent={exec_cmd!r}",
        )

        # Serialize the whole execute -> device_output -> device_status cycle for
        # THIS device. Until the previous command's full response (read up to the
        # prompt by live_device._read_until) has been flushed to the client, the
        # next command cannot start. This prevents echoes / CLIQ messages from
        # interleaving out of order with the asynchronous invoke_shell() output.
        async with runtime.lock:
            t0 = time.perf_counter()
            on_chunk, stream_state = _make_stream_bridge(device_id)
            try:
                # Paged execute: returns the first page plus a flag telling us
                # whether the device is now sitting at a `--More--` pager prompt.
                output, more = await asyncio.to_thread(
                    runtime.device.execute_paged, exec_cmd, on_chunk
                )
            except Exception as exc:
                if await _guard_device_io_failure(device_id, runtime, exc):
                    return
                await websocket.send_json(
                    {
                        "type": "error_panel",
                        "device_id": device_id,
                        "errorType": "wrong_mode",
                        "what": "Command execution failed",
                        "how": str(exc),
                    }
                )
                return

            latency_ms = round((time.perf_counter() - t0) * 1000)
            # Healthy read — reset the empty-read death strike counter.
            runtime.empty_read_strikes = 0
            # Display-boundary guard: strip control artifacts (^@/^C/C0) from the
            # fallback body so a delayed/laggy read can never surface a symbol.
            # DISPLAY-side only — device writes are untouched (typed==sent kept).
            output = _sanitize_console_display(output or "")
            # If output already streamed live to the client, suppress the final
            # body so it is not rendered twice (the streamed text stands in).
            streamed = stream_state["streamed"]

            # Accepted-only learning: the device accepted this command unless its
            # response carried an IOS error marker. Learn it for future ghost text.
            _learn_accepted(exec_cmd, runtime.mode, runtime.node_type, output)
            _refine_capability(runtime, exec_cmd, output)
            # Bug P — the device just ACCEPTED this command, so clear any stale
            # rejection so the advisory no longer defers (device is the truth).
            if is_accepted(output):
                runtime.rejected.discard(command.strip().lower())
                runtime.rejected.discard((exec_cmd or "").strip().lower())
                # Part 2 — persist this device-confirmed command to the durable,
                # shared, cross-session cache as a VALUE-STRIPPED TEMPLATE. The
                # raw operands the user typed (interface names, IPs, masks, IDs,
                # WORDs) are replaced with typed placeholders BEFORE storage so
                # the shared corpus is pollution-free by construction and dedupes
                # across users/devices ('...neighbor Gi1' and '...neighbor Te1/0/1'
                # collapse to one '...neighbor <interface>' entry). The DB PRIMARY
                # KEY (template + device_type + platform + mode) makes the insert
                # idempotent — an equivalent entry confirmed by ANY user is shared,
                # never duplicated. Two-tier; PD commands scoped to platform.
                try:
                    cache = _get_cmd_cache()
                    if cache is not None and (exec_cmd or "").strip():
                        tmpl = extract_template(
                            exec_cmd,
                            help_lookup=_template_help_lookup(
                                runtime.node_type, runtime.mode
                            ),
                        )
                        existing = set(cache.known_commands(runtime.node_type, runtime.mode))
                        cache.record_command(
                            tmpl.template, runtime.node_type, runtime.mode
                        )
                        if tmpl.changed:
                            obs.log_event(
                                "learn", "value_strip", "accepted", "ok",
                                store="accepted_commands",
                                command_template=tmpl.template,
                                stripped_args=redact_args(tmpl.stripped),
                                platform=runtime.node_type, mode=runtime.mode,
                                source="device",
                            )
                        obs.log_event(
                            "learn", "db_persist", "accepted",
                            "deduped" if tmpl.template in existing else "inserted",
                            store="accepted_commands",
                            command_template=tmpl.template,
                            platform=runtime.node_type, mode=runtime.mode,
                            source="device", shared=True,
                        )
                        # Keep inline ghost clean: seed the CLEAN keyword prefix
                        # (up to the first placeholder) into the live index so
                        # '<interface>' is never ghosted into the terminal.
                        prefix = clean_prefix(tmpl.template)
                        if prefix and _ghost_index is not None:
                            _ghost_index.add(prefix, tier=2, count=1, live=True)
                        # Two-stage integration: this same device-accepted
                        # template may match a pending product-CR advisory
                        # candidate for this platform — promote it to trusted.
                        kb_store = _get_kb_store()
                        if kb_store is not None:
                            promoted_id = promote_on_device_acceptance(
                                kb_store, exec_cmd, runtime.node_type,
                            )
                            if promoted_id is not None:
                                obs.log_event(
                                    "learn", "candidate_promotion", "accepted", "promoted",
                                    store="commands", command_template=tmpl.template,
                                    platform=runtime.node_type, source="product_cr",
                                )
                except Exception:  # pragma: no cover - cache must never block
                    log.debug("command cache persist skipped", exc_info=True)
            # Phase A usage analytics: command run (verb only, never full line).
            obs.log_usage(
                "command_run",
                detail=(exec_cmd.split() or ["?"])[0],
                elapsed_ms=latency_ms,
            )

            # Feature 2 — sticky protocol context from real CLI activity. An
            # accepted protocol command sets/refreshes the context (source=cli);
            # any other executed command ages it (idle/unrelated-count clears).
            # Mirrored to the client so the "Context: OSPF" chip stays in sync.
            prev_ctx = dict(runtime.active_context) if runtime.active_context else None
            cli_protocol = _protocol_for_command(exec_cmd) if is_accepted(output) else None
            if cli_protocol:
                _set_active_context(runtime, cli_protocol, "cli")
            else:
                _touch_active_context_on_command(runtime, exec_cmd)
            if runtime.active_context != prev_ctx:
                await websocket.send_json({
                    "type": "active_context", "device_id": device_id,
                    "context": runtime.active_context,
                })

            prompt = runtime.device.current_prompt or ""

            if more:
                # Device is paging. Stream this page WITHOUT a prompt and hand
                # control to the pager_key handler for raw Space/Enter/q routing.
                runtime.pager_active = True
                await websocket.send_json(
                    {
                        "type": "device_output",
                        "device_id": device_id,
                        "output": "" if streamed else (output or ""),
                        "prompt": "",
                        "paging": True,
                    }
                )
                await websocket.send_json(
                    {
                        "type": "pager_active",
                        "device_id": device_id,
                        "prompt": runtime.prompt,
                        "latency": latency_ms,
                    }
                )
                SESSION_LOG.append_command(log_key, exec_cmd)
                return

            runtime.pager_active = False
            _prev_mode = runtime.mode
            runtime.mode = _parse_mode(prompt)
            runtime.prompt = prompt
            # Bug O — mode-scoped help is keyed by mode, so an exec/config switch
            # automatically stops the prior mode's '?'-help tokens from completing
            # the new mode (e.g. config 'sh' -> shell vs exec 'sh' -> show). Log
            # the transition for observability.
            if runtime.mode != _prev_mode:
                obs.log_event(
                    "validator", "mode_scope", "transition", "refreshed",
                    device_uuid=device_id, detail=f"{_prev_mode}->{runtime.mode}",
                )

            # Bug 10 (defense-in-depth): if a command unexpectedly parks the
            # device at an interactive login prompt (e.g. a session that aged
            # out mid-flow), do NOT render the raw 'Password:' line as ordinary
            # output or proceed to validate the next keystroke. Surface the
            # secure credential dialog and let the frontend arm AUTH_PASSTHROUGH.
            # The prompt text itself is the only thing emitted; nothing the user
            # types afterward is echoed, logged, or validated.
            auth_stage = looks_like_auth_prompt(output or "")
            if auth_stage:
                obs.log_event(
                    "auth", "auth_passthrough", auth_stage, "prompt_detected",
                    detail=device_id,
                )
                await websocket.send_json(
                    {
                        "type": "device_output",
                        "device_id": device_id,
                        "output": "" if streamed else (output or ""),
                        "prompt": "",
                    }
                )
                await websocket.send_json(
                    {
                        "type": "device_auth_required",
                        "device_id": device_id,
                        "stage": auth_stage,
                    }
                )
                return

            # Bug 10 (defense-in-depth): if a command unexpectedly parks the
            # device at an interactive login prompt (e.g. a session that aged
            # out mid-flow), do NOT render the raw 'Password:' line as ordinary
            # output or proceed to validate the next keystroke. Surface the
            # secure credential dialog and let the frontend arm AUTH_PASSTHROUGH.
            # The prompt text itself is the only thing emitted; nothing the user
            # types afterward is echoed, logged, or validated.
            auth_stage = looks_like_auth_prompt(output or "")
            if auth_stage:
                obs.log_event(
                    "auth", "auth_passthrough", auth_stage, "prompt_detected",
                    detail=device_id,
                )
                await websocket.send_json(
                    {
                        "type": "device_output",
                        "device_id": device_id,
                        "output": "" if streamed else (output or ""),
                        "prompt": "",
                    }
                )
                await websocket.send_json(
                    {
                        "type": "device_auth_required",
                        "device_id": device_id,
                        "stage": auth_stage,
                    }
                )
                return

            # Defensive: remove all trailing prompts, echoed commands, and duplicates.
            output_clean = output or ""
            # Remove repeated trailing prompts
            iterations = 0
            while iterations < 5 and output_clean and prompt:
                stripped = output_clean.rstrip()
                if stripped.endswith(prompt):
                    output_clean = stripped[:-len(prompt)].rstrip()
                    iterations += 1
                else:
                    break
            # Remove echoed command at start if present
            lines = output_clean.split("\n")
            if lines and lines[0].strip() == command.strip():
                lines = lines[1:]
            output_clean = "\n".join(lines).rstrip()

            # Per-byte diagnostic trace of the final device_output body + prompt
            # sent to the browser — gated behind CLIQ_TRACE_BYTES (DEBUG).
            _dbg_out = "" if streamed else output_clean
            if _TRACE_BYTES:
                log.debug(
                    "WS_SEND device_output: streamed=%s output_repr=%r output_hex=%s prompt_repr=%r prompt_hex=%s",
                    streamed, _dbg_out, [hex(ord(c)) for c in _dbg_out],
                    prompt, [hex(ord(c)) for c in prompt],
                )
            await websocket.send_json(
                {
                    "type": "device_output",
                    "device_id": device_id,
                    "output": "" if streamed else output_clean,
                    "prompt": prompt,
                }
            )
            await websocket.send_json(
                {
                    "type": "device_status",
                    "device_id": device_id,
                    "mode": runtime.mode,
                    "status": runtime.status,
                    "latency": latency_ms,
                    "prompt": runtime.prompt,
                }
            )

            # Persist to durable memory: command history + resumed mode so a
            # later reconnect restores both.
            SESSION_LOG.append_command(log_key, exec_cmd)
            SESSION_LOG.save_state(
                log_key, meta.get("lab_id", ""), device_id,
                hostname=runtime.prompt, mode=runtime.mode,
            )

            # Bug 8 — record the device's authoritative VLAN verdict from a real
            # command result too (not only '?'-help): an accepted vlan command
            # proves support. Bug M: NEVER downgrade to 'unsupported' from a
            # rejected after-Enter command — a corrupted/typo'd line (Bug N) or a
            # mode mismatch would falsely brand a VLAN-capable C8000v. Only the
            # authoritative '?'-help verdict may record 'unsupported'.
            if _vlan_family_keyword(command) and is_accepted(output):
                _record_vlan_capability(device_id, True, "after_enter")

            # After-Enter "did you mean?" — when the DEVICE rejected the command
            # (its output carries an error marker), re-query the device's own
            # context-help to surface the closest valid command. Advisory only:
            # we send a live_hint that arms the inline 💡; nothing re-runs.
            # Wrapped so an advisory failure can never regress command output.
            try:
                # Bug I — classify the device response. Only a SYNTAX rejection
                # is eligible for "did you mean?"; an operational '%' message
                # (e.g. '%OSPF: No router process 1') means the command was
                # valid, so we stay silent. response_type drives observability.
                _rtype = response_type(output)
                obs.log_validation(
                    "error_classify", "after_enter", _rtype,
                    response_type=_rtype, device_uuid=device_id,
                    detail=(command.split() or ["?"])[0],
                )
                if is_syntax_error(output):
                    # Bug P — the device just rejected this exact command. Record
                    # it so the Explain/advisory stack defers to the device and
                    # never claims it's "valid for platform X" or suggests a re-run.
                    runtime.rejected.add(command.strip().lower())
                    runtime.rejected.add((exec_cmd or "").strip().lower())
                    # Unified post-Enter advisory — carries the pre-Enter verdict
                    # (mode + intent from `analysis`) into the decision and applies
                    # the STRICT priority order (destructive > mode/context >
                    # abbreviation > incomplete > fuzzy), surfacing only the single
                    # highest-value advisory. Fixes: wrong-mode context carry
                    # (Bug 1), abbreviation expand-and-confirm (Bug 2), and the
                    # self-contradictory do-tip + show->flow suppression (Bug 3).
                    advisory = await asyncio.to_thread(
                        route_post_enter_advisory,
                        command,
                        output or "",
                        runtime.mode,
                        analysis,
                        lambda pfx: _device_help_text(runtime.device, pfx),
                        prompt,
                        _expand_abbrev_line,
                    )
                    if advisory is not None:
                        obs.log_validation(
                            "advisory_tier", "after_enter", advisory.tier,
                            detail=f"tier={advisory.tier} "
                            f"prio={advisory.priority} | {(command or '')[:60]}",
                        )
                        hint_kind = advisory.kind
                        hint_msg = advisory.message
                        hint_fix = advisory.fix
                        # Fuzzy tier only: run the device-driven fix back through
                        # the model engine so EVERY mistaken token is corrected
                        # (the caret only fixes one), e.g. 'do shw valn' ->
                        # 'do show vlan'. Higher tiers are already authoritative.
                        if (
                            advisory.tier == "fuzzy"
                            and hint_kind == "typo"
                            and hint_fix
                        ):
                            polished = _valid_command_fix(hint_fix, runtime.mode)
                            if polished:
                                hint_fix = polished
                                hint_msg = f"Did you mean: {polished}?"
                        hint_payload: dict[str, Any] = {
                            "kind": hint_kind,
                            "message": hint_msg,
                            "fix": hint_fix,
                        }
                        # Destructive confirmations carry a terminal-red variant
                        # + danger flag so the client can render it in ANSI red.
                        if advisory.ansi_message:
                            hint_payload["ansiMessage"] = advisory.ansi_message
                        if advisory.danger:
                            hint_payload["danger"] = True
                        await websocket.send_json(
                            {
                                "type": "live_hint",
                                "device_id": device_id,
                                "afterEnter": True,
                                "command": hint_fix or command,
                                "hint": hint_payload,
                            }
                        )
                        # Bug 5 (cross-cutting observability): a capability-driven
                        # suggestion also surfaces a persistent Advisory card so
                        # the mismatch is visible beyond the inline 💡. `quiet`
                        # keeps it from writing a duplicate prompt line.
                        if hint_kind == "wrong_device":
                            alt_fix = _first_alternative(advisory.message)
                            await websocket.send_json(
                                {
                                    "type": "error_panel",
                                    "device_id": device_id,
                                    "errorType": "wrong_device",
                                    "what": advisory.message.split(".")[0],
                                    "how": advisory.message,
                                    "fixCommand": alt_fix,
                                    "quiet": True,
                                }
                            )
                            obs.log_validation(
                                "vlan_suggest", "after_enter", "shown",
                                detail=(command or "")[:80],
                            )
            except Exception:  # pragma: no cover - advisory must never crash
                log.debug("did_you_mean (after-Enter) skipped", exc_info=True)


    async def resolve_confirmation(device_id: str, answer: str) -> None:
        """Resolve the operator's y/n answer to a held destructive command.

        Consulted when a ``confirm_response`` keystroke arrives while a command
        is held by :data:`confirm_gate`:

          * 'y' — FORWARD: re-run the held command with ``_confirmed=True`` so it
            reaches the device (the device performs the action).
          * 'n' — DISCARD: drop the held command; it is NEVER sent. Redraw the
            prompt cleanly.
          * any other key / bare Enter — REPROMPT: keep the command held and
            re-render the red confirmation.
        """
        outcome, held, reprompt = confirm_gate.resolve(device_id, answer)
        if held is None:
            # Nothing was held (stale/duplicate response) — ignore.
            return
        runtime = runtimes.get(device_id)
        if outcome == CONFIRM_FORWARD:
            obs.log_event(
                "safety", "destructive_gate", "response", "forwarded",
                device_uuid=device_id,
                detail=f"answer=y — releasing held command to device: {held.command!r}",
            )
            await run_command(
                device_id, held.command, raw=held.raw, _confirmed=True,
            )
            return
        if outcome == CONFIRM_DISCARD:
            obs.log_event(
                "safety", "destructive_gate", "response", "discarded",
                device_uuid=device_id,
                detail=f"answer=n — held command discarded, NOT sent: {held.command!r}",
            )
            await websocket.send_json(
                {
                    "type": "confirm_resolved",
                    "device_id": device_id,
                    "outcome": "discarded",
                    "command": held.command,
                    "prompt": runtime.prompt if runtime else "",
                }
            )
            return
        # CONFIRM_REPROMPT — any other key / bare Enter. Keep the command held.
        obs.log_event(
            "safety", "destructive_gate", "response", "reprompt",
            device_uuid=device_id,
            detail=f"answer={answer!r} — not y/n, re-prompting; command still held: {held.command!r}",
        )
        await websocket.send_json(
            {
                "type": "confirm_required",
                "device_id": device_id,
                "command": held.command,
                "message": reprompt.message if reprompt else "",
                "ansiMessage": reprompt.ansi_message if reprompt else None,
                "reprompt": True,
            }
        )


    try:
        while True:
            message = await websocket.receive_json()
            raw_msg_type = message.get("type")
            msg_type = _normalize_ws_msg_type(raw_msg_type)
            device_id = message.get("device_id", "")

            if msg_type == "connect_device":
                await connect_device(device_id)
            elif msg_type == "auth_credentials":
                # Bug 7: masked credentials from the UI for a device parked at an
                # interactive console login. Used in-memory only — never echoed
                # back, persisted, or logged.
                await complete_device_auth(
                    device_id,
                    str(message.get("username", "")),
                    str(message.get("password", "")),
                )
            elif msg_type == "ghost_accept":
                # Phase A usage analytics (local-only, Phase H seed). Additive:
                # the frontend may emit this when the user accepts ghost text via
                # Tab/→. Verb only — never the full accepted line. Harmless if the
                # frontend never sends it.
                obs.log_usage(
                    "ghost_accept",
                    detail=(str(message.get("text", "")).split() or ["?"])[0],
                )
            elif msg_type == "suggest":
                # Real-time keystroke suggestions: tiered prefix index (KB ->
                # document -> learned) with the legacy engine as an abbreviation
                # fallback. NOT semantic search — fast in-memory prefix lookup.
                partial = message.get("partial", "")
                # Bug 4a — client keystroke sequence. Echoed back on ghost_text /
                # live_hint / clear_warnings so the frontend can DISCARD a stale
                # response that lands after a newer keystroke (in-flight cancel).
                suggest_seq = message.get("seq")
                runtime = runtimes.get(device_id)
                if runtime and runtime.pager_active:
                    # Device is paging — suppress ghost/suggest entirely; keys are
                    # routed to the pager (see pager_key handler).
                    continue
                if runtime and partial.strip():
                    analysis = _engine.analyze(
                        partial, runtime.mode, is_executing=True,
                        device_type=runtime.node_type,
                    )
                    # Tiered ghost text: device '?'-help tokens (Tier 1,
                    # authoritative for the live device) first, then the index
                    # (KB -> RAG/PDF -> learned), then the legacy engine ghost as
                    # an abbreviation fallback so 'sh ip ...' style input still
                    # completes.
                    # Bug 3 — never ghost-complete a 'do ...' line in exec/enable
                    # mode: 'do' is invalid there, so offering continuations
                    # (e.g. "do sh|ow ...") would coach an invalid command.
                    if _is_exec_mode_do(runtime.mode, partial):
                        ghost = ""
                    else:
                        # Feature 2 — bias (not filter) the index toward the
                        # sticky protocol context. Only re-orders candidates;
                        # unrelated commands (e.g. 'show version') still complete.
                        bias_protocol = _active_context_protocol(runtime)
                        ghost = (
                            _ghost_complete(partial, device_id, runtime.mode, runtime.node_type, bias_protocol)
                            or (analysis.ghost_text or "")
                        )
                    await websocket.send_json(
                        {
                            "type": "ghost_text",
                            "device_id": device_id,
                            "text": ghost,
                            "seq": suggest_seq,
                        }
                    )
                    # The input line changed: any executed-command warnings now
                    # describe a PRIOR input and are stale. Tell the client to
                    # drop them so the panel only ever reflects current state.
                    await websocket.send_json(
                        {
                            "type": "clear_warnings",
                            "device_id": device_id,
                            "inputKey": analysis.input_key,
                            "seq": suggest_seq,
                        }
                    )
                    # Live "did you mean" hint while typing (overlaid on terminal)
                    hint = _build_live_hint(analysis)
                    # Bug 8 — on-type VLAN-unsupported advisory. The DEVICE is the
                    # source of truth: we only hint when a PRIOR 'show vlan ?'
                    # capture (or rejection) on THIS device proved it lacks VLANs.
                    # The verdict is cached per device, so this is an O(1) dict
                    # lookup on every keystroke — no re-querying 'show vlan ?'.
                    # Capable devices (C8k/cat9000v/nxos9000) get a True verdict
                    # from prewarm, so this never false-fires there.
                    if (
                        _vlan_family_keyword(partial)
                        and _vlan_capability.get(device_id) is False
                    ):
                        cap_hint = build_capability_hint("vlan")
                        if cap_hint is not None:
                            hint = {
                                "kind": cap_hint.kind,
                                "message": cap_hint.message,
                                "fix": cap_hint.fix,
                            }
                            obs.log_validation(
                                "vlan_capability_cache", "on_type", "shown",
                                device_uuid=device_id, mode=runtime.mode,
                                detail=(partial or "").strip()[:80],
                            )
                    if hint and hint.get("kind") == "wrong_device":
                        tid, tlabel = _find_compatible_device(analysis.matched_command)
                        if tid:
                            hint["fixDeviceId"] = tid
                            hint["fixDeviceLabel"] = tlabel
                            hint["message"] = f"{hint['message']}  Run on {tlabel}?"
                    # Bug 6 (cross-cutting observability): record the proactive
                    # config-mode 'do' hint so the demo can show it fired on-type.
                    if hint and hint.get("message") == _CONFIG_DO_HINT_MSG:
                        obs.log_validation(
                            "do_hint", "on_type", "shown",
                            detail=(partial or "").strip()[:80],
                        )
                    await websocket.send_json(
                        {
                            "type": "live_hint",
                            "device_id": device_id,
                            "hint": hint,
                            "inputKey": analysis.input_key,
                            "seq": suggest_seq,
                        }
                    )
                    # Bug K — observability: record that a mid-type typo match was
                    # suppressed (we never fire corrections before Enter). Proves
                    # the gate held without affecting the command path.
                    if analysis.status == "typo" and hint is None:
                        obs.log_validation(
                            "suggest_match", "on_type", "suppressed",
                            detail=(partial or "").strip()[:80],
                        )
                elif runtime:
                    # Empty input: clear any live hint and ghost text.
                    await websocket.send_json(
                        {"type": "ghost_text", "device_id": device_id, "text": "", "seq": suggest_seq}
                    )
                    await websocket.send_json(
                        {"type": "live_hint", "device_id": device_id, "hint": None, "seq": suggest_seq}
                    )
                    await websocket.send_json(
                        {"type": "clear_warnings", "device_id": device_id, "inputKey": ""}
                    )
            elif msg_type == "client_event":
                # Cross-cutting observability: the frontend forwards render-order
                # (and similar UI) decisions so they land in the unified
                # structured log alongside the backend layers. Advisory only --
                # never affects the device/command path.
                try:
                    obs.log_event(
                        str(message.get("layer", "renderer")),
                        str(message.get("op", "render_order")),
                        "client",
                        str(message.get("outcome", "")),
                        detail=str(message.get("detail", "") or device_id),
                    )
                except Exception:  # pragma: no cover - logging must never break
                    pass
            elif msg_type == "ping":
                # In-session reconnect heartbeat. The client pings periodically so
                # it can detect a silently half-dead browser socket (one that
                # stops delivering bytes without firing onclose) and freeze input
                # + show the reconnect banner BEFORE the operator types into a
                # dead session. We echo a pong carrying the client's timestamp so
                # it can clear its pong-timeout. Pure liveness — no device I/O.
                await websocket.send_json({"type": "pong", "ts": message.get("ts")})
            elif msg_type in {"command", "run_fix"}:
                await run_command(
                    device_id,
                    message.get("command", ""),
                    raw=bool(message.get("raw", False)),
                )

            elif msg_type == "confirm_response":
                # Operator's y/n answer to a held destructive command. 'y' releases
                # it to the device, 'n' discards it, anything else re-prompts.
                await resolve_confirmation(
                    device_id, str(message.get("answer", "")),
                )

            elif msg_type == "pager_key":
                # Native --More-- paging: route a single raw key to the device.
                # CML standard: space = next page, enter = next line, q = quit.
                # Tab is ALSO a quit shortcut (convenience): it exits paging just
                # like q, so the engineer can bail out of a long output fast.
                runtime = runtimes.get(device_id)
                if not runtime or not runtime.pager_active:
                    continue
                logical = str(message.get("key", "space")).strip().lower()
                raw_key = {
                    "space": " ", " ": " ",
                    "enter": "\r", "return": "\r", "\r": "\r", "\n": "\r",
                    "q": "q", "quit": "q", "tab": "q", "\t": "q",
                }.get(logical, " ")

                # ── Streaming '?'-help pager (Priority 1+2) ────────────────
                # Help paging streams device_help pages; tokens from each page
                # are merged into the ghost cache so completion keeps improving.
                if runtime.help_paging:
                    async with runtime.lock:
                        try:
                            page, more = await asyncio.to_thread(
                                runtime.device.context_help_more, raw_key,
                            )
                        except Exception as exc:
                            runtime.help_paging = False
                            runtime.pager_active = False
                            log.warning("help pager error: %s", exc)
                            await websocket.send_json({
                                "type": "device_help", "device_id": device_id,
                                "partial": runtime.help_partial, "output": "",
                                "more": False, "prompt": runtime.prompt,
                            })
                            continue
                        _cache_device_help(
                            device_id, runtime.help_partial, page, extend=True,
                            mode=runtime.mode, node_type=runtime.node_type,
                        )
                        runtime.help_paging = more
                        runtime.pager_active = more
                    await websocket.send_json({
                        "type": "device_help", "device_id": device_id,
                        "partial": runtime.help_partial, "output": page,
                        "more": more, "prompt": runtime.prompt,
                    })
                    continue

                async with runtime.lock:
                    t0 = time.perf_counter()
                    try:
                        page, more = await asyncio.to_thread(
                            runtime.device.pager_send_key, raw_key
                        )
                    except Exception as exc:
                        runtime.pager_active = False
                        await websocket.send_json({
                            "type": "pager_done", "device_id": device_id,
                            "prompt": runtime.prompt, "mode": runtime.mode,
                        })
                        log.warning("pager_key error: %s", exc)
                        continue
                    latency_ms = round((time.perf_counter() - t0) * 1000)
                    prompt = runtime.device.current_prompt or runtime.prompt

                    if more:
                        await websocket.send_json({
                            "type": "device_output", "device_id": device_id,
                            "output": page or "", "prompt": "", "paging": True,
                        })
                        continue

                    # Pager finished: flush the last page and restore the prompt.
                    runtime.pager_active = False
                    runtime.mode = _parse_mode(prompt)
                    runtime.prompt = prompt
                    await websocket.send_json({
                        "type": "device_output", "device_id": device_id,
                        "output": page or "", "prompt": prompt,
                    })
                    await websocket.send_json({
                        "type": "pager_done", "device_id": device_id,
                        "prompt": prompt, "mode": runtime.mode,
                    })
                    await websocket.send_json({
                        "type": "device_status", "device_id": device_id,
                        "mode": runtime.mode, "status": runtime.status,
                        "latency": latency_ms, "prompt": runtime.prompt,
                    })

            elif msg_type == "ask":
                # Normalize question: strip leading/trailing whitespace and ensure no residual '?'.
                raw_question = message.get("question", "")
                question = raw_question.strip().lstrip('?').strip()
                if not question:
                    log.warning("ask handler received empty question after normalization: raw=%r", raw_question)
                    continue
                _init_advisory_stack()
                runtime = runtimes.get(device_id)
                # Use the live device's platform/mode as SOFT hints only. They
                # must never hard-eliminate Q&A results (a iosv device must still
                # see iosxe-tagged IOS commands). See _qa_respond for the
                # family-aware soft-hint + retry-without-filter logic.
                platform_raw = message.get("platform")
                mode_raw = message.get("mode")
                platform = (
                    platform_raw.strip() if isinstance(platform_raw, str) and platform_raw.strip()
                    else (runtime.node_type if runtime else None)
                )
                mode = (
                    mode_raw.strip() if isinstance(mode_raw, str) and mode_raw.strip()
                    else (runtime.mode if runtime else None)
                )
                # Phase 5 — scenario-based advisory. Detect troubleshooting intent
                # (conservative) and build a capability/mode-gated toolkit. Static
                # in-memory lookup — no embedding call on this path. Sits at the
                # BOTTOM of the hierarchy (defers to live-device rejections).
                toolkit = _build_toolkit_advisory(question, runtime, mode)
                # Feature 3 — command discovery / reference (checked separately;
                # async ?-walk + learned + KB merge with source attribution).
                reference = await _build_discovery_reference(question, runtime, mode, device_id)
                # Feature 2 — set the sticky protocol context when intent fires
                # (advisory source), and mirror it to the client so the panel can
                # render the "Context: OSPF" chip. Discovery counts as intent too.
                ctx_protocol = None
                if toolkit:
                    ctx_protocol = toolkit.get("protocol")
                elif reference:
                    ctx_protocol = reference.get("protocol")
                if ctx_protocol and runtime is not None:
                    _set_active_context(runtime, ctx_protocol, "advisory")
                    await websocket.send_json({
                        "type": "active_context", "device_id": device_id,
                        "context": runtime.active_context,
                    })
                # SAFEGUARD: Q&A routed to Phase 6 assistant (1713 KB), NOT legacy engine (195 commands).
                log.debug(
                    "WS ask (Phase 6 Q&A): question=%r platform_hint=%s mode_hint=%s assistant_ready=%s kb_path=%s",
                    question,
                    platform,
                    mode,
                    _assistant is not None,
                    _KB_DB,
                )
                if _assistant is not None:
                    try:
                        resp, annotated = await _qa_respond(question, platform, mode)
                        log.info(
                            "WS ask handler: tier=%s results=%d groups=%d annotated=%s query=%r",
                            resp.response_tier, len(resp.results), len(resp.grouped_results), annotated, question,
                        )
                        answer_message = resp.message
                        if annotated and (resp.results or resp.grouped_results):
                            answer_message = f"{resp.message} (from iosxe reference — verify on your platform)"
                        # Bug 3: prefer a curated recipe for known procedures.
                        # Fall back to KB assembly (labelled UNVERIFIED) only when
                        # no curated recipe matches the question.
                        sequence: list[dict[str, Any]] = []
                        if _is_setup_query(question):
                            curated = _resolve_recipe(question, platform)
                            if curated:
                                sequence = curated
                                answer_message = (
                                    f"{answer_message}  (curated, verified procedure)"
                                )
                            else:
                                sequence = _build_sequence(resp)
                                if sequence:
                                    answer_message = (
                                        f"{answer_message}  (assembled from KB — "
                                        f"UNVERIFIED, review each step before running)"
                                    )
                        await websocket.send_json({
                            "type": "intel_answer", "device_id": device_id,
                            "question": question, "tier": resp.response_tier,
                            "message": answer_message,
                            "results": [_serialize_qa_result(r) for r in resp.results],
                            "groups": [
                                {
                                    "label": g.label,
                                    "results": [_serialize_qa_result(r) for r in g.results],
                                }
                                for g in resp.grouped_results
                            ],
                            "sequence": sequence,
                            "toolkit": toolkit,
                            "reference": reference,
                        })
                    except Exception as exc:
                        log.error("WS ask error: %s", exc)
                        await websocket.send_json({
                            "type": "intel_answer", "device_id": device_id,
                            "question": question, "tier": "fallback",
                            "message": "Could not retrieve an answer. Try rephrasing.",
                            "results": [], "groups": [],
                            "toolkit": toolkit,
                            "reference": reference,
                        })
                else:
                    await websocket.send_json({
                        "type": "intel_answer", "device_id": device_id,
                        "question": question, "tier": "fallback",
                        "message": "Knowledge base not loaded. Advisory answers unavailable.",
                        "results": [], "groups": [],
                        "toolkit": toolkit,
                        "reference": reference,
                    })
                if _memory_store is not None:
                    try:
                        resolutions = await asyncio.to_thread(
                            _memory_store.recall_similar, question, 3, 0.4, platform,
                        )
                        if resolutions:
                            await websocket.send_json({
                                "type": "intel_memory", "device_id": device_id,
                                "trigger": "ask",
                                "resolutions": [_serialize_resolution(pr) for pr in resolutions],
                            })
                    except Exception as exc:
                        log.warning("Memory recall after ask failed: %s", exc)

            elif msg_type == "clear_context":
                # Feature 2 — engineer explicitly cleared the sticky context chip.
                runtime = runtimes.get(device_id)
                if runtime is not None:
                    _clear_active_context(runtime, reason="explicit")
                    await websocket.send_json({
                        "type": "active_context", "device_id": device_id,
                        "context": None,
                    })

            elif msg_type == "set_context":
                # Feature 2 — engineer picked/changed the protocol from the chip.
                protocol = str(message.get("protocol", "")).strip().lower()
                runtime = runtimes.get(device_id)
                if runtime is not None:
                    if protocol and get_toolkit(protocol) is not None:
                        _set_active_context(runtime, protocol, "advisory")
                    else:
                        _clear_active_context(runtime, reason="explicit")
                    await websocket.send_json({
                        "type": "active_context", "device_id": device_id,
                        "context": runtime.active_context,
                    })

            elif msg_type == "recall_memory":
                query = message.get("query", "").strip()
                if not query:
                    continue
                _init_advisory_stack()
                runtime = runtimes.get(device_id)
                platform = runtime.node_type if runtime else None
                if _memory_store is not None:
                    try:
                        resolutions = await asyncio.to_thread(
                            _memory_store.recall_similar, query, 5, 0.35, platform,
                        )
                        await websocket.send_json({
                            "type": "intel_memory", "device_id": device_id,
                            "trigger": "recall",
                            "resolutions": [_serialize_resolution(pr) for pr in resolutions],
                        })
                    except Exception as exc:
                        log.warning("recall_memory WS error: %s", exc)

            elif msg_type == "mark_resolved":
                issue_text = message.get("issue_text", "").strip()
                commands = message.get("commands", [])
                platform = message.get("platform", "iosxe")
                device_name = message.get("device_name") or None
                notes = message.get("outcome_notes") or None
                if not issue_text:
                    continue
                _init_advisory_stack()
                if _memory_store is not None:
                    try:
                        memory_id = await asyncio.to_thread(
                            _memory_store.store_memory,
                            issue_text,
                            commands if isinstance(commands, list) else [],
                            platform, device_name, "resolved", notes, "cross_device",
                        )
                        await websocket.send_json({
                            "type": "intel_resolve_ack", "device_id": device_id,
                            "memory_id": memory_id, "issue_text": issue_text,
                        })
                    except Exception as exc:
                        log.error("mark_resolved WS error: %s", exc)

            elif msg_type == "explain":
                # VS Code-style 'Explain' action: advisory plain-language summary.
                command = (message.get("command", "") or "").strip()
                if not command:
                    continue
                runtime = runtimes.get(device_id)
                platform = message.get("platform") or (runtime.node_type if runtime else None)
                mode = message.get("mode") or (runtime.mode if runtime else None)
                try:
                    # Bug P — defer to the device if it just rejected this command.
                    rejected = bool(runtime and command.strip().lower() in runtime.rejected)
                    info = await asyncio.to_thread(
                        _explain_command, command, platform, mode,
                        device_rejected=rejected,
                    )
                    await websocket.send_json({
                        "type": "intel_explain", "device_id": device_id, **info,
                    })
                except Exception as exc:
                    log.warning("explain WS error: %s", exc)

            elif msg_type == "device_help":
                # Device-native context help: run 'partial?' on the device and
                # stream the device's OWN help output back (passthrough, not Q&A).
                #
                # Priority 1: do NOT blanket-rstrip the partial. The trailing
                # whitespace is the engineer's intent signal ('show ?' lists the
                # subcommands; 'show?' completes the token). We collapse internal
                # whitespace runs but PRESERVE a single trailing space so
                # 'show ?' and 'show   ?' behave identically. context_help_start()
                # builds the final canonical request from this.
                raw_partial = message.get("partial", "") or ""
                _collapsed = re.sub(r"\s+", " ", raw_partial)
                _trailing_ws = raw_partial != raw_partial.rstrip()
                partial = _collapsed.rstrip() + (" " if _trailing_ws else "")
                runtime = runtimes.get(device_id)
                if runtime and not getattr(runtime, "dead", False) and partial.strip():
                    async with runtime.lock:
                        try:
                            # Priority 2: CML-style streaming. Capture only the
                            # FIRST page and return immediately — never block ~27s
                            # auto-paging the whole list. ``more`` tells us the
                            # device is sitting at a '--More--' prompt.
                            help_text, more = await asyncio.to_thread(
                                runtime.device.context_help_start, partial,
                            )
                        except Exception as exc:
                            log.warning("device_help error: %s", exc)
                            help_text, more = "", False
                        # An invalid '?'-help path makes the device answer with a
                        # '%'-prefixed error instead of an options list. Surface
                        # it faithfully (like CML) and do NOT feed it to the ghost
                        # engine or learn it. Errors never page, so force more=off.
                        help_error = is_help_error(help_text)
                        suggestion = None
                        if help_error:
                            more = False
                            # Bonus (advisory): offer the closest valid option
                            # from the last valid prefix's own '?'-help. Best
                            # effort — never blocks the faithful error render.
                            try:
                                suggestion = await asyncio.to_thread(
                                    _did_you_mean,
                                    partial.strip(),
                                    help_text,
                                    lambda pfx: _device_help_text(runtime.device, pfx),
                                    "",
                                )
                            except Exception:  # pragma: no cover - advisory only
                                log.debug("device_help did_you_mean skipped", exc_info=True)
                        else:
                            # Priority 1: cache THIS page's tokens immediately so
                            # the engineer's next keystrokes ghost-complete from
                            # the live device help before the full list is
                            # captured (handoff).
                            _cache_device_help(device_id, partial, help_text, mode=runtime.mode, node_type=runtime.node_type)
                        # Bug 8 — the device just told us, authoritatively,
                        # whether it understands the vlan family. Bug 11: use the
                        # conservative verdict so NX-OS (valid but differently
                        # formatted 'show vlan ?' output) is never mis-flagged as
                        # unsupported. Only a '%'-error records 'unsupported'.
                        if _vlan_family_keyword(partial):
                            verdict = _vlan_help_verdict(help_text)
                            if verdict is not None:
                                _record_vlan_capability(device_id, verdict, "device_help")
                        runtime.help_partial = partial
                        runtime.help_paging = more
                        runtime.pager_active = more
                    await websocket.send_json({
                        "type": "device_help", "device_id": device_id,
                        "partial": partial, "output": help_text,
                        "more": more, "isError": help_error,
                        "prompt": runtime.prompt,
                        # Bug 4 — explicit help-complete signal. The frontend uses
                        # this (final page, more=False) as the SOLE trigger to
                        # redraw the prompt: it writes the help body then redraws
                        # the prompt inside xterm's write-completion callback. For
                        # multi-page NX-OS help (more=True) helpComplete stays
                        # False so the prompt is held until the last page lands.
                        "helpComplete": not more,
                    })
                    obs.log_event(
                        "renderer", "render_order", "help_emitted",
                        "complete" if not more else "more_pending",
                        detail=f"{device_id}:{(partial or '').strip()[:60]}",
                    )
                    if help_error and suggestion is not None:
                        help_hint_kind = getattr(suggestion, "kind", "typo")
                        # Same second-pass polish as the after-Enter path: fully
                        # correct every mistaken token, not just the caret one.
                        help_msg = suggestion.message
                        help_fix = suggestion.fix
                        if help_hint_kind == "typo" and help_fix:
                            polished = _valid_command_fix(help_fix, runtime.mode)
                            if polished:
                                help_fix = polished
                                help_msg = f"Did you mean: {polished}?"
                        await websocket.send_json({
                            "type": "live_hint", "device_id": device_id,
                            "afterEnter": True,
                            "command": help_fix or partial.strip(),
                            "hint": {
                                "kind": help_hint_kind,
                                "message": help_msg,
                                "fix": help_fix,
                            },
                        })
                        if help_hint_kind == "wrong_device":
                            obs.log_validation(
                                "capability_suggest", "device_help", "shown",
                                detail=(partial or "").strip()[:80],
                            )

            else:
                log.warning(
                    "Unsupported WS message type: raw=%r normalized=%r keys=%s",
                    raw_msg_type,
                    msg_type,
                    list(message.keys()),
                )
                await websocket.send_json({
                    "type": "error_panel", "device_id": device_id,
                    "errorType": "missing_arg", "what": "Unsupported message",
                    "how": f"Unknown type: {msg_type}",
                })
    except WebSocketDisconnect:

        pass
    finally:
        # Phase A usage analytics (local-only, Phase H seed): session end.
        obs.log_usage("session_end", devices=len(runtimes))
        # Bug 9 — stop the keepalive loop; Bug 8 — cancel any in-flight prewarm.
        if _keepalive_task is not None:
            _keepalive_task.cancel()
        for task in list(_bg_tasks):
            task.cancel()
        for runtime in runtimes.values():
            try:
                await asyncio.to_thread(runtime.device.disconnect)
            except Exception:
                pass
        # Bug P (Ask path) — drop this socket's devices from the module-level
        # advisory registry so a stale runtime is never reconciled against.
        # Bug J — release every console lock this session held so another
        # session can attach immediately after we drop off.
        # In-session reconnect — RETAIN the session (token + device registry +
        # held creds) and arm the grace countdown so a reconnect resumes with no
        # re-login; the grace reaper wipes it if no reconnect arrives in time.
        _release_socket_registrations(token)


def _release_socket_registrations(token: str) -> None:
    """Release a dropped socket's transient device registrations, then decide
    whether to retain the session for in-session reconnect.

    Pops every runtime + console lock owned by ``token`` (so the console frees
    for a fresh attach and no stale runtime is reconciled against). SSH sessions
    hold their console password in-memory for the live session; to support
    reconnect WITHOUT a re-login we do NOT drop the session here — instead we
    RETAIN it and arm the grace countdown (:data:`_SESSION_LAST_SEEN`). A
    reconnect under the same token resumes it as-is; the grace reaper wipes it
    (and the password) if no reconnect arrives within the window. CML sessions
    are already retained (SESSIONS is never popped for them), so both transports
    reconnect without re-authenticating.

    Split out of :func:`ws_terminal`'s ``finally`` so the drop bookkeeping is
    unit-testable without the TestClient portal deferring the disconnect.
    """
    for key in [k for k in _DEVICE_RUNTIMES if k[0] == token]:
        _DEVICE_RUNTIMES.pop(key, None)
    for path, owner in list(_CONSOLE_LOCKS.items()):
        if owner == token:
            _CONSOLE_LOCKS.pop(path, None)
    _sess = SESSIONS.get(token)
    if _sess is not None and getattr(_sess, "connection_type", "cml") == "ssh":
        _SESSION_LAST_SEEN[token] = time.monotonic()
        obs.log_event(
            "connection", "socket_drop", "websocket", "retained",
            connection_type="ssh", grace_ttl_s=_SESSION_GRACE_TTL_S,
            detail=token[:8],
        )


# ── Single-port static hosting ────────────────────────────────────────────────
# Serve the built React/xterm frontend (`frontend/dist`) so the whole app — page
# + API + WebSocket — is reachable from ONE process on ONE port. Registered LAST
# so every `/api/*`, `/ws`, and `/health` route above is matched FIRST and can
# never be shadowed by the SPA catch-all. Purely additive: when the build dir is
# absent (e.g. dev via `npm run dev` on :5173) nothing is mounted and the API is
# unchanged.
def _resolve_frontend_dir() -> Path:
    """Resolve the static build directory (env-overridable, default
    ``<repo>/frontend/dist``)."""
    override = os.environ.get("CLIQ_FRONTEND_DIR")
    if override:
        return Path(override).expanduser().resolve()
    # server.py → web → cliq → src → <repo root>
    return (Path(__file__).resolve().parents[3] / "frontend" / "dist").resolve()


FRONTEND_DIR = _resolve_frontend_dir()

if FRONTEND_DIR.is_dir():
    _assets_dir = FRONTEND_DIR / "assets"
    if _assets_dir.is_dir():
        # Hashed JS/CSS bundles: let StaticFiles handle caching/range requests.
        app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

    _INDEX_HTML = FRONTEND_DIR / "index.html"

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        """Serve a real file from the build dir when it exists; otherwise fall
        back to ``index.html`` so client-side routing and page refreshes work.

        API/WS/health are registered earlier and always win. Unknown ``/api``,
        ``/ws``, and ``/health`` paths return a real 404 (not the SPA shell) so
        those namespaces keep clean HTTP semantics.
        """
        if full_path == "" or full_path == "index.html":
            return FileResponse(_INDEX_HTML)

        first_segment = full_path.split("/", 1)[0]
        if first_segment in {"api", "ws", "health"}:
            raise HTTPException(status_code=404, detail="Not Found")

        # Path-traversal guard: only serve files that resolve INSIDE the build
        # dir. `..` escapes and absolute paths are rejected by the containment
        # check below.
        candidate = (FRONTEND_DIR / full_path).resolve()
        if candidate.is_file() and (
            candidate == FRONTEND_DIR or FRONTEND_DIR in candidate.parents
        ):
            return FileResponse(candidate)

        return FileResponse(_INDEX_HTML)

    log.info("Serving static frontend from %s", FRONTEND_DIR)
else:
    log.info(
        "No static frontend at %s (set CLIQ_FRONTEND_DIR or run `npm run build`); "
        "API-only mode.",
        FRONTEND_DIR,
    )
