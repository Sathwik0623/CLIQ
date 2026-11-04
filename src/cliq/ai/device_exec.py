"""
CLIQ Copilot Autonomous Device Command Execution (M1.2).

Secure backend tool that lets CLIQ Copilot execute device commands THROUGH the
existing console execution infrastructure — the same live ``DeviceRuntime``
objects (``CMLConsoleDevice`` / ``SSHConsoleDevice``) and the same
``runtime.lock`` + ``execute_paged`` pathway the terminal WebSocket uses.
No parallel terminal implementation exists here; this module only validates,
authorizes, audits, and serializes Copilot-initiated executions.

Security model:
- Strict session ownership: ``(session_token, device_id)`` must resolve to a
  live runtime owned by the caller. No cross-session fallback.
- Wrong-device protection: the caller supplies ``lab_id`` + ``device_id`` and
  both must match the session's own device inventory record.
- Platform-aware validation: Cisco IOS commands never go to Linux endpoints
  and Linux commands never go to Cisco devices.
- Read-only diagnostics auto-execute; configuration-changing commands require
  a separate explicit approval ticket (prepare -> confirm -> execute with a
  single-use approval token). Critically destructive commands are always
  blocked, even with approval.
- Every attempt (allowed, blocked, or failed) emits an audit record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("cliq.ai.device_exec")

# ── Platform classification ──────────────────────────────────────────────────

PLATFORM_CISCO_IOS = "cisco_ios"
PLATFORM_LINUX = "linux"

#: CML node definitions (and SSH device types) that speak Cisco IOS-style CLI.
_IOS_NODE_DEFS = frozenset({
    "iosv", "iosvl2", "csr1000v", "cat8000v", "iosxrv", "iosxrv9000",
    "nxosv", "nxosv9000", "ioll2", "iol", "cisco_ios", "cisco_nxos",
    "cisco_xe", "cisco_xr",
})

#: CML node definitions (and SSH device types) that are Linux/BusyBox hosts.
_LINUX_NODE_DEFS = frozenset({
    "server", "desktop", "alpine", "ubuntu", "linux", "busybox", "host",
    "linux_host", "docker_host",
})


_IOS_ROUTER_DEFS = frozenset({
    "iosv", "csr1000v", "cat8000v", "iosxrv", "iosxrv9000",
    "cisco_ios", "cisco_xe", "cisco_xr",
})
_IOS_SWITCH_DEFS = frozenset({"iosvl2", "ioll2", "iol"})
_NXOS_SWITCH_DEFS = frozenset({"nxosv", "nxosv9000", "cisco_nxos"})


def describe_platform(node_type: str) -> str:
    """Human-readable platform label for inventory/context display.

    Unknown types stay unknown — never a guessed Cisco label. Display-only;
    execution validation is governed by :func:`classify_platform`.
    """
    norm = (node_type or "").strip().lower()
    if not norm or norm == "unknown":
        return "Unknown platform"
    if norm in _IOS_ROUTER_DEFS:
        return "Cisco IOS Router"
    if norm in _IOS_SWITCH_DEFS:
        return "Cisco IOSvL2 Switch"
    if norm in _NXOS_SWITCH_DEFS:
        return "Cisco NX-OS Switch"
    if norm in _LINUX_NODE_DEFS or "linux" in norm:
        return "Linux Endpoint"
    if norm in _IOS_NODE_DEFS:
        return "Cisco IOS Device"
    return "Unknown platform"


def classify_platform(node_type: str, ssh_device_type: str = "") -> str:
    """Map a device inventory type to ``cisco_ios`` / ``linux`` / ``""``.

    Returns ``""`` for unknown/unsupported types (e.g. ``external_connector``,
    empty SSH type) so the caller rejects with ``unsupported_platform`` instead
    of guessing.
    """
    norm = (node_type or "").strip().lower()
    if not norm or norm == "unknown":
        norm = (ssh_device_type or "").strip().lower()
    if not norm or norm == "unknown":
        return ""
    if norm in _IOS_NODE_DEFS:
        return PLATFORM_CISCO_IOS
    if norm in _LINUX_NODE_DEFS:
        return PLATFORM_LINUX
    # Prefix fallbacks for versioned/variant definitions (e.g. "iosv-2").
    for prefix in ("iosv", "ios", "nxos", "cat", "csr", "iosxr", "iosxe"):
        if norm.startswith(prefix):
            return PLATFORM_CISCO_IOS
    for prefix in ("linux", "alpine", "ubuntu", "busybox", "server", "desktop"):
        if norm.startswith(prefix):
            return PLATFORM_LINUX
    return ""


# ── Command classification ───────────────────────────────────────────────────

#: Critically destructive commands: ALWAYS blocked, approval cannot override.
_CRITICAL_BLOCKED_PREFIXES = (
    "reload",
    "write erase",
    "erase startup-config",
    "erase nvram",
    "format",
    "configure replace",
    "delete flash",
    "delete nvram",
)

#: IOS read-only diagnostic heads (auto-executed without approval).
_IOS_READ_ONLY_HEADS = frozenset({"show", "ping", "traceroute", "trace"})

#: IOS-only command heads: rejected on Linux endpoints (platform mismatch).
_IOS_ONLY_HEADS = frozenset({
    "show", "configure", "conf", "config", "interface", "vlan", "switchport",
    "spanning-tree", "router", "switch", "enable", "disable",
    "access-list", "ip access", "standby", "channel-group", "port-channel",
    "errdisable", "logging", "snmp-server", "username", "crypto",
})

#: Linux-only command heads: rejected on Cisco devices (platform mismatch).
#: ``ping``/``traceroute`` exist on both platforms and are intentionally absent.
_LINUX_ONLY_HEADS = frozenset({
    "ifconfig", "route", "arp", "netstat", "ss", "ip addr", "ip route",
    "ip link", "ip neigh", "ip -", "ls", "cat", "ps", "dmesg", "iptables",
    "brctl", "udhcpc", "wget", "ping6", "which", "busybox",
})

#: Linux read-only heads (auto-executed; config sub-forms need approval).
_LINUX_READ_ONLY_HEADS = frozenset({
    "ifconfig", "route", "arp", "ping", "traceroute", "ip", "netstat", "which", "busybox",
    "hostname",
})

#: Tokens that make a Linux diagnostic command configuration-changing.
_LINUX_CONFIG_TOKENS = frozenset({
    "add", "del", "delete", "up", "down", "netmask", "broadcast",
    "pointopoint", "hw", "ether", "multicast", "promisc", "flush",
    "set", "replace", "-s", "-d",
})

#: IOS output modifiers allowed after ``|`` (read-only filters).
_IOS_PIPE_MODIFIERS = frozenset({
    "include", "exclude", "begin", "section", "count",
})

#: Linux pipe targets allowed after ``|`` (read-only filters).
_LINUX_PIPE_TARGETS = frozenset({"grep", "head", "tail", "wc", "sort"})

#: Shell metacharacters that indicate command chaining/injection: always reject.
_CHAINING_RE = re.compile(r"[;&`]|&&|\|\||\$\(|\$\{")

_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

MAX_COMMAND_CHARS = 512
MAX_COMMANDS_PER_TURN = 8


def _strip_do_prefix(cmd: str) -> str:
    norm = " ".join(cmd.strip().split())
    if norm.lower().startswith("do "):
        return norm[3:].strip()
    return norm


def _split_pipe(cmd: str) -> Tuple[str, Optional[str]]:
    """Split ``cmd`` on the first ``|``; returns (head, filter or None)."""
    if "|" not in cmd:
        return cmd, None
    head, filt = cmd.split("|", 1)
    return head.strip(), filt.strip()


def _is_critical_blocked(cmd: str) -> bool:
    low = " ".join(cmd.strip().lower().split())
    if low.startswith("do "):
        low = low[3:].strip()
    return any(
        low == prefix or low.startswith(prefix + " ") or low.startswith(prefix + "|")
        for prefix in _CRITICAL_BLOCKED_PREFIXES
    )


def _linux_is_read_only(tokens: List[str]) -> bool:
    """True when a Linux diagnostic command is strictly read-only."""
    if not tokens:
        return False
    head = tokens[0]
    rest = tokens[1:]
    rest_low = [t.lower() for t in rest]
    if head == "ping":
        # Any ping form is read-only (count is enforced at send time).
        return not any(t in _LINUX_CONFIG_TOKENS for t in rest_low)
    if head == "traceroute":
        return True
    if head == "netstat":
        return True
    if head == "which":
        # Applet lookup has no configuration form.
        return True
    if head == "busybox":
        # Bare (usage) and --list are read-only; `busybox <applet> ...`
        # can invoke anything and needs approval.
        return rest_low in ([], ["--list"])
    if head == "hostname":
        # Bare `hostname` prints the name; any argument sets it.
        return not rest_low
    if head in ("ifconfig", "route", "arp"):
        if any(t in _LINUX_CONFIG_TOKENS for t in rest_low):
            return False
        # An IPv4 literal alongside ifconfig/route args means assignment.
        if _IPV4_RE.search(" ".join(rest)):
            return False
        return True
    if head == "ip":
        # Only explicit read-only subcommands; anything else needs approval.
        if not rest_low:
            return False
        if rest_low[0] not in ("addr", "route", "link", "neigh", "address"):
            return False
        if any(t in _LINUX_CONFIG_TOKENS for t in rest_low[1:]):
            return False
        return True
    return False


@dataclass
class CommandVerdict:
    ok: bool
    error_code: str = ""
    message: str = ""
    read_only: bool = False
    send_command: str = ""


def validate_command_for_platform(command: str, platform: str) -> CommandVerdict:
    """Validate ``command`` against ``platform`` grammar and safety policy.

    Returns a verdict with ``read_only=True`` for auto-executable diagnostics,
    ``read_only=False`` for configuration-changing commands (caller must demand
    an approval token), or ``ok=False`` with an ``error_code`` for rejections.
    Pure function — no I/O — so it is unit-testable offline.
    """
    raw = (command or "").strip()
    if not raw:
        return CommandVerdict(ok=False, error_code="empty_command",
                              message="Command is empty.")
    if "\n" in raw or "\r" in raw:
        return CommandVerdict(ok=False, error_code="multi_command",
                              message="Only a single command per request is allowed.")
    if len(raw) > MAX_COMMAND_CHARS:
        return CommandVerdict(ok=False, error_code="command_too_long",
                              message=f"Command exceeds {MAX_COMMAND_CHARS} characters.")
    if _CHAINING_RE.search(raw):
        return CommandVerdict(ok=False, error_code="multi_command",
                              message="Command chaining (; && || $() ``) is not allowed.")
    if _is_critical_blocked(raw):
        return CommandVerdict(
            ok=False, error_code="blocked_destructive",
            message=f"'{raw}' is critically destructive and is always blocked for Copilot.")

    cmd = _strip_do_prefix(raw)
    head_part, pipe_filter = _split_pipe(cmd)
    head_tokens = head_part.split()
    if not head_tokens:
        return CommandVerdict(ok=False, error_code="empty_command",
                              message="Command is empty.")
    head = head_tokens[0].lower()
    head_two = " ".join(head_tokens[:2]).lower()

    if platform == PLATFORM_CISCO_IOS:
        if head in _LINUX_ONLY_HEADS or head_two in _LINUX_ONLY_HEADS:
            return CommandVerdict(
                ok=False, error_code="platform_mismatch",
                message=f"'{raw}' is a Linux command and cannot run on a Cisco device.")
        if pipe_filter:
            filt_head = pipe_filter.split()[0].lower() if pipe_filter.split() else ""
            if filt_head not in _IOS_PIPE_MODIFIERS:
                return CommandVerdict(
                    ok=False, error_code="unsupported_command",
                    message=f"Pipe filter '| {pipe_filter}' is not a read-only IOS output modifier.")
        if head in _IOS_READ_ONLY_HEADS:
            return CommandVerdict(ok=True, read_only=True, send_command=raw)
        # Anything else on IOS (configure, interface, hostname, ...) changes or
        # may change configuration: explicit approval required.
        return CommandVerdict(ok=True, read_only=False, send_command=raw)

    if platform == PLATFORM_LINUX:
        if head in _IOS_ONLY_HEADS or head_two in _IOS_ONLY_HEADS:
            return CommandVerdict(
                ok=False, error_code="platform_mismatch",
                message=f"'{raw}' is a Cisco IOS command and cannot run on a Linux endpoint.")
        if pipe_filter:
            filt_head = pipe_filter.split()[0].lower() if pipe_filter.split() else ""
            if filt_head not in _LINUX_PIPE_TARGETS:
                return CommandVerdict(
                    ok=False, error_code="unsupported_command",
                    message=f"Pipe filter '| {pipe_filter}' is not an allowed read-only filter.")
        if head not in _LINUX_READ_ONLY_HEADS:
            return CommandVerdict(
                ok=False, error_code="unsupported_command",
                message=f"'{raw}' is not a supported Linux diagnostic command "
                        "(supported: hostname, ifconfig, route, arp, ping, traceroute, ip, netstat, which, busybox).")
        tokens = [head] + [t for t in head_part.split()[1:]]
        normalized = _normalize_linux_case(raw, pipe_filter)
        if _linux_is_read_only(tokens):
            send = _ensure_linux_ping_count(normalized)
            return CommandVerdict(ok=True, read_only=True, send_command=send)
        return CommandVerdict(ok=True, read_only=False, send_command=normalized)

    return CommandVerdict(ok=False, error_code="unsupported_platform",
                          message="Unknown device platform; cannot validate command.")


def _normalize_linux_case(raw: str, pipe_filter: Optional[str]) -> str:
    """Normalize a Linux command's case-sensitive parts to lowercase.

    BusyBox commands, subcommands, and short flags are lowercase-only; the
    device reports ``command not found`` for ``ARP`` or ``-N``. The head token
    and ``-flag`` tokens are lowercased; all other arguments (interface names,
    addresses, hostnames, filter patterns) are preserved verbatim so nothing
    the operator typed is silently rewritten. IOS needs no equivalent because
    its CLI is case-insensitive.
    """
    head_part = raw if pipe_filter is None else raw.split("|", 1)[0]
    tokens = head_part.split()
    if not tokens:
        return raw
    norm = [tokens[0].lower()]
    for tok in tokens[1:]:
        if re.fullmatch(r"-[A-Za-z]+", tok or ""):
            norm.append(tok.lower())
        else:
            norm.append(tok)
    send = " ".join(norm)
    if pipe_filter is not None:
        filt_tokens = pipe_filter.split()
        if filt_tokens:
            filt_tokens = [filt_tokens[0].lower()] + filt_tokens[1:]
        send = f"{send} | {' '.join(filt_tokens)}".rstrip()
    return send


def _ensure_linux_ping_count(command: str) -> str:
    """Append ``-c 4`` to a Linux ping lacking a count/deadline flag.

    A bare Linux ``ping`` runs forever; without a count the execution would
    always hit the timeout instead of returning output.
    """
    tokens = command.split()
    if not tokens or tokens[0].lower() != "ping":
        return command
    if any(t in ("-c", "-w", "-W", "-c4") or t.startswith("-c") for t in tokens[1:]):
        return command
    return f"{command} -c 4"


# ── Approval tickets (prepare -> confirm -> single-use token) ─────────────────

TICKET_TTL_SECONDS = 10 * 60
APPROVAL_TOKEN_TTL_SECONDS = 5 * 60

_approval_lock = threading.Lock()
_approval_tickets: Dict[str, Dict[str, Any]] = {}
_approval_tokens: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _prune_expired_locked() -> None:
    now = _now()
    for ticket_id in [k for k, v in _approval_tickets.items() if v["expires_at"] <= now]:
        _approval_tickets.pop(ticket_id, None)
    for tok in [k for k, v in _approval_tokens.items() if v["expires_at"] <= now]:
        _approval_tokens.pop(tok, None)


def prepare_approval(session_token: str, lab_id: str, device_id: str,
                     device_label: str, platform: str,
                     command: str) -> Dict[str, Any]:
    """Mint an approval ticket for a configuration-changing command.

    The ticket binds the exact ``(session, lab, device, command)`` tuple and
    expires after :data:`TICKET_TTL_SECONDS`. The engineer confirms it out of
    band (UI Approve button); confirmation yields a single-use approval token.
    """
    ticket_id = f"m12-{uuid.uuid4().hex[:12]}"
    with _approval_lock:
        _prune_expired_locked()
        _approval_tickets[ticket_id] = {
            "ticket_id": ticket_id,
            "session_token": session_token,
            "lab_id": lab_id,
            "device_id": device_id,
            "device_label": device_label,
            "platform": platform,
            "command": " ".join(command.strip().split()),
            "created_at": _now(),
            "expires_at": _now() + TICKET_TTL_SECONDS,
        }
    return dict(_approval_tickets[ticket_id])


def confirm_approval(session_token: str, ticket_id: str) -> Optional[Dict[str, Any]]:
    """Exchange a valid, owned, unexpired ticket for a single-use token."""
    with _approval_lock:
        _prune_expired_locked()
        ticket = _approval_tickets.get(ticket_id)
        if not ticket or ticket["session_token"] != session_token:
            return None
        _approval_tickets.pop(ticket_id, None)
        token = f"appr-{uuid.uuid4().hex}"
        _approval_tokens[token] = {
            "approval_token": token,
            "session_token": session_token,
            "lab_id": ticket["lab_id"],
            "device_id": ticket["device_id"],
            "command": ticket["command"],
            "created_at": _now(),
            "expires_at": _now() + APPROVAL_TOKEN_TTL_SECONDS,
            "used": False,
        }
        return dict(_approval_tokens[token])


def consume_approval_token(session_token: str, lab_id: str, device_id: str,
                           command: str, approval_token: str) -> bool:
    """Validate and single-use-consume an approval token. Fail-closed."""
    norm = " ".join((command or "").strip().split())
    with _approval_lock:
        _prune_expired_locked()
        rec = _approval_tokens.get(approval_token or "")
        if not rec or rec.get("used"):
            return False
        if rec["session_token"] != session_token:
            return False
        if rec["lab_id"] != lab_id or rec["device_id"] != device_id:
            return False
        if rec["command"] != norm:
            return False
        rec["used"] = True
        return True


# ── Structured result ────────────────────────────────────────────────────────

MAX_OUTPUT_CHARS = 32768

_IOS_ERROR_RE = re.compile(r"(?m)^\s*%\s*\S")
_LINUX_NOT_FOUND_RE = re.compile(
    r"(?i)(command not found|no such file|not found|invalid option|"
    r"unknown host|network is unreachable|bad address)")
_LINUX_ERROR_RE = re.compile(r"(?im)^(.*\b(error|failed|denied|refused)\b.*)$")


@dataclass
class DeviceExecResult:
    success: bool
    device_id: str = ""
    device_label: str = ""
    lab_id: str = ""
    platform: str = ""
    command: str = ""
    send_command: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0
    error: str = ""
    error_code: str = ""
    latency_ms: float = 0.0
    truncated: bool = False
    approval_required: bool = False
    ticket: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "success": self.success,
            "device_id": self.device_id,
            "device_label": self.device_label,
            "lab_id": self.lab_id,
            "platform": self.platform,
            "command": self.command,
            "send_command": self.send_command or self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_status": self.exit_status,
            "error": self.error,
            "error_code": self.error_code,
            "latency_ms": round(self.latency_ms, 2),
            "truncated": self.truncated,
            "approval_required": self.approval_required,
        }
        if self.ticket:
            d["ticket"] = {
                k: v for k, v in self.ticket.items() if k != "session_token"
            }
        return d


def _detect_device_error(platform: str, output: str) -> Tuple[int, str]:
    """Derive (exit_status, error_line) from console output text.

    Consoles carry no real exit codes; IOS reports errors as ``%``-prefixed
    lines and BusyBox as ``not found``/``error`` prose. First error line wins.
    """
    text = output or ""
    if platform == PLATFORM_CISCO_IOS:
        for line in text.splitlines():
            if line.strip().startswith("%"):
                return 1, line.strip()[:300]
        return 0, ""
    m = _LINUX_NOT_FOUND_RE.search(text)
    if m:
        line = next((l for l in text.splitlines()
                     if m.group(0).lower() in l.lower()), m.group(0))
        return 127, line.strip()[:300]
    m = _LINUX_ERROR_RE.search(text)
    if m:
        return 1, m.group(1).strip()[:300]
    return 0, ""


# ── Audit ────────────────────────────────────────────────────────────────────

def _audit_record(record: Dict[str, Any]) -> None:
    """Append one JSONL audit record. Best-effort: never raises."""
    try:
        from cliq.utils.observability import redact as _redact
        safe_cmd = str(_redact(record.get("command", "")))
    except Exception:
        safe_cmd = str(record.get("command", ""))[:200]
    record = dict(record)
    record["command"] = safe_cmd
    record["ts"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    try:
        from cliq.utils import observability as obs
        obs.log_event(
            "command", "copilot_exec", "complete",
            "success" if record.get("success") else "error",
            elapsed_ms=float(record.get("latency_ms") or 0.0),
            detail=f"{record.get('device_label')}:{record.get('error_code') or 'ok'}",
            device_uuid=str(record.get("device_id") or ""),
            lab_id=str(record.get("lab_id") or ""),
            platform=str(record.get("platform") or ""),
        )
    except Exception:
        pass
    try:
        audit_path = Path.home() / ".cliq" / "copilot_exec_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("copilot exec audit write failed: %s", exc)


def _base_audit(session_token: str, lab_id: str, device_id: str,
                device_label: str, platform: str, command: str,
                actor: str = "copilot", plan_id: str = "") -> Dict[str, Any]:
    audit: Dict[str, Any] = {
        "actor": actor,
        "session_prefix": (session_token or "")[:8],
        "lab_id": lab_id,
        "device_id": device_id,
        "device_label": device_label,
        "platform": platform,
        "command": command,
    }
    if plan_id:
        audit["plan_id"] = plan_id
    return audit


# ── Ownership resolution ─────────────────────────────────────────────────────

def _get_server_module():
    from cliq.web import server
    return server


def resolve_owned_device(session_token: str, lab_id: str,
                         device_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Resolve and ownership-validate a device. Returns (meta, error_code).

    Strict: the token must own the session, the device must be in that token's
    own inventory, and the inventory ``lab_id`` must equal the requested one.
    No cross-session fallback (unlike the advisory context service).
    """
    server = _get_server_module()
    if not session_token or session_token not in server.SESSIONS:
        return None, "unauthorized"
    meta = (server.DEVICES_BY_TOKEN.get(session_token) or {}).get(device_id)
    if not meta:
        return None, "device_not_found"
    if (meta.get("lab_id") or "") != (lab_id or ""):
        return None, "wrong_lab"
    return meta, ""


# ── Execution (reuses the terminal console pathway) ──────────────────────────

DEFAULT_TIMEOUT_SECONDS = 30.0
MIN_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 120.0


#: Bound on auto-paged output per command. CML consoles keep native
#: ``--More--`` paging (``terminal length 0`` is deliberately never set), so
#: Copilot must drain the pager itself; the cap keeps a runaway listing from
#: stalling the turn (the caller's timeout still applies on top).
MAX_PAGER_PAGES = 25


def _run_on_device(device: Any, send_command: str) -> str:
    """Run one command via the device's existing console driver.

    Prefers ``execute_paged`` (same entry the WebSocket terminal uses) and
    falls back to ``execute``. Runs in a worker thread via ``asyncio.to_thread``.
    """
    if hasattr(device, "execute_paged"):
        output, more = device.execute_paged(send_command)
        pager = getattr(device, "pager_send_key", None)
        if more and callable(pager):
            # Autonomous drain: the interactive terminal leaves paging to the
            # engineer, but here an undrained pager would desync every later
            # command (leftover pages land in the next command's slot).
            pages = [output or ""]
            while more and len(pages) < MAX_PAGER_PAGES:
                nxt, more = pager(" ")
                pages.append(nxt or "")
            if more:
                # Still paging: quit to restore a clean prompt for the next
                # command, and mark the truncation in-band (never silent).
                try:
                    pager("q")
                except Exception:
                    pass
                pages.append(
                    f"…[pager truncated after {MAX_PAGER_PAGES} pages]")
            return "\n".join(pages)
        return output or ""
    if hasattr(device, "execute"):
        return device.execute(send_command) or ""
    raise RuntimeError("Device driver supports neither execute_paged nor execute")


def _history_prefix(plan_id: str, send_command: str) -> str:
    """Session-history prefix distinguishing plan executions."""
    if plan_id:
        return f"[plan:{plan_id}] {send_command}"
    return f"[copilot] {send_command}"


async def _run_validated_command(
    session_token: str,
    lab_id: str,
    device_id: str,
    device_label: str,
    platform: str,
    command: str,
    send_command: str,
    timeout: float,
    t0: float,
    actor: str = "copilot",
    plan_id: str = "",
) -> DeviceExecResult:
    """Run an already-validated + approved command via the console pathway.

    Shared by single-command execution (M1.2, after per-command approval)
    and plan execution (M1.4, after plan approval). Both callers enforce
    identical ownership, platform, and approval checks before reaching here;
    this helper owns runtime resolution, locking, timeout, error detection,
    and auditing — the single execution pathway.
    """
    server = _get_server_module()
    timeout = max(MIN_TIMEOUT_SECONDS, min(float(timeout or DEFAULT_TIMEOUT_SECONDS),
                                           MAX_TIMEOUT_SECONDS))
    runtime = server._DEVICE_RUNTIMES.get((session_token, device_id))
    if (runtime is None or getattr(runtime, "device", None) is None
            or getattr(runtime, "dead", False)
            or getattr(runtime, "status", "") == "disconnected"):
        reason = getattr(runtime, "dead_reason", "") if runtime else ""
        msg = (f"Device '{device_label}' is not connected in this session"
               + (f" ({reason}). Reconnect the terminal first."
                  if reason else ". Reconnect the terminal first."))
        audit = _base_audit(session_token, lab_id, device_id, device_label,
                            platform, command or "",
                            actor=actor, plan_id=plan_id)
        audit.update({"success": False, "error_code": "device_disconnected",
                      "latency_ms": 0.0, "error": msg,
                      "send_command": send_command})
        _audit_record(audit)
        return DeviceExecResult(success=False, device_id=device_id,
                                device_label=device_label, lab_id=lab_id,
                                platform=platform, command=(command or "").strip(),
                                send_command=send_command,
                                error=msg, error_code="device_disconnected",
                                exit_status=2)
    try:
        if not bool(getattr(runtime.device, "is_connected", True)):
            raise ConnectionError("device channel is not connected")
    except ConnectionError as exc:
        msg = f"Device '{device_label}' console channel is down ({exc}). Reconnect first."
        audit = _base_audit(session_token, lab_id, device_id, device_label,
                            platform, command or "",
                            actor=actor, plan_id=plan_id)
        audit.update({"success": False, "error_code": "device_disconnected",
                      "latency_ms": 0.0, "error": msg,
                      "send_command": send_command})
        _audit_record(audit)
        return DeviceExecResult(success=False, device_id=device_id,
                                device_label=device_label, lab_id=lab_id,
                                platform=platform, command=(command or "").strip(),
                                send_command=send_command,
                                error=msg, error_code="device_disconnected",
                                exit_status=2)

    send_command = send_command
    try:
        # Serialize with terminal I/O on this device (same lock the WebSocket
        # run_command path holds) and bound the wait with a timeout.
        async with runtime.lock:
            output = await asyncio.wait_for(
                asyncio.to_thread(_run_on_device, runtime.device, send_command),
                timeout=timeout,
            )
    except asyncio.TimeoutError:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        msg = (f"Command timed out after {timeout:.0f}s on {device_label}. "
               "The device may still be producing output; retry or check the terminal.")
        audit = _base_audit(session_token, lab_id, device_id, device_label,
                            platform, command or "",
                            actor=actor, plan_id=plan_id)
        audit.update({"success": False, "error_code": "timeout",
                      "latency_ms": latency_ms, "error": msg,
                      "send_command": send_command})
        _audit_record(audit)
        return DeviceExecResult(success=False, device_id=device_id,
                                device_label=device_label, lab_id=lab_id,
                                platform=platform, command=(command or "").strip(),
                                send_command=send_command,
                                error=msg, error_code="timeout",
                                exit_status=124, latency_ms=latency_ms)
    except Exception as exc:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        from cliq.connection.live_device import ConnectionLostError
        try:
            from cliq.connection.ssh_console_device import ConsoleBusyError
        except Exception:
            ConsoleBusyError = ()  # type: ignore
        if isinstance(exc, (ConnectionError, ConnectionLostError, ConsoleBusyError)):
            msg = f"Console connection to '{device_label}' failed ({exc}). Reconnect first."
            code = "device_disconnected"
            status = 2
        else:
            msg = f"Execution on '{device_label}' failed: {str(exc)[:200]}"
            code = "execution_error"
            status = 2
        audit = _base_audit(session_token, lab_id, device_id, device_label,
                            platform, command or "",
                            actor=actor, plan_id=plan_id)
        audit.update({"success": False, "error_code": code,
                      "latency_ms": latency_ms, "error": msg,
                      "send_command": send_command})
        _audit_record(audit)
        return DeviceExecResult(success=False, device_id=device_id,
                                device_label=device_label, lab_id=lab_id,
                                platform=platform, command=(command or "").strip(),
                                send_command=send_command,
                                error=msg, error_code=code,
                                exit_status=status, latency_ms=latency_ms)

    latency_ms = (time.perf_counter() - t0) * 1000.0
    output = output or ""
    truncated = len(output) > MAX_OUTPUT_CHARS
    if truncated:
        output = output[:MAX_OUTPUT_CHARS] + "\n…[output truncated]"
    exit_status, dev_error = _detect_device_error(platform, output)
    try:
        runtime.empty_read_strikes = 0
        prompt = getattr(runtime.device, "current_prompt", "") or ""
        if prompt:
            runtime.prompt = prompt
    except Exception:
        pass
    try:
        from cliq.memory.store import SessionLog
        server.SESSION_LOG.append_command(
            SessionLog.make_key(lab_id, device_id), _history_prefix(plan_id, send_command))
    except Exception:
        pass

    audit = _base_audit(session_token, lab_id, device_id, device_label,
                        platform, command or "",
                        actor=actor, plan_id=plan_id)
    audit.update({"success": exit_status == 0,
                  "error_code": "" if exit_status == 0 else "device_error",
                  "latency_ms": latency_ms,
                  "exit_status": exit_status,
                  "send_command": send_command,
                  "output_lines": len(output.splitlines()),
                  "output_excerpt": output[:300],
                  "error": dev_error})
    _audit_record(audit)

    if exit_status != 0:
        return DeviceExecResult(success=False, device_id=device_id,
                                device_label=device_label, lab_id=lab_id,
                                platform=platform, command=(command or "").strip(),
                                send_command=send_command,
                                stdout=output, stderr="", exit_status=exit_status,
                                error=dev_error or "Device reported an error.",
                                error_code="device_error", latency_ms=latency_ms,
                                truncated=truncated)
    return DeviceExecResult(success=True, device_id=device_id,
                            device_label=device_label, lab_id=lab_id,
                            platform=platform, command=(command or "").strip(),
                            send_command=send_command,
                            stdout=output, stderr="", exit_status=0,
                            error="", error_code="", latency_ms=latency_ms,
                            truncated=truncated)



async def execute_device_command(
    session_token: str,
    lab_id: str,
    device_id: str,
    command: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    approval_token: Optional[str] = None,
) -> DeviceExecResult:
    """Execute ``command`` on ``device_id`` via the existing console pathway.

    Reuses the live ``DeviceRuntime`` + ``runtime.lock`` + console driver, so a
    Copilot execution serializes with the engineer's terminal I/O on the same
    device exactly like a second terminal command would.
    """
    t0 = time.perf_counter()
    timeout = max(MIN_TIMEOUT_SECONDS, min(float(timeout or DEFAULT_TIMEOUT_SECONDS),
                                           MAX_TIMEOUT_SECONDS))

    meta, err = resolve_owned_device(session_token, lab_id, device_id)
    if err:
        audit = _base_audit(session_token, lab_id, device_id, "", "", command or "")
        audit.update({"success": False, "error_code": err, "latency_ms": 0.0,
                      "error": {"unauthorized": "Invalid or missing session.",
                                "device_not_found": f"Device '{device_id}' is not in this session.",
                                "wrong_lab": f"Device '{device_id}' does not belong to lab '{lab_id}'."}[err]})
        _audit_record(audit)
        return DeviceExecResult(success=False, device_id=device_id, lab_id=lab_id,
                                command=(command or "").strip(), error=audit["error"],
                                error_code=err, exit_status=2)

    device_label = str(meta.get("label") or device_id)
    platform = classify_platform(str(meta.get("type") or ""),
                                 str(meta.get("ssh_device_type") or ""))
    if not platform:
        audit = _base_audit(session_token, lab_id, device_id, device_label, "", command or "")
        audit.update({"success": False, "error_code": "unsupported_platform",
                      "latency_ms": 0.0,
                      "error": f"Device '{device_label}' has unsupported platform "
                               f"'{meta.get('type')}' for Copilot execution."})
        _audit_record(audit)
        return DeviceExecResult(success=False, device_id=device_id,
                                device_label=device_label, lab_id=lab_id,
                                command=(command or "").strip(), error=audit["error"],
                                error_code="unsupported_platform", exit_status=2)

    verdict = validate_command_for_platform(command or "", platform)
    if not verdict.ok:
        audit = _base_audit(session_token, lab_id, device_id, device_label,
                            platform, command or "")
        audit.update({"success": False, "error_code": verdict.error_code,
                      "latency_ms": 0.0, "error": verdict.message})
        _audit_record(audit)
        return DeviceExecResult(success=False, device_id=device_id,
                                device_label=device_label, lab_id=lab_id,
                                platform=platform, command=(command or "").strip(),
                                send_command=verdict.send_command,
                                error=verdict.message, error_code=verdict.error_code,
                                exit_status=2)

    if not verdict.read_only:
        approved = bool(approval_token) and consume_approval_token(
            session_token, lab_id, device_id, command or "", approval_token or "")
        if not approved:
            ticket = prepare_approval(session_token, lab_id, device_id,
                                      device_label, platform, command or "")
            msg = (f"'{(command or '').strip()}' may change configuration on "
                   f"{device_label}; explicit approval is required.")
            audit = _base_audit(session_token, lab_id, device_id, device_label,
                                platform, command or "")
            audit.update({"success": False, "error_code": "approval_required",
                          "latency_ms": 0.0, "error": msg,
                          "send_command": verdict.send_command,
                          "ticket_id": ticket["ticket_id"]})
            _audit_record(audit)
            return DeviceExecResult(success=False, device_id=device_id,
                                    device_label=device_label, lab_id=lab_id,
                                    platform=platform, command=(command or "").strip(),
                                    send_command=verdict.send_command,
                                    error=msg, error_code="approval_required",
                                    exit_status=2, approval_required=True, ticket=ticket)
    return await _run_validated_command(
        session_token, lab_id, device_id, device_label, platform,
        command or "", verdict.send_command, timeout, t0,
    )


# ── Natural-language intent parsing (orchestrator helper) ────────────────────

#: Verbs that signal the engineer wants Copilot to run commands itself.
_EXEC_INTENT_RE = re.compile(
    r"\b(execute|run|retrieve|get|collect|inspect|check|pull|fetch|gather|"
    r"capture|poll|query|execute these|run these)\b",
    re.IGNORECASE,
)

_SELF_RE = re.compile(
    r"\b(yourself|itself|for me|on (the )?device|on sw\d+|on r\d+|"
    r"from sw\d+|from r\d+)\b",
    re.IGNORECASE,
)

#: Explicit execution declines ("don't run ...", "do not execute ..."). Named
#: devices + named commands imply dispatch (the verb is optional) — unless the
#: turn explicitly declines it. The gap excludes clause boundaries so "don't
#: know, run ..." still dispatches.
_EXEC_DECLINED_RE = re.compile(
    r"\b(?:do\s+not|don't|never)\b[^,;.\n]{0,20}\b(?:execut|run|collect|"
    r"retrie|inspect|check|fetch|gather|poll|query)|"
    r"\bstop\b[^,;.\n]{0,20}\b(?:execut|run)\b",
    re.IGNORECASE,
)

#: Known IOS ``show`` keywords. Extraction-only: a ``show`` phrase is treated as
#: an executable command ONLY when its first keyword is in this set, so prose
#: such as "Show the actual execution output..." is never planned. The explicit
#: REST path stays device-authoritative (any ``show`` form may be sent there and
#: the device reports ``%`` errors honestly).
_IOS_SHOW_KEYWORDS = frozenset({
    "vlan", "interfaces", "interface", "ip", "ipv6", "mac", "mac-address-table",
    "address-table", "spanning-tree", "stp", "cdp", "lldp", "etherchannel",
    "port-channel", "channel-group", "version", "running-config", "startup-config",
    "run", "start", "boot", "bootvar", "inventory", "logging", "log", "arp",
    "clock", "calendar", "users", "processes", "cpu", "memory", "protocols",
    "controllers", "diag", "diagnostic", "env", "environment", "tech-support",
    "tech", "privilege", "sessions", "history", "hosts", "host", "access-lists",
    "access-list", "standby", "hsrp", "vrrp", "glbp", "vrf", "bgp", "ospf",
    "eigrp", "isis", "rip", "mpls", "snmp", "ntp", "timezone", "file", "flash",
    "flash0", "flash1", "license", "platform", "module", "modules", "power",
    "power-supply", "fans", "fan", "temperature", "transceivers", "controllers",
    "line", "lines", "terminal", "snmp-server", "crypto", "dot1x", "authentication",
    "errdisable", "storm-control", "udld", "vtp", "dtp", "pagp", "lacp", "qos",
    "policy-map", "class-map", "service-policy", "flow", "netflow", "sflow",
    "monitor", "rmon", "alarm", "call", "voice", "dial-peer", "isdn", "controller",
    "frame-relay", "atm", "serial", "ethernet", "fastethernet", "gigabitethernet",
    "tengigabitethernet", "loopback", "tunnel", "vlan-switch", "switch",
    "switchport", "trunk", "vlan-membership", "spanning", "bridge", "bridge-group",
})

#: Scannable diagnostic command heads in free text. Argument matchers refuse to
#: swallow the conjunction "and", location prepositions ("on SW1"), pronouns,
#: or the next command's head word.
_NON_ARG = (r"and\b|on\b|for\b|from\b|to\b|in\b|at\b|with\b|via\b|"
           r"yourself\b|itself\b")
_CMD_HEAD = (r"show\b|ping\b|traceroute\b|trace\b|ifconfig\b|route\b|arp\b|ip\b|"
             r"netstat\b|which\b|busybox\b|sudo\b|hostname\b")
_NEXT_ARG = rf"(?!{_NON_ARG})"
_NEXT_HEAD = rf"(?!{_NON_ARG}|{_CMD_HEAD})"
_SHOW_TAIL_END = (r"(?:,|;|\band\b|\bon\b|\bfor\b|\bfrom\b|\bto\b|\bin\b|\bat\b|"
                  r"\bwith\b|\bvia\b|\byourself\b|\bitself\b|\n|\.(?:\s|$)|$)")
_SHOW_KEYWORD_ALT = r"(?:{})".format("|".join(sorted(_IOS_SHOW_KEYWORDS, key=len, reverse=True)))
_COMMAND_SCAN_RE = re.compile(
    r"(?im)\b("
    rf"show\s+{_SHOW_KEYWORD_ALT}\b[a-z0-9\-\/\.\s]*?(?={_SHOW_TAIL_END})|"
    rf"ping(?:[ \t]+{_NEXT_HEAD}\S+){{1,4}}|"
    rf"traceroute(?:[ \t]+{_NEXT_HEAD}\S+){{1,4}}|"
    rf"trace(?:[ \t]+{_NEXT_HEAD}\S+){{1,4}}|"
    rf"ifconfig(?:[ \t]+{_NEXT_HEAD}(?:-[a-zA-Z]+|[a-zA-Z0-9][a-zA-Z0-9\-\.]*))?|"
    r"route(?:[ \t]+-[a-zA-Z]+)?|"
    r"arp(?:[ \t]+-[a-zA-Z]+)?|"
    r"ip[ \t]+(?:addr|route|link|neigh)(?:[ \t]+show|[ \t]+list)?|"
    r"netstat(?:[ \t]+-[a-zA-Z]+)?|"
    rf"which(?:[ \t]+{_NEXT_ARG}[a-z0-9_\.][a-z0-9_\.\-]*)+|"
    r"busybox(?:[ \t]+--list)?|"
    r"hostname\b"
    r")",
)


def _candidate_devices(user_text: str,
                       devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for dev in devices:
        for key in ("label", "id"):
            name = str(dev.get(key) or "").strip()
            if not name:
                continue
            if re.search(rf"\b{re.escape(name)}\b", user_text, re.IGNORECASE):
                found.append(dev)
                break
    # De-duplicate while preserving order.
    seen, unique = set(), []
    for dev in found:
        did = dev.get("id")
        if did not in seen:
            seen.add(did)
            unique.append(dev)
    return unique


def _candidate_commands(user_text: str, limit: int = MAX_COMMANDS_PER_TURN) -> List[str]:
    commands: List[str] = []
    for match in _COMMAND_SCAN_RE.finditer(user_text or ""):
        cmd = " ".join(match.group(1).strip().rstrip(".").split())
        if not cmd or len(cmd) > MAX_COMMAND_CHARS:
            continue
        low = cmd.lower()
        if low in ("show", "ip", "route"):
            continue
        if cmd not in commands:
            commands.append(cmd)
        if len(commands) >= limit:
            break
    return commands


@dataclass
class RejectedCommand:
    """An extracted candidate refused before execution, with the reason."""

    command: str = ""
    error_code: str = ""
    message: str = ""


MAX_MULTI_DEVICES = 10


@dataclass
class AutonomousPlan:
    device_id: str = ""
    device_label: str = ""
    lab_id: str = ""
    commands: List[str] = field(default_factory=list)
    rejected: List[RejectedCommand] = field(default_factory=list)


def plan_multi_execution(user_text: str,
                         devices: List[Dict[str, Any]],
                         lab_lookup: Dict[str, str],
                         platform_lookup: Dict[str, str]) -> List[AutonomousPlan]:
    """Plan read-only execution across explicitly mentioned devices.

    Each device receives the platform-appropriate subset of the extracted
    commands; mismatches and overflow are explicit per-device rejections.
    """
    # Explicit devices + explicit commands imply dispatch (the verb is
    # optional); only an explicit decline opts out. Extraction below still
    # admits only validated command patterns, never prose.
    if _EXEC_DECLINED_RE.search(user_text or ""):
        return []
    matched = _candidate_devices(user_text or "", devices)
    if not matched:
        return []
    selected, overflow = matched[:MAX_MULTI_DEVICES], matched[MAX_MULTI_DEVICES:]
    commands = _candidate_commands(
        user_text, limit=MAX_COMMANDS_PER_TURN * max(1, len(selected)))
    if not commands:
        return []
    plans: List[AutonomousPlan] = []
    for dev in selected:
        device_id = str(dev.get("id"))
        platform = platform_lookup.get(device_id, "")
        plan = AutonomousPlan(
            device_id=device_id,
            device_label=str(dev.get("label") or device_id),
            lab_id=lab_lookup.get(device_id, ""))
        for cmd in commands:
            verdict = validate_command_for_platform(cmd, platform)
            if verdict.ok and verdict.read_only:
                if len(plan.commands) >= MAX_COMMANDS_PER_TURN:
                    plan.rejected.append(RejectedCommand(
                        command=cmd, error_code="too_many_commands",
                        message=(f"per-device limit of {MAX_COMMANDS_PER_TURN} "
                                 "commands exceeded; re-request leftovers")))
                else:
                    plan.commands.append(verdict.send_command)
            elif verdict.ok and not verdict.read_only:
                plan.rejected.append(RejectedCommand(
                    command=cmd, error_code="approval_required",
                    message="Configuration-changing command needs explicit approval."))
            else:
                plan.rejected.append(RejectedCommand(
                    command=cmd, error_code=verdict.error_code,
                    message=verdict.message))
        plans.append(plan)
    for dev in overflow:
        plans.append(AutonomousPlan(
            device_id=str(dev.get("id")),
            device_label=str(dev.get("label") or dev.get("id")),
            lab_id=lab_lookup.get(str(dev.get("id")), ""),
            rejected=[RejectedCommand(
                command="", error_code="too_many_devices",
                message=(f"device limit of {MAX_MULTI_DEVICES} exceeded; "
                         "re-request this device separately"))]))
    return plans


def dispatch_skip_reason(user_text: str,
                         devices: List[Dict[str, Any]],
                         lab_lookup: Dict[str, str]) -> str:
    """Explain why autonomous execution would not dispatch ("" if it would).

    Mirrors ``plan_multi_execution`` gates plus the orchestrator's lab-id
    filter, so a turn naming devices and commands that dispatches nothing is
    reported explicitly instead of passing as a silent inspection. Declined
    turns and turns requesting nothing return "" (nothing failed).
    """
    text = user_text or ""
    if _EXEC_DECLINED_RE.search(text):
        return ""
    try:
        matched = _candidate_devices(text, devices or [])
        commands = _candidate_commands(text)
    except Exception:
        return "the request could not be parsed for devices and commands"
    if not matched or not commands:
        return ""
    missing = [str(d.get("label") or d.get("id")) for d in matched
               if not lab_lookup.get(str(d.get("id")), "")]
    if missing and len(missing) == len(matched):
        return ("no lab context is available for " + ", ".join(missing)
                + "; open the lab so its devices attach to this session")
    return ""


def plan_autonomous_execution(
    user_text: str,
    devices: List[Dict[str, Any]],
    lab_lookup: Optional[Dict[str, str]] = None,
    platform_lookup: Optional[Dict[str, str]] = None,
) -> Optional[AutonomousPlan]:
    """Parse a Copilot turn into a single-device autonomous execution plan.

    Returns ``None`` when the turn is advisory-only (no exec intent, no device
    mention, ambiguous devices, or no recognizable commands). When the target
    platform is known, every candidate is pre-validated: only commands passing
    platform validation are planned for execution; the rest land in
    ``rejected`` with reasons so the orchestrator can explain without ever
    touching the device. Pure function of its inputs — no I/O — so it is
    unit-testable offline.
    """
    text = user_text or ""
    if not _EXEC_INTENT_RE.search(text):
        return None
    matched = _candidate_devices(text, devices)
    # Single-device plans only: multi-device fan-out stays advisory until the
    # engineer confirms each target explicitly.
    if len(matched) != 1:
        return None
    candidates = _candidate_commands(text)
    if not candidates:
        return None
    dev = matched[0]
    device_id = str(dev.get("id") or "")
    lab_id = ""
    if lab_lookup is not None:
        lab_id = lab_lookup.get(device_id, "")
    platform = ""
    if platform_lookup is not None:
        platform = platform_lookup.get(device_id, "")
    if not platform:
        platform = classify_platform(
            str(dev.get("node_type") or dev.get("type") or ""))
    commands: List[str] = []
    rejected: List[RejectedCommand] = []
    for cand in candidates:
        if not platform:
            # Platform unknown: keep the candidate; execute_device_command
            # re-validates against the real inventory platform before any
            # device I/O, so nothing unvalidated can reach the runtime.
            commands.append(cand)
            continue
        verdict = validate_command_for_platform(cand, platform)
        if verdict.ok:
            commands.append(cand)
        else:
            rejected.append(RejectedCommand(command=cand,
                                            error_code=verdict.error_code,
                                            message=verdict.message))
    return AutonomousPlan(device_id=device_id,
                          device_label=str(dev.get("label") or device_id),
                          lab_id=lab_id, commands=commands, rejected=rejected)


#: Tool contract advertised to the Copilot system prompt and API clients.
TOOL_SPEC: Dict[str, Any] = {
    "name": "execute_device_command",
    "description": (
        "Execute a CLI command on a lab device through the IDE's existing "
        "console session and return the actual device output."
    ),
    "parameters": {
        "lab_id": "CML lab id owning the device (wrong-device protection).",
        "device_id": "Target device id within the caller's session.",
        "command": "Single read-only diagnostic command (IOS show/ping/traceroute; "
                   "Linux hostname/ifconfig/route/arp/ping/traceroute/ip/netstat).",
        "timeout": "Execution timeout in seconds (5-120, default 30).",
        "approval_token": "Single-use token for configuration-changing commands.",
    },
    "platforms": ["cisco_ios (IOSv/IOSvL2)", "linux (BusyBox endpoints)"],
    "read_only_auto": True,
    "config_requires_approval": True,
}
