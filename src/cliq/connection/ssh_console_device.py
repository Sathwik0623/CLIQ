#!/usr/bin/env python3
"""
ssh_console_device.py — SSH terminal-server console backend for REAL hardware.

CLIQ was CML-only (:class:`cliq.connection.live_device.CMLConsoleDevice`). This
adds an ADDITIVE second transport so CLIQ can drive a *real* Cisco device whose
console is reachable through a lab **terminal server** over SSH — e.g. an
ISR4331 (IOS-XE 16.6.4) at ``ts01.example.org`` with a port-mapped
username (``operator:19`` = that box's console line).

Design goals (identical north-star to the CML path):

* **Same surface** — implements the :class:`cliq.connection.transport.DeviceTransport`
  protocol, so every piece of CLIQ intelligence (``?``-help capture → ghost,
  device-over-KB reconciliation, learning, capability gating, abbreviation
  expansion) works UNCHANGED over SSH.
* **Input integrity (Bug N)** — the exact bytes CLIQ hands :meth:`execute` are
  the exact bytes sent to the device; every write funnels through
  :meth:`_audit_send` which logs typed-vs-sent (credentials redacted) so
  corruption on real hardware is provable.
* **Never-execute** — ``?``-help is captured with NO submit newline and the
  re-displayed line is cleared with Ctrl-U; nothing auto-runs.
* **Device is truth** — prompt/mode detection reuses the CML path's parsers, so
  the default-hostname ``Router>`` / ``Router#`` is classified correctly even
  without a custom hostname, and the device's own ``% Invalid`` drives
  did-you-mean reconciliation.

netmiko (``cisco_ios`` / ``cisco_xe``) is used for the heavy lifting (prompt
handling, enable, paging control) because it is IOS-XE-aware and far more robust
than raw paramiko for real gear.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

from cliq.connection.live_device import (
    ConnectionError,
    ConnectionLostError,
    _ANSI_STRIP,
    _PROMPT_RE,
    _extract_context_help,
    is_help_error,
    mode_from_prompt,
)
from cliq.connection.output_cleaner import clean
from cliq.utils import observability as obs
from cliq.validation.platform_rules import (
    capability_from_pid,
    node_type_from_pid,
    parse_pid_from_running_config,
    parse_pid_from_show_version,
)

log = logging.getLogger("cliq.connection.ssh_console")

# Per-byte hex tracing (added during the ^@ diagnostics phase) is OFF by
# default; set CLIQ_TRACE_BYTES=1 to re-enable the SSH_RAW_READ/SSH_EMIT hex
# dumps at DEBUG. Never gates transport behaviour — logging only.
_TRACE_BYTES = os.getenv("CLIQ_TRACE_BYTES", "").strip().lower() in ("1", "true", "yes", "on")

# Default terminal server for the CX BGL lab (documented for convenience; every
# field is still an explicit constructor argument, nothing is hardcoded into the
# connection itself).
DEFAULT_TS_HOST = "ts01.example.org"

# ``--More--`` pager token (same family the CML path recognizes).
_MORE_RE = re.compile(r"--+\s*More\s*--+|---\(MORE\)---", re.IGNORECASE)

# A prompt token the console prepends to its command echo. Unlike a full prompt
# match this does NOT require trailing whitespace, because a terminal server
# echoes the command with NO space after the prompt (e.g. ``Router#show run``).
# Used to recognise and strip the echoed command line from captured output.
_PROMPT_TOKEN_RE = re.compile(r"^[\w.\-]+(?:\([^)]+\))?[#>]\s*")

# Terminal-server console-line "occupied" phrasings. Single-session consoles
# reject (or refuse) a second attach; we surface this as a distinct, actionable
# error instead of a misleading "authentication failed".
_BUSY_RE = re.compile(
    r"(already in use|line\s+\d+\s+is\s+in\s+use|\bin use\b|connection refused"
    r"|refused by remote host|session\s+\w+\s+is\s+active|console.*busy)",
    re.IGNORECASE,
)

try:
    from netmiko import (
        ConnectHandler,
        NetmikoAuthenticationException,
        NetmikoTimeoutException,
        redispatch,
    )
    _NETMIKO_OK = True
except ImportError:  # pragma: no cover - netmiko is a core dep, guard anyway
    _NETMIKO_OK = False
    ConnectHandler = None  # type: ignore[assignment]
    NetmikoAuthenticationException = Exception  # type: ignore[assignment]
    NetmikoTimeoutException = Exception  # type: ignore[assignment]
    redispatch = None  # type: ignore[assignment]


class ConsoleBusyError(ConnectionError):
    """Raised when the target console line is already held by another session.

    A subclass of :class:`ConnectionError` so existing generic error handling
    still catches it, while call sites that care can distinguish a busy console
    (single-session, another user attached) from a wrong-password auth failure.
    """


def parse_host_port(raw_host: str, default_port: int = 22) -> tuple[str, int]:
    """Parse host and port, stripping schemes, trailing slashes, and extracting :port.

    Handles:
      - '10.10.20.181' -> ('10.10.20.181', default_port)
      - '10.10.20.181:22' -> ('10.10.20.181', 22)
      - 'ssh://10.10.20.181:10022' -> ('10.10.20.181', 10022)
      - '[fe80::1]:2222' -> ('fe80::1', 2222)
    """
    text = (raw_host or "").strip()
    if not text:
        return "", default_port

    if "://" in text:
        text = text.split("://", 1)[1]

    text = text.split("/")[0].split("?")[0].split("#")[0].strip()

    if text.startswith("["):
        if "]" in text:
            parts = text[1:].split("]", 1)
            host = parts[0]
            rest = parts[1]
            if rest.startswith(":"):
                try:
                    port = int(rest[1:])
                    return host, port
                except ValueError:
                    pass
            return host, default_port
        return text.strip("[]"), default_port

    if ":" in text:
        parts = text.split(":", 1)
        host = parts[0].strip()
        try:
            port = int(parts[1].strip())
            return host, port
        except ValueError:
            return host, default_port

    return text, default_port


def build_console_username(user: str, port: str | int) -> str:
    """Compose the terminal-server port-mapped username ``user:port``.

    The CX terminal server maps a console line to a username suffix (``:19`` =
    ISR4331 console, ``:18`` = ISR4451). ``user`` may already carry the suffix;
    in that case it is returned unchanged so callers can pass either form.
    """
    u = (user or "").strip()
    if ":" in u:
        return u
    p = str(port).strip()
    return f"{u}:{p}" if p else u


class SSHConsoleDevice:
    """Drive a real Cisco device directly or through a terminal server over SSH (netmiko).

    Parameters
    ----------
    host:        hostname or IP address (e.g. ``10.10.20.181`` or ``ts01.example.org``).
    username:    SSH username (e.g. ``developer`` or ``operator``).
    password:    SSH password (used in-memory only; NEVER logged/persisted).
    port_user:   console line selector (e.g. ``19``) for terminal-server mode.
    port:        SSH TCP port (default 22).
    device_type: netmiko platform driver (``cisco_ios`` / ``cisco_xe``).
    secret:      enable secret; defaults to ``password`` (typical for lab gear).
    timeout:     connect/auth timeout in seconds.
    method:      ``"direct"`` (direct SSH to device) or ``"terminal_server"``.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port_user: str | int = "",
        port: int = 22,
        device_type: str = "cisco_ios",
        secret: str = "",
        timeout: int = 20,
        method: str = "",
    ) -> None:
        if not _NETMIKO_OK:
            raise ImportError("netmiko is not installed. Run: pip install netmiko")

        clean_host, parsed_port = parse_host_port(host, default_port=int(port) if port else 22)
        self.host = clean_host
        self.port = parsed_port
        self.base_username = (username or "").strip()
        self.port_user = str(port_user).strip() if port_user is not None else ""
        if method and method.strip():
            self.method = method.strip().lower()
        else:
            self.method = "terminal_server" if self.port_user else "direct"

        if self.method == "terminal_server":
            self.console_username = build_console_username(self.base_username, self.port_user)
        else:
            self.console_username = self.base_username

        self.password = password
        self.device_type = device_type or "cisco_ios"
        self.secret = (secret or "").strip()
        self.timeout = timeout
        self._lock = threading.RLock()

        self._conn = None
        self._connected = False
        self._current_prompt: str = ""
        # Real HW identity, discovered from 'show version' on connect. node_type
        # keys the persistent cache / KB reconciliation; capability gates
        # switching vs routing advisories (device stays the source of truth).
        self.pid: str = ""
        self.node_type: str = ""
        self.capability: frozenset[str] = frozenset()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """SSH to the device or terminal server, land at prompt, enable, identify."""
        with self._lock:
            self._connect_locked()

    def _connect_locked(self) -> None:
        st = obs.StageTimer(
            "connection", "ssh_console_connect", detail=self.host,
        )
        log.info(
            "SSH connect to %s:%s as %s (method=%s, driver=%s) ...",
            self.host, self.port, self.console_username, self.method, self.device_type,
        )

        if self.method == "direct":
            try:
                handler_kwargs = {
                    "device_type": self.device_type,
                    "host": self.host,
                    "port": self.port,
                    "username": self.base_username,
                    "password": self.password,
                    "conn_timeout": self.timeout,
                    "banner_timeout": self.timeout,
                    "auth_timeout": self.timeout,
                    "global_cmd_verify": False,
                    "fast_cli": False,
                }
                if self.secret:
                    handler_kwargs["secret"] = self.secret
                with self._lock:
                    self._conn = ConnectHandler(**handler_kwargs)
                    if self.secret:
                        self._conn.secret = self.secret
            except NetmikoAuthenticationException as exc:
                st.mark("auth", "error", detail="authentication failed")
                self._log_connect_outcome(st, "error", stage="auth")
                raise ConnectionError(
                    f"Authentication failed for {self.base_username}@{self.host}:{self.port}"
                ) from exc
            except NetmikoTimeoutException as exc:
                st.mark("auth", "error", detail="timeout")
                self._log_connect_outcome(st, "error", stage="auth")
                raise ConnectionError(
                    f"Timeout connecting to {self.host}:{self.port}. "
                    "Verify device IP, port, and that VPN is connected if required."
                ) from exc
            except Exception as exc:
                st.mark("auth", "error", detail=type(exc).__name__)
                self._log_connect_outcome(st, "error", stage="auth")
                raise ConnectionError(
                    f"Cannot reach device at {self.host}:{self.port}: {exc}"
                ) from exc

            st.mark("auth", "success", detail="device SSH connected")

            # Disable paging and line-wrapping
            try:
                self._conn.send_command_timing("terminal length 0")
                self._conn.send_command_timing("terminal width 0")
            except Exception:  # pragma: no cover - width/length best-effort
                pass

            try:
                self._conn.set_base_prompt()
            except Exception:  # pragma: no cover - base prompt best-effort
                pass

            self._connected = True
            self._current_prompt = self._read_prompt()
            st.mark("prompt_ready", "success", detail=self._current_prompt)
            self._identify_device()
            st.mark(
                "identify", "success",
                detail=f"pid={self.pid or '?'} cap={self._capability_class()}",
            )
            self._log_connect_outcome(st, "success")
            log.info(
                "Direct SSH ready: %s:%s (prompt=%r pid=%r capability=%s)",
                self.host, self.port, self._current_prompt, self.pid, self._capability_class(),
            )
            return

        # Terminal Server Console Mode
        try:
            ts_kwargs = {
                "device_type": "terminal_server",
                "host": self.host,
                "port": self.port,
                "username": self.console_username,
                "password": self.password,
                "conn_timeout": self.timeout,
                "banner_timeout": self.timeout,
                "auth_timeout": self.timeout,
                "global_cmd_verify": False,
                "global_delay_factor": 1,
            }
            if self.secret:
                ts_kwargs["secret"] = self.secret
            with self._lock:
                self._conn = ConnectHandler(**ts_kwargs)
                if self.secret:
                    self._conn.secret = self.secret
        except NetmikoAuthenticationException as exc:
            st.mark("auth", "error", detail="authentication failed")
            self._log_connect_outcome(st, "error", stage="auth")
            raise ConnectionError(
                f"Authentication failed for {self.console_username}@{self.host}:{self.port}"
            ) from exc
        except NetmikoTimeoutException as exc:
            st.mark("auth", "error", detail="timeout")
            self._log_connect_outcome(st, "error", stage="auth")
            raise ConnectionError(
                f"Timeout connecting to terminal server {self.host}:{self.port}. "
                "Check host, port, and VPN connectivity."
            ) from exc
        except Exception as exc:
            st.mark("auth", "error", detail=type(exc).__name__)
            self._log_connect_outcome(st, "error", stage="auth")
            raise ConnectionError(
                f"Cannot reach terminal server {self.host}:{self.port}: {exc}"
            ) from exc
        st.mark("auth", "success", detail="terminal server attached")

        # Wake the console line and detect its base prompt.
        try:
            prompt = self._wake_console()
        except ConsoleBusyError:
            st.mark("prompt", "error", detail="console in use")
            self._log_connect_outcome(st, "error", stage="prompt")
            raise
        except TimeoutError as exc:
            st.mark("prompt", "error", detail="no prompt")
            self._log_connect_outcome(st, "error", stage="prompt")
            raise ConnectionError(
                f"No console prompt from {self.host} line "
                f"{self.port_user or self.port}. The line may be unresponsive or "
                "held by another session."
            ) from exc
        st.mark("prompt_detected", "success", detail=prompt)

        # Upgrade raw terminal-server session to target device type
        redispatch(self._conn, device_type=self.device_type, session_prep=False)
        st.mark("redispatch", "success", detail=self.device_type)

        # Mode preservation: do not run enable or disable automatically on
        # connect/reattach. CLIQ preserves the device's natural landing mode
        # ('cat9200>' or 'cat9200#'). Elevation to privileged EXEC occurs only
        # when explicitly requested (e.g. typing 'enable').

        # Stop line-wrapping so output parses cleanly; KEEP native paging (like
        # the CML path). Use *timing* (no prompt-pattern wait) so a slow console
        # can't trip a pattern-not-detected timeout on this setup command.
        try:
            # Disable paging AND line-wrap so every command's full output
            # streams back cleanly to the next prompt (no '--More--' to
            # intercept). Uses *timing* sends so a slow console can't trip a
            # pattern-not-detected timeout on these setup commands.
            self._conn.send_command_timing("terminal length 0")
            self._conn.send_command_timing("terminal width 0")
        except Exception:  # pragma: no cover - width/length best-effort
            pass
        # Re-establish the base prompt now that we are cisco_ios + privileged.
        try:
            self._conn.set_base_prompt()
        except Exception:  # pragma: no cover - base prompt best-effort
            pass

        self._connected = True
        self._current_prompt = self._read_prompt()
        st.mark("prompt_ready", "success", detail=self._current_prompt)

        # Identify the real device from 'show version' → PID → capability class.
        self._identify_device()
        st.mark(
            "identify", "success",
            detail=f"pid={self.pid or '?'} cap={self._capability_class()}",
        )
        self._log_connect_outcome(st, "success")
        log.info(
            "SSH console ready: %s (prompt=%r pid=%r capability=%s)",
            self.host, self._current_prompt, self.pid, self._capability_class(),
        )

    def _wake_console(self) -> str:
        """Poke the console line and return its detected base prompt.

        A terminal-server console line stays silent until it receives a newline,
        so we send ``\\r\\n`` (re-poking every second in case the first newline is
        eaten by a banner) and read until a ``>``/``#`` prompt appears. If the
        line reports it is occupied we raise :class:`ConsoleBusyError` so callers
        can distinguish "in use" from a wrong-password auth failure; a silent
        line eventually raises :class:`TimeoutError`.
        """
        conn = self._conn
        buf = ""
        deadline = time.time() + self.timeout
        last_poke = 0.0
        while time.time() < deadline:
            now = time.time()
            if now - last_poke >= 1.0:
                self._audit_send("\r\n", source="console_wake")
                conn.write_channel("\r\n")
                last_poke = now
            chunk = conn.read_channel()
            if chunk:
                buf += chunk
                clean_buf = _ANSI_STRIP.sub("", buf)
                if _BUSY_RE.search(clean_buf):
                    raise ConsoleBusyError(
                        f"Console line {self.console_username}@{self.host} is in "
                        "use by another session. Ensure no other session is "
                        "attached to this console port, then retry."
                    )
                tail = (
                    clean_buf.rstrip().split("\n")[-1].strip()
                    if clean_buf.strip() else ""
                )
                if tail.endswith("#") or tail.endswith(">"):
                    self._current_prompt = tail
                    return tail
            else:
                time.sleep(0.1)
        raise TimeoutError("no console prompt detected")

    def _identify_device(self) -> None:
        """Capture the PID from ``show version`` and derive the capability class.

        Feeds the existing PID→capability map with the REAL PID so, e.g., an
        ISR4331/K9 is classed router-only and VLAN/switching advisories are
        gated OFF. Best-effort: if ``show version`` can't be read the device
        stays capability-permissive (``both``) and the live device decides.
        """
        if not self.is_connected or not self._conn:
            return
        try:
            output = self.execute("show version")
        except Exception as exc:  # pragma: no cover - identify is best-effort
            log.warning("show version failed during identify: %s", exc)
            return
        pid = parse_pid_from_show_version(output)
        if not pid:
            # Fallback: the PID is embedded in the running config as
            # 'license udi pid ISR4331/K9 sn ...' even when a trimmed 'show
            # version' capture lacks the banner/PID line.
            try:
                run_cfg = self.execute("show running-config | include license udi")
                pid = parse_pid_from_running_config(run_cfg)
            except Exception as exc:  # pragma: no cover - fallback best-effort
                log.warning("running-config PID fallback failed: %s", exc)
        if not pid:
            log.info("Could not parse PID from show version; capability stays permissive.")
            return
        self.pid = pid
        self.node_type = node_type_from_pid(pid)
        self.capability = capability_from_pid(pid)
        log.info(
            "Identified device PID=%s node_type=%s capability=%s",
            self.pid, self.node_type, sorted(self.capability),
        )

    def _capability_class(self) -> str:
        caps = self.capability
        if not caps or len(caps) != 1:
            return "both"
        return next(iter(caps))

    def _log_connect_outcome(self, st, outcome: str, *, stage: str = "connect") -> None:
        """Emit the required ssh_console_connect summary event (creds redacted)."""
        obs.log_event(
            "connection", "ssh_console_connect", stage, outcome,
            transport="ssh_console",
            host=self.host,
            port_user=self.console_username,  # user:port — no password anywhere
            pid=self.pid or "",
            capability_class=self._capability_class(),
            elapsed_ms=st.total_ms(),
        )

    def reattach(self) -> str:
        """Re-establish a dropped terminal-server session; return the CLI mode.

        Terminal-server console sessions do not survive an SSH drop the way a
        CML console proxy does, so this is a clean reconnect that re-identifies
        the device and resumes at its live prompt.
        """
        with self._lock:
            log.info("Re-attaching SSH console to %s (id=%s)...", self.host, id(self))
            if self.is_connected and self._conn is not None:
                try:
                    chan = getattr(self._conn, "remote_conn", None)
                    chan_ok = (chan is None or (not getattr(chan, "closed", False) and not getattr(chan, "eof_received", False)))
                    if chan_ok and self._conn.is_alive():
                        log.info("Connection to %s is already healthy; skipping reattach", self.host)
                        return mode_from_prompt(self._current_prompt)
                except Exception:
                    pass
            self.disconnect()
            self.connect()
            return mode_from_prompt(self._current_prompt)

    def complete_login(self, username: str, password: str) -> None:
        """Interface parity with the CML path.

        The terminal-server login is completed with the password supplied to
        :meth:`connect`, so there is no parked-login stage here. Provided so the
        WebSocket auth handler can call it uniformly across transports.
        """
        raise ConnectionError(
            "SSH console login is completed at connect(); no interactive stage."
        )

    def disconnect(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.disconnect()
                except Exception:  # pragma: no cover - teardown is best-effort
                    pass
            self._conn = None
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._conn is not None

    @property
    def current_prompt(self) -> str:
        return self._current_prompt

    @property
    def awaiting_auth(self) -> bool:
        # Password is supplied up-front to netmiko, so the console never parks
        # at an interactive login stage from CLIQ's perspective.
        return False

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def _check_connection(self) -> None:
        """Raise :class:`ConnectionLostError` if the SSH channel appears dead."""
        with self._lock:
            if self._conn is None:
                raise ConnectionLostError("SSH console not connected")
            chan = getattr(self._conn, "remote_conn", None)
            if chan is not None:
                if getattr(chan, "closed", False) or getattr(chan, "eof_received", False):
                    self._connected = False
                    raise ConnectionLostError("SSH console channel closed")
                transport = getattr(chan, "transport", None)
                if transport is not None and not transport.is_active():
                    self._connected = False
                    raise ConnectionLostError("SSH console transport closed")
            try:
                alive = self._conn.is_alive()
            except Exception as exc:  # pragma: no cover - is_alive should not raise
                self._connected = False
                raise ConnectionLostError(f"SSH console unresponsive: {exc}") from exc
            if not alive:
                self._connected = False
                raise ConnectionLostError("SSH console channel closed")

    # ------------------------------------------------------------------
    # Command execution — input integrity (Bug N) enforced here
    # ------------------------------------------------------------------

    def execute(self, command: str) -> str:
        """Run ``command`` via CONTINUOUS raw-channel streaming (mirrors the CML
        backend's :meth:`CMLConsoleDevice._read_page`).

        The command is written to the raw channel EXACTLY ONCE, then the console
        byte stream is read forward continuously until the trailing IOS prompt
        appears — each byte is read once and never revisited. There is NO
        netmiko ``send_command`` (its chunked command/response model loses sync
        with a continuous console and re-reads STALE buffer content, which
        surfaced repeated partial lines — ``Async0/2/13 ... down`` forever — and
        ``^@`` empty reads), NO retry-from-buffer, and NO idle-gate re-read: the
        stream only ever advances, so stale-buffer replay is structurally
        impossible. The tracked prompt/mode is updated from the trailing prompt
        so mode transitions (``configure terminal`` -> ``Router(config)#``)
        reflect immediately.

        Input integrity (Bug N): the bytes written are EXACTLY ``command`` plus
        the single submit newline; :meth:`_audit_send` records typed-vs-sent so
        no ghost-text/echo corruption can slip onto real hardware unnoticed.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected. Call connect() first.")
        command = command.strip()
        with self._lock:
            self._check_connection()
            t0 = time.monotonic()
            self._audit_send(command + "\r\n", source="execute")
            try:
                self._conn.write_channel(command + "\r\n")
                _raw, output = self._stream_command(command, timeout=60.0)
            except Exception as exc:
                obs.log_event(
                    "connection", "run_command", "complete", "error",
                    transport="ssh_console",
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                    detail=(command.split() or ["?"])[0],
                )
                if isinstance(exc, ConnectionLostError):
                    raise
                raise ConnectionError(f"Command failed: {exc}") from exc
            obs.log_event(
                "connection", "run_command", "complete", "success",
                transport="ssh_console",
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
                detail=(command.split() or ["?"])[0], out_chars=len(output),
            )
            return output

    def execute_paged(self, command: str, on_chunk=None) -> tuple[str, bool]:
        """Run ``command`` and stream its output CONTINUOUSLY as bytes arrive.

        Mirrors the CML backend: the command is written once, then the raw
        console stream is forwarded to ``on_chunk`` in monotonic slices as it
        arrives — each byte forwarded EXACTLY once. A strict prefix guard in
        :meth:`_stream_command` makes re-emitting already-streamed text
        impossible, so no line is ever repeated on the display (the
        ``Async0/2/13 ... down`` stale-buffer replay cannot occur). Returns
        ``(page_text, more)`` with ``more`` always ``False`` — native paging is
        auto-advanced inside the stream, so there is never a pending ``--More--``.
        The web layer additionally strips any residual ``^@``/``^C`` at the
        display boundary as belt-and-suspenders.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected. Call connect() first.")
        command = command.strip()
        with self._lock:
            self._check_connection()
            t0 = time.monotonic()
            self._audit_send(command + "\r\n", source="execute_paged")
            try:
                self._conn.write_channel(command + "\r\n")
                _raw, page = self._stream_command(command, on_chunk=on_chunk, timeout=60.0)
            except Exception as exc:
                obs.log_event(
                    "connection", "run_command", "complete", "error",
                    transport="ssh_console",
                    elapsed_ms=(time.monotonic() - t0) * 1000.0,
                    detail=(command.split() or ["?"])[0],
                )
                if isinstance(exc, ConnectionLostError):
                    raise
                raise ConnectionError(f"Command failed: {exc}") from exc
            obs.log_event(
                "connection", "run_command", "complete", "success",
                transport="ssh_console",
                elapsed_ms=(time.monotonic() - t0) * 1000.0,
                detail=(command.split() or ["?"])[0], out_chars=len(page),
            )
            return page, False

    def pager_send_key(self, key: str) -> tuple[str, bool]:
        """No-op pager for the SSH transport (netmiko handles paging internally).

        Returns ``("", False)`` so the WebSocket pager handler stays uniform
        across transports; there is never a pending ``--More--`` to advance.
        """
        return "", False

    # ------------------------------------------------------------------
    # Context ('?') help — advisory, never submits a command
    # ------------------------------------------------------------------

    def context_help_start(self, partial: str) -> tuple[str, bool]:
        """Capture the device ``?``-help for ``partial`` over SSH.

        Sends ``partial?`` with NO newline (never-execute), reads the help body,
        auto-advances any ``--More--`` internally, then clears the re-displayed
        line with Ctrl-U so nothing is left buffered. Returns ``(page, more)``
        with ``more=False`` — the full help is captured in one call, mirroring
        what the ghost/handoff layer expects.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected. Call connect() first.")
        with self._lock:
            self._check_connection()

        # IOS distinguishes 'show?' (complete the token) from 'show ?' (list
        # subcommands) purely by trailing whitespace — preserve that intent.
        wants_subcommands = partial != partial.rstrip()
        body = re.sub(r"\s+", " ", partial).strip()
        help_query = body + (" ?" if wants_subcommands else "?")

        raw = self._send_help_query(help_query)
        self._clear_help_line(len(partial))
        # CRITICAL (Bug N / ^@): fully drain the channel AFTER the line-clear so
        # no residual bytes — Ctrl-U/backspace echoes or stray NUL (^@) — leak
        # into the NEXT command's input and mangle it. Re-detects the prompt
        # from the drained tail WITHOUT sending a newline (never-execute).
        self._flush_channel()
        page = _extract_context_help(_ANSI_STRIP.sub("", raw), partial)
        log.info("[SSH-HELP] partial=%r -> page=%d chars", partial, len(page))
        return page, False

    def context_help_more(self, key: str = " ") -> tuple[str, bool]:
        """Interface parity: SSH help is captured whole in :meth:`context_help_start`.

        There is never a pending help pager to advance, so this returns
        ``("", False)``.
        """
        return "", False

    def _send_help_query(self, help_query: str) -> str:
        """Write ``partial?`` (no newline), drain the help, drive any --More--.

        Uses netmiko's raw channel I/O so NO submit newline is ever sent. The
        write funnels through :meth:`_audit_send` for the input-integrity trail.
        """
        conn = self._conn
        self._audit_send(help_query, source="context_help.query")
        conn.write_channel(help_query)
        pages: list[str] = []
        buf = ""
        deadline = time.time() + 8.0
        last_data = time.time()
        while time.time() < deadline:
            chunk = conn.read_channel()
            if chunk:
                # Strip NUL (^@) at the source so it never reaches the parser.
                buf += chunk.replace("\x00", "")
                last_data = time.time()
                if _MORE_RE.search(_ANSI_STRIP.sub("", buf)):
                    # Advance the pager WITHOUT a newline (space = next page).
                    pages.append(buf)
                    buf = ""
                    self._audit_send(" ", source="context_help.more_key")
                    conn.write_channel(" ")
                    continue
            else:
                # Idle gap after some data → the help finished (a '?'-help never
                # ends in a bare device prompt, so idle means "done").
                if buf and (time.time() - last_data) >= 0.5:
                    break
                time.sleep(0.05)
        pages.append(buf)
        return "".join(pages)

    def _clear_help_line(self, partial_len: int) -> None:
        """Erase the partial line IOS re-displays after a ``?``-help capture.

        Sends ONLY Ctrl-U (kill-line) — NEVER Ctrl-C or a newline — so nothing
        is executed and no fresh prompt is emitted. Falls back to sized
        backspaces if the console swallowed Ctrl-U. Drained bytes are discarded.
        """
        conn = self._conn
        try:
            self._audit_send("\x15", source="context_help.ctrl_u")
            conn.write_channel("\x15")
            time.sleep(0.1)
            echo = _ANSI_STRIP.sub("", conn.read_channel())
            if echo.strip():
                self._audit_send("\x7f" * (partial_len + 2), source="context_help.backspace")
                conn.write_channel("\x7f" * (partial_len + 2))
                time.sleep(0.1)
                conn.read_channel()
        except Exception:  # pragma: no cover - cleanup is best-effort
            pass

    def _flush_channel(self, idle: float = 0.3, timeout: float = 2.0) -> str:
        """Read and DISCARD all residual bytes until the channel goes idle.

        Isolation guard for the ``?``-help flow: after the line-clear, any
        leftover bytes (Ctrl-U/backspace echoes, stray NUL ``^@``) are drained
        here so they cannot contaminate the next command's input. NUL bytes are
        stripped. If the drained tail happens to end in a prompt, the tracked
        prompt is refreshed — but NOTHING is written to the wire, so the
        never-execute guarantee holds.
        """
        conn = self._conn
        buf = ""
        deadline = time.time() + timeout
        last_data = time.time()
        while time.time() < deadline:
            try:
                chunk = conn.read_channel()
            except Exception:  # pragma: no cover - drain is best-effort
                break
            if chunk:
                buf += chunk.replace("\x00", "")
                last_data = time.time()
                continue
            if (time.time() - last_data) >= idle:
                break
            time.sleep(0.05)
        stripped = _ANSI_STRIP.sub("", buf).rstrip()
        last = stripped.split("\n")[-1] if stripped else ""
        if last and _PROMPT_RE.search(last):
            self._current_prompt = last.strip()
        return buf

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_prompt(self) -> str:
        """Return the device's current prompt via netmiko (default-hostname safe).

        ``find_prompt`` returns whatever the device shows — including the default
        ``Router>`` / ``Router#`` — so mode detection works without a custom
        hostname.
        """
        try:
            prompt = (self._conn.find_prompt() or "").strip()
        except Exception:  # pragma: no cover - prompt read best-effort
            return self._current_prompt
        if prompt:
            self._current_prompt = prompt
        return self._current_prompt

    @staticmethod
    def _normalize_console_output(raw: str, command: str) -> str:
        """Turn a raw terminal-server capture into a single clean block.

        A live console read is messy in three device-specific ways that must
        never reach the display; this resolves all of them:

        1. **NUL (``^@``)** — the terminal server sprays null bytes; stripped.
        2. **Bare CR line REPAINTS** — a slow console re-emits the current line
           with a bare ``\\r`` (carriage return = "overwrite from column 0")
           when output pauses/resumes (e.g. ``...up d\\r...up down``). Splitting
           on ``\\n`` alone would turn that into TWO lines (a duplicate/partial).
           Each line keeps only the text after its LAST ``\\r`` — the final
           overwrite — so the line renders once, complete.
        3. **Echoed command** — the device echoes the command as
           ``<prompt><command>`` (often with NO space, e.g.
           ``Router#show running-config``) or on its own line. The leading blank
           lines and that echo line are dropped so the command never appears to
           "run again".

        :func:`clean` is still applied afterwards for ANSI/trailing-prompt/blank
        trimming — this only handles the raw-console quirks it cannot.
        """
        if not raw:
            return ""
        text = raw.replace("\x00", "")
        # Literal caret-@ (bytes 0x5e 0x40): the terminal server emits this as
        # VISIBLE two-char text (NOT a NUL byte), which is why every prior
        # \x00-strip did nothing. Strip the exact '^@' pairing (this also clears
        # a standalone '^@' line, leaving a blank line the leading-blank skip
        # below removes). A lone '^' error marker under '% Invalid input' is
        # never followed by '@', so the real caret marker is preserved.
        text = text.replace("^@", "")
        # CRLF -> LF first; then resolve any remaining BARE CR as an in-place
        # overwrite (keep only the last non-empty part after CR on each visual line).
        text = text.replace("\r\n", "\n")
        lines = []
        for ln in text.split("\n"):
            if "\r" in ln:
                parts = [p for p in ln.split("\r") if p]
                lines.append(parts[-1] if parts else "")
            else:
                lines.append(ln)
        # Skip leading blank lines (the NUL/blank noise before the echo).
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        # Drop the echoed command line if present ('<prompt>cmd' or bare 'cmd').
        cmd_l = (command or "").strip().lower()
        if cmd_l and idx < len(lines):
            s = lines[idx].strip()
            m = _PROMPT_TOKEN_RE.match(s)
            candidate = s[m.end():].strip() if m else s
            if candidate.lower() == cmd_l:
                idx += 1
        return "\n".join(lines[idx:])

    @staticmethod
    def _find_command_echo(buf: str, command: str) -> "int | None":
        """Return the index in ``buf`` immediately AFTER this command's echo line,
        or ``None`` if the echo of ``command`` has not arrived yet.

        Terminal-server SYNC (SSH-only): the CX terminal server BUFFERS the
        previous command's response, so a read issued right after sending a new
        command can return that STALE block first — ending in its own trailing
        ``Router#`` prompt. If the reader accepted that prompt it would return
        the PREVIOUS command's output (the proven off-by-one). Synchronizing on
        THIS command's echo lets the caller discard the stale prefix and stream
        only the bytes that follow.

        The match is robust: the literal ``^@`` (0x5e 0x40) idle marker is
        stripped first, an optional leading device prompt (``Router#``) is
        removed, and the remaining text is compared to the command as typed.
        Only COMPLETE echo lines (terminated by a newline) are considered, so
        the returned index is the exact start of the command's own output.
        """
        cmd_l = (command or "").strip().lower()
        if not cmd_l:
            return 0
        # Only lines terminated by a newline are complete; the trailing element
        # of split('\n') is an in-flight partial line and must NOT be matched
        # (its output boundary is not yet known).
        parts = buf.split("\n")
        pos = 0
        for line in parts[:-1]:
            seg_len = len(line) + 1  # + the consumed '\n'
            s = line.replace("^@", "").replace("\x00", "").strip()
            m = _PROMPT_TOKEN_RE.match(s)
            candidate = s[m.end():].strip() if m else s
            if candidate.lower() == cmd_l:
                return pos + seg_len  # first byte AFTER this echo line
            pos += seg_len
        return None

    def _clean_stream(self, raw: str, command: str) -> str:
        """Single monotonic clean applied to BOTH the streamed deltas and the
        final page, so the text forwarded live is byte-identical to the block
        returned by :meth:`execute`.

        Resolves the raw-console quirks (strip NUL ``^@``, collapse bare-CR line
        repaints, drop the echoed ``<prompt><command>`` line) via
        :meth:`_normalize_console_output`, then finishes with the shared
        ANSI/trailing-prompt/blank trim in :func:`clean`. Because the transform
        is stable as the buffer grows, the streaming prefix guard can forward
        each byte exactly once.
        """
        return clean(self._normalize_console_output(raw, command), command)

    def _stream_command(self, command: str, on_chunk=None,
                        timeout: float = 60.0) -> tuple[str, str]:
        conn = self._conn
        buf = ""
        streamed = ""
        emit_pos = 0
        echo_found = False
        is_exit_cmd = bool(command and command.strip().lower() in ("exit", "quit", "logout"))
        is_direct = (self.method == "direct")
        cmd_l = (command or "").strip().lower()
        old_prompt = self._current_prompt
        deadline = time.time() + (5.0 if is_exit_cmd else timeout)

        while time.time() < deadline:
            chunk = conn.read_channel()
            if not chunk:
                # Check channel health if read returned empty
                chan = getattr(conn, "remote_conn", None)
                if chan is not None:
                    if getattr(chan, "closed", False) or getattr(chan, "eof_received", False):
                        self._connected = False
                        if is_exit_cmd:
                            raise ConnectionLostError("SSH session closed by remote host")
                        raise ConnectionLostError("SSH console channel closed")
                if hasattr(conn, "is_alive") and not conn.is_alive():
                    self._connected = False
                    if is_exit_cmd:
                        raise ConnectionLostError("SSH session closed by remote host")
                    raise ConnectionLostError("SSH console channel closed")
                time.sleep(0.04)
                continue

            if _TRACE_BYTES:
                log.debug(
                    "SSH_RAW_READ: repr=%r hex=%s",
                    chunk, [hex(ord(c)) for c in chunk],
                )
            buf += chunk.replace("\x00", "")

            # 1. Echo detection / discard
            if not echo_found:
                echo_idx = self._find_command_echo(buf, command)
                if echo_idx is not None:
                    echo_found = True
                    buf = buf[echo_idx:]
                    emit_pos = 0
                elif is_direct and "\n" in buf:
                    # In direct SSH, first complete line is the command echo
                    first_line, rest = buf.split("\n", 1)
                    first_clean = first_line.replace("\x00", "").replace("^@", "").strip().lower()
                    if cmd_l in first_clean or not first_clean:
                        buf = rest
                    echo_found = True
                    emit_pos = 0
                elif not cmd_l:
                    echo_found = True
                    emit_pos = 0

            # 2. Incremental streaming: emit clean delta as soon as available
            if on_chunk is not None and echo_found:
                clean_now = self._clean_stream(buf, command)
                if len(clean_now) > len(streamed) and clean_now.startswith(streamed):
                    delta = clean_now[len(streamed):]
                    streamed = clean_now
                    if _TRACE_BYTES:
                        log.debug(
                            "SSH_EMIT: repr=%r hex=%s",
                            delta, [hex(ord(c)) for c in delta],
                        )
                    try:
                        on_chunk(delta)
                    except Exception:
                        pass

            # 3. Termination / Prompt checks            # 3. Termination / Prompt checks
            stripped = _ANSI_STRIP.sub("", buf).rstrip()
            last = stripped.split("\n")[-1].strip() if stripped else ""

            if is_exit_cmd:
                # Mode transition on exit (e.g. config -> enable mode)
                if _PROMPT_RE.search(last) and last != old_prompt:
                    self._current_prompt = last
                    return buf, self._clean_stream(buf, command)
                # Logout banner on terminal server
                if _PRESS_RETURN_RE.search(stripped):
                    self._audit_send("\r\n", source="exit_press_return")
                    conn.write_channel("\r\n")
                    time.sleep(0.2)
                    tail = self._read_prompt()
                    if tail:
                        self._current_prompt = tail
                        return buf, self._clean_stream(buf, command)
            else:
                if _PROMPT_RE.search(last) and echo_found:
                    if on_chunk is not None:
                        clean_now = self._clean_stream(buf, command)
                        if len(clean_now) > len(streamed) and clean_now.startswith(streamed):
                            delta = clean_now[len(streamed):]
                            streamed = clean_now
                            try:
                                on_chunk(delta)
                            except Exception:
                                pass
                    self._current_prompt = last.strip()
                    return buf, self._clean_stream(buf, command)

                if _MORE_RE.search(stripped):
                    self._audit_send(" ", source="pager.more_key")
                    conn.write_channel(" ")

        # Loop deadline reached
        stripped = _ANSI_STRIP.sub("", buf).rstrip()
        last = stripped.split("\n")[-1].strip() if stripped else ""

        if is_exit_cmd:
            chan = getattr(conn, "remote_conn", None)
            if chan is not None and (getattr(chan, "closed", False) or getattr(chan, "eof_received", False)):
                self._connected = False
                raise ConnectionLostError("SSH session closed by remote host")
            if hasattr(conn, "is_alive") and not conn.is_alive():
                self._connected = False
                raise ConnectionLostError("SSH session closed by remote host")
            if _PROMPT_RE.search(last) and last != old_prompt:
                self._current_prompt = last
                return buf, self._clean_stream(buf, command)
            self._connected = False
            raise ConnectionLostError("SSH session closed by remote host")

        if _PROMPT_RE.search(last):
            self._current_prompt = last
            return buf, self._clean_stream(buf, command)

        chan = getattr(conn, "remote_conn", None)
        if chan is not None and (getattr(chan, "closed", False) or getattr(chan, "eof_received", False)):
            self._connected = False
            raise ConnectionLostError("SSH console channel closed")
        if hasattr(conn, "is_alive") and not conn.is_alive():
            self._connected = False
            raise ConnectionLostError("SSH console channel closed")

        return buf, self._clean_stream(buf, command)

    def _audit_send(self, data, source: str, secret: bool = False) -> None:
        """Single chokepoint that LOGS every byte handed to the device.

        Proves input integrity on real hardware: for a normal write the exact
        bytes are recorded (typed == sent). Credential writes pass
        ``secret=True`` so only a redacted length is logged — a password can
        never leak through the audit trail. This method does NOT itself write to
        the wire (netmiko owns the channel); it exists so every send site is
        observable and auditable, matching the CML backend's contract.
        """
        try:
            payload = data if isinstance(data, str) else str(data)
            has_submit = ("\r" in payload) or ("\n" in payload)
            if secret:
                log.debug(
                    "DEVICE-WRITE source=%s submit=%s bytes=<redacted len=%d>",
                    source, has_submit, len(payload),
                )
            else:
                log.debug(
                    "DEVICE-WRITE source=%s submit=%s bytes=%r",
                    source, has_submit, payload,
                )
            # Input-integrity trail (redacted): prove typed == sent on real HW.
            obs.log_validation(
                "input", "input_integrity", "verbatim",
                transport="ssh_console",
                detail=f"source={source} sent={'<redacted>' if secret else payload!r}",
            )
        except Exception:  # pragma: no cover - auditing must never break I/O
            pass

    def check_enable_mode(self) -> bool:
        """Return True if the session is currently in privileged EXEC mode ('#')."""
        with self._lock:
            if not self.is_connected or not self._conn:
                return False
            try:
                if self._conn.check_enable_mode():
                    return True
            except Exception:
                pass
            return bool(self._current_prompt and self._current_prompt.strip().endswith("#"))

    def enable(self, secret: str = "") -> bool:
        """Elevate the session to privileged EXEC mode ('#').

        Parameters
        ----------
        secret: Optional enable secret to use (and retain for this session).

        Returns
        -------
        True if the session is confirmed in privileged mode ('#').

        Raises
        ------
        ConnectionError: if disconnected or if device rejects authentication.
        ValueError: if no enable secret is available.
        """
        with self._lock:
            if not self.is_connected or not self._conn:
                raise ConnectionError("Not connected. Call connect() first.")

            # If current prompt already indicates privileged EXEC mode ('#'), return success without calling enable() again
            if self.check_enable_mode():
                self._current_prompt = self._read_prompt()
                return True

            sec = (secret or self.secret or "").strip()
            if not sec:
                raise ValueError("No enable secret configured. Enter the enable secret at login to access privileged mode.")

            self.secret = sec
            self._conn.secret = sec
            try:
                self._conn.enable()
                self._current_prompt = self._read_prompt()
                if not self.check_enable_mode():
                    raise ConnectionError("Enable authentication rejected by device.")
                return True
            except Exception as exc:
                try:
                    self._current_prompt = self._read_prompt()
                except Exception:
                    pass
                clean_err = str(exc).replace(sec, "<redacted>")
                log.warning("enable failed: %s", clean_err)
                raise ConnectionError(f"Enable authentication failed: {clean_err}") from exc

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "SSHConsoleDevice":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        state = "connected" if self.is_connected else "disconnected"
        return f"SSHConsoleDevice({self.console_username}@{self.host}, {state})"
